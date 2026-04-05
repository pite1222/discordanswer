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
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "あなたはDiscordサーバーの親切なアシスタントです。質問に対して簡潔で分かりやすい日本語で回答してください。",
)
# サーバー履歴の取得設定
HISTORY_FETCH_LIMIT = int(os.environ.get("HISTORY_FETCH_LIMIT", "200"))
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "30"))

# --- Claude クライアント ---
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# --- Discord ボット ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = discord.Client(intents=intents)


def is_question(text: str) -> bool:
    """メッセージが質問かどうかを簡易判定する。"""
    question_markers = ["?", "？", "教えて", "分かる", "わかる", "どう", "なぜ", "なに", "何"]
    return any(marker in text for marker in question_markers)


async def fetch_channel_history(channel: discord.TextChannel, limit: int = 200) -> list[str]:
    """チャンネルの最近のメッセージ履歴を取得する。"""
    messages = []
    after = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
    try:
        async for msg in channel.history(limit=limit, after=after, oldest_first=False):
            if msg.author.bot and msg.author == bot.user:
                continue
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M")
            messages.append(f"[{timestamp}] {msg.author.display_name}: {msg.content}")
    except discord.Forbidden:
        logger.warning("チャンネル #%s の履歴取得権限がありません", channel.name)
    except Exception:
        logger.exception("チャンネル #%s の履歴取得に失敗", channel.name)
    messages.reverse()
    return messages


async def fetch_server_context(guild: discord.Guild) -> str:
    """サーバー内のテキストチャンネルからメッセージ履歴を収集する。"""
    all_history = []

    for channel in guild.text_channels:
        # 読み取り権限があるチャンネルのみ
        permissions = channel.permissions_for(guild.me)
        if not permissions.read_messages or not permissions.read_message_history:
            continue

        history = await fetch_channel_history(channel, limit=HISTORY_FETCH_LIMIT)
        if history:
            all_history.append(f"=== #{channel.name} ===")
            all_history.extend(history)
            all_history.append("")

    return "\n".join(all_history)


async def generate_answer(question: str, server_context: str) -> str:
    """サーバーの履歴をコンテキストとして、Claude API で回答を生成する。"""
    system = f"""{SYSTEM_PROMPT}

以下はこのDiscordサーバー内の最近のメッセージ履歴です。
質問に回答する際は、このサーバーの内容・文脈を踏まえて回答してください。
サーバーの履歴に関連する情報がない場合は、その旨を伝えた上で一般的な知識で回答してください。

--- サーバー履歴 ---
{server_context}
--- 履歴ここまで ---"""

    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


@bot.event
async def on_ready():
    logger.info("ボット起動: %s (ID: %s)", bot.user.name, bot.user.id)
    if TARGET_CHANNEL_IDS:
        logger.info("監視チャンネル: %s", TARGET_CHANNEL_IDS)
    else:
        logger.warning("TARGET_CHANNEL_IDS が未設定です。すべてのチャンネルで応答します。")


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
            # サーバーのメッセージ履歴を取得
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
