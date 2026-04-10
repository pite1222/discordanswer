import os
import logging
from datetime import datetime, timedelta, timezone

import anthropic
import discord
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# --- 設定 ---
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TARGET_CHANNEL_IDS = {
    int(cid.strip())
    for cid in os.environ.get("TARGET_CHANNEL_IDS", "").split(",")
    if cid.strip()
}
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
ADVISOR_MODEL = os.environ.get("ADVISOR_MODEL", "claude-opus-4-6")
ADVISOR_MAX_USES = int(os.environ.get("ADVISOR_MAX_USES", "2"))
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "あなたはDiscordサーバーの親切なアシスタントです。質問に対して簡潔で分かりやすい日本語で回答してください。",
)
HISTORY_FETCH_LIMIT = int(os.environ.get("HISTORY_FETCH_LIMIT", "200"))
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "30"))

# 全履歴を取得する優先チャンネル名 (カンマ区切り、部分一致)
PRIORITY_CHANNEL_NAMES = [
    name.strip().lower()
    for name in os.environ.get("PRIORITY_CHANNEL_NAMES", "トラブルシューティング,troubleshoot").split(",")
    if name.strip()
]

# --- Claude クライアント ---
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# --- Discord ボット ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = discord.Client(intents=intents)

# --- 優先チャンネルの全履歴キャッシュ ---
# { channel_id: [formatted_message, ...] }
priority_cache: dict[int, list[str]] = {}
# 最後にキャッシュしたメッセージのID (差分取得用)
priority_cache_last_id: dict[int, int] = {}


def is_priority_channel(channel: discord.TextChannel) -> bool:
    """優先チャンネル（全履歴取得対象）かどうかを判定する。"""
    name = channel.name.lower()
    return any(keyword in name for keyword in PRIORITY_CHANNEL_NAMES)


def is_question(text: str) -> bool:
    """メッセージが質問かどうかを簡易判定する。"""
    question_markers = ["?", "？", "教えて", "分かる", "わかる", "どう", "なぜ", "なに", "何"]
    return any(marker in text for marker in question_markers)


def format_message(msg: discord.Message) -> str:
    """メッセージをテキスト形式にフォーマットする。"""
    timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M")
    text = f"[{timestamp}] {msg.author.display_name}: {msg.content}"
    # 添付ファイル名も記録
    if msg.attachments:
        files = ", ".join(a.filename for a in msg.attachments)
        text += f" [添付: {files}]"
    return text


async def load_full_history(channel: discord.TextChannel) -> list[str]:
    """チャンネルの全履歴を取得する。"""
    messages = []
    count = 0
    try:
        async for msg in channel.history(limit=None, oldest_first=True):
            if msg.author == bot.user:
                continue
            messages.append(format_message(msg))
            count += 1
            if count % 500 == 0:
                logger.info("  #%s: %d件取得中...", channel.name, count)
    except discord.Forbidden:
        logger.warning("チャンネル #%s の履歴取得権限がありません", channel.name)
    except Exception:
        logger.exception("チャンネル #%s の全履歴取得に失敗", channel.name)
    logger.info("  #%s: 全履歴 %d件 取得完了", channel.name, len(messages))
    return messages


async def update_priority_cache(channel: discord.TextChannel):
    """優先チャンネルのキャッシュを差分更新する。"""
    if channel.id not in priority_cache:
        return

    last_id = priority_cache_last_id.get(channel.id)
    if not last_id:
        return

    new_messages = []
    try:
        after = discord.Object(id=last_id)
        async for msg in channel.history(limit=None, after=after, oldest_first=True):
            if msg.author == bot.user:
                continue
            new_messages.append(format_message(msg))
            last_id = msg.id
    except Exception:
        logger.exception("キャッシュ差分更新に失敗: #%s", channel.name)
        return

    if new_messages:
        priority_cache[channel.id].extend(new_messages)
        priority_cache_last_id[channel.id] = last_id
        logger.info("#%s: キャッシュに %d件 追加 (合計 %d件)",
                    channel.name, len(new_messages), len(priority_cache[channel.id]))


async def fetch_channel_history(channel: discord.TextChannel, limit: int = 200) -> list[str]:
    """通常チャンネルの最近のメッセージ履歴を取得する。"""
    messages = []
    after = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
    try:
        async for msg in channel.history(limit=limit, after=after, oldest_first=False):
            if msg.author == bot.user:
                continue
            messages.append(format_message(msg))
    except discord.Forbidden:
        pass
    except Exception:
        logger.exception("チャンネル #%s の履歴取得に失敗", channel.name)
    messages.reverse()
    return messages


async def fetch_server_context(guild: discord.Guild) -> str:
    """サーバー内のテキストチャンネルからメッセージ履歴を収集する。"""
    all_history = []

    for channel in guild.text_channels:
        permissions = channel.permissions_for(guild.me)
        if not permissions.read_messages or not permissions.read_message_history:
            continue

        if is_priority_channel(channel):
            # 優先チャンネル: キャッシュを差分更新して全履歴を使う
            await update_priority_cache(channel)
            history = priority_cache.get(channel.id, [])
            if history:
                all_history.append(f"=== #{channel.name} (全履歴 {len(history)}件) ===")
                all_history.extend(history)
                all_history.append("")
        else:
            # 通常チャンネル: 最近の履歴のみ
            history = await fetch_channel_history(channel, limit=HISTORY_FETCH_LIMIT)
            if history:
                all_history.append(f"=== #{channel.name} ===")
                all_history.extend(history)
                all_history.append("")

    return "\n".join(all_history)


async def generate_answer(question: str, server_context: str) -> str:
    """サーバーの履歴をコンテキストとして、Claude API で回答を生成する。"""
    system = f"""{SYSTEM_PROMPT}

以下はこのDiscordサーバー内のメッセージ履歴です。
特に「トラブルシューティング」チャンネルには過去の全履歴が含まれています。
質問に回答する際は、このサーバーの内容・過去のやり取りを踏まえて回答してください。
サーバーの履歴に関連する情報がない場合は、その旨を伝えた上で一般的な知識で回答してください。

--- サーバー履歴 ---
{server_context}
--- 履歴ここまで ---"""

    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": question}],
        tools=[
            {
                "type": "advisor_20260301",
                "name": "advisor",
                "model": ADVISOR_MODEL,
                "max_uses": ADVISOR_MAX_USES,
            },
        ],
        extra_headers={"anthropic-beta": "advisor-tool-2026-03-01"},
    )
    return "".join(
        block.text for block in response.content if hasattr(block, "text")
    )


@bot.event
async def on_ready():
    logger.info("ボット起動: %s (ID: %s)", bot.user.name, bot.user.id)
    if TARGET_CHANNEL_IDS:
        logger.info("監視チャンネル: %s", TARGET_CHANNEL_IDS)
    else:
        logger.warning("TARGET_CHANNEL_IDS が未設定です。すべてのチャンネルで応答します。")

    # 起動時に優先チャンネルの全履歴を事前読み込み
    logger.info("優先チャンネルの全履歴を読み込み中...")
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if is_priority_channel(channel):
                permissions = channel.permissions_for(guild.me)
                if not permissions.read_messages or not permissions.read_message_history:
                    logger.warning("  #%s: 権限不足でスキップ", channel.name)
                    continue
                logger.info("  #%s の全履歴を取得開始...", channel.name)
                history = await load_full_history(channel)
                priority_cache[channel.id] = history
                # 最後のメッセージIDを記録
                try:
                    async for msg in channel.history(limit=1):
                        priority_cache_last_id[channel.id] = msg.id
                        break
                except Exception:
                    pass
    logger.info("優先チャンネルの読み込み完了 (合計 %d件)",
                sum(len(v) for v in priority_cache.values()))
    logger.info("ボット準備完了！")


@bot.event
async def on_message(message: discord.Message):
    # 自分自身のメッセージは無視
    if message.author == bot.user:
        return

    # Botのメッセージは無視
    if message.author.bot:
        return

    # チャンネルフィルタ (未設定なら全チャンネル対象)
    if TARGET_CHANNEL_IDS and message.channel.id not in TARGET_CHANNEL_IDS:
        return

    # 質問判定
    if not is_question(message.content):
        return

    logger.info(
        "質問検出 [#%s] %s: %s",
        message.channel.name,
        message.author.name,
        message.content[:80],
    )

    async with message.channel.typing():
        try:
            server_context = await fetch_server_context(message.guild)
            logger.info("サーバー履歴取得完了 (%d文字)", len(server_context))
            answer = await generate_answer(message.content, server_context)
        except Exception:
            logger.exception("回答生成に失敗しました")
            await message.reply("申し訳ありません。回答の生成中にエラーが発生しました。")
            return

    # 2000文字制限の対応
    if len(answer) > 2000:
        for i in range(0, len(answer), 2000):
            await message.reply(answer[i : i + 2000])
    else:
        await message.reply(answer)


def main():
    if not DISCORD_BOT_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN が設定されていません。")
    if not ANTHROPIC_API_KEY:
        raise SystemExit("ANTHROPIC_API_KEY が設定されていません。")
    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
