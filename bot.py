import os
import logging
import base64
from datetime import datetime, timedelta, timezone

import anthropic
import discord
import httpx
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

# GitHub settings
GITHUB_REPO = os.environ.get("GITHUB_REPO", "pite1222/conductor")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "zephyr-4.1")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# 全履歴を取得する優先チャンネル名 (カンマ区切り、部分一致)
PRIORITY_CHANNEL_NAMES = [
    name.strip().lower()
    for name in os.environ.get("PRIORITY_CHANNEL_NAMES", "トラブルシューティング,troubleshoot").split(",")
    if name.strip()
]

# --- Advisor Strategy ---
ADVISOR_TOOL = {
    "type": "advisor_20260301",
    "name": "advisor",
    "model": ADVISOR_MODEL,
    "max_uses": ADVISOR_MAX_USES,
}
ADVISOR_HEADERS = {"anthropic-beta": "advisor-tool-2026-03-01"}

# --- GitHub Tools ---
GITHUB_TOOLS = [
    {
        "name": "get_repo_tree",
        "description": "GitHubリポジトリのディレクトリ構造を取得する。パスを指定するとそのディレクトリの中身を返す。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "取得するディレクトリパス（例: 'config', 'boards/shields'）。ルートの場合は空文字。",
                    "default": "",
                }
            },
        },
    },
    {
        "name": "get_file_contents",
        "description": "GitHubリポジトリから指定したファイルの内容を取得する。コード、設定ファイル、READMEなどを読む時に使う。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "ファイルパス（例: 'config/conductor.keymap', 'README.md'）",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": "GitHubリポジトリ内のコードを検索する。キーワードに一致するファイルとコード断片を返す。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "検索クエリ（例: 'LED', 'battery', 'PMW3610'）",
                }
            },
            "required": ["query"],
        },
    },
]

# --- GitHub API helpers ---
_http = httpx.Client(timeout=15)


def _github_headers() -> dict:
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    return headers


def github_get_tree(path: str = "") -> str:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    params = {"ref": GITHUB_BRANCH}
    resp = _http.get(url, headers=_github_headers(), params=params)
    if resp.status_code != 200:
        return f"Error: {resp.status_code} {resp.text[:200]}"
    items = resp.json()
    if isinstance(items, dict):
        # Single file, not a directory
        return f"{items['name']} ({items['type']}, {items.get('size', '?')} bytes)"
    lines = []
    for item in items:
        icon = "📁" if item["type"] == "dir" else "📄"
        size = f" ({item.get('size', '?')}B)" if item["type"] == "file" else ""
        lines.append(f"{icon} {item['path']}{size}")
    return "\n".join(lines)


def github_get_file(path: str) -> str:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    params = {"ref": GITHUB_BRANCH}
    resp = _http.get(url, headers=_github_headers(), params=params)
    if resp.status_code != 200:
        return f"Error: {resp.status_code} — file not found or inaccessible"
    data = resp.json()
    if data.get("type") != "file":
        return f"Error: '{path}' is a directory, not a file. Use get_repo_tree instead."
    content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    # Truncate very large files
    if len(content) > 8000:
        content = content[:8000] + f"\n\n... (truncated, total {len(content)} chars)"
    return content


def github_search_code(query: str) -> str:
    url = "https://api.github.com/search/code"
    params = {"q": f"{query} repo:{GITHUB_REPO}"}
    resp = _http.get(url, headers=_github_headers(), params=params)
    if resp.status_code != 200:
        return f"Error: {resp.status_code} {resp.text[:200]}"
    data = resp.json()
    if data["total_count"] == 0:
        return f"No results found for '{query}'"
    results = []
    for item in data["items"][:10]:
        results.append(f"📄 {item['path']}")
    return f"Found {data['total_count']} files:\n" + "\n".join(results)


def handle_tool_call(name: str, input_data: dict) -> str:
    logger.info("Tool call: %s(%s)", name, input_data)
    if name == "get_repo_tree":
        return github_get_tree(input_data.get("path", ""))
    elif name == "get_file_contents":
        return github_get_file(input_data["path"])
    elif name == "search_code":
        return github_search_code(input_data["query"])
    return f"Unknown tool: {name}"


# --- Claude クライアント ---
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
logger.info("Claude SDK v%s / model=%s / advisor=%s (max_uses=%d) / repo=%s@%s",
            anthropic.__version__, CLAUDE_MODEL, ADVISOR_MODEL, ADVISOR_MAX_USES,
            GITHUB_REPO, GITHUB_BRANCH)

# --- Discord ボット ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = discord.Client(intents=intents)

# --- 優先チャンネルの全履歴キャッシュ ---
priority_cache: dict[int, list[str]] = {}
priority_cache_last_id: dict[int, int] = {}


def is_priority_channel(channel: discord.TextChannel) -> bool:
    name = channel.name.lower()
    return any(keyword in name for keyword in PRIORITY_CHANNEL_NAMES)


def format_message(msg: discord.Message) -> str:
    timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M")
    text = f"[{timestamp}] {msg.author.display_name}: {msg.content}"
    if msg.attachments:
        files = ", ".join(a.filename for a in msg.attachments)
        text += f" [添付: {files}]"
    return text


async def load_full_history(channel: discord.TextChannel) -> list[str]:
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


MAX_CONTEXT_CHARS = int(os.environ.get("MAX_CONTEXT_CHARS", "15000"))


async def fetch_server_context(guild: discord.Guild) -> str:
    all_history = []
    total_chars = 0
    for channel in guild.text_channels:
        if total_chars >= MAX_CONTEXT_CHARS:
            break
        permissions = channel.permissions_for(guild.me)
        if not permissions.read_messages or not permissions.read_message_history:
            continue
        if is_priority_channel(channel):
            await update_priority_cache(channel)
            history = priority_cache.get(channel.id, [])
            if history:
                # Use only the most recent messages to stay within limits
                recent = history[-100:]
                header = f"=== #{channel.name} (最新{len(recent)}件 / 全{len(history)}件) ==="
                all_history.append(header)
                all_history.extend(recent)
                all_history.append("")
                total_chars += sum(len(m) for m in recent)
        else:
            history = await fetch_channel_history(channel, limit=HISTORY_FETCH_LIMIT)
            if history:
                all_history.append(f"=== #{channel.name} ===")
                all_history.extend(history)
                all_history.append("")
                total_chars += sum(len(m) for m in history)

    result = "\n".join(all_history)
    if len(result) > MAX_CONTEXT_CHARS:
        result = result[-MAX_CONTEXT_CHARS:]
    return result


async def generate_answer(question: str, server_context: str) -> str:
    """Agentic loop: Claude can call GitHub tools to fetch repo info."""
    system = f"""{SYSTEM_PROMPT}

## 回答の優先順位
1. **最優先: GitHubリポジトリの情報** — ファームウェアの仕様・設定・コードに関する質問は、GitHubツールを使ってリポジトリ（{GITHUB_REPO}@{GITHUB_BRANCH}）のソースコード・README・設定ファイルを取得し、その内容を根拠に回答してください。積極的にツールを使ってください。
2. **補足: Discordサーバーの履歴** — 過去のやり取りやトラブルシューティングの実例として参照してください。
3. **一般知識** — 上記に該当しない場合のみ、一般的な知識で回答してください。

## Discordサーバー履歴
特に「トラブルシューティング」チャンネルには過去の全履歴が含まれています。

--- サーバー履歴 ---
{server_context}
--- 履歴ここまで ---"""

    all_tools = GITHUB_TOOLS + [ADVISOR_TOOL]
    messages = [{"role": "user", "content": question}]
    max_iterations = 8

    for i in range(max_iterations):
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=system,
            messages=messages,
            tools=all_tools,
            extra_headers=ADVISOR_HEADERS,
        )
        logger.info("Claude応答 [iter=%d]: stop_reason=%s, blocks=%d",
                    i, response.stop_reason, len(response.content))

        if response.stop_reason != "tool_use":
            return "".join(
                block.text for block in response.content if hasattr(block, "text")
            )

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = handle_tool_call(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    # Fallback if max iterations reached
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
    if message.author == bot.user:
        return
    if message.author.bot:
        return

    logger.info("MSG [#%s] ch_id=%s %s: %s",
                getattr(message.channel, 'name', '?'), message.channel.id,
                message.author.name, message.content[:80])

    if TARGET_CHANNEL_IDS and message.channel.id not in TARGET_CHANNEL_IDS:
        return

    logger.info("応答開始 [#%s] %s: %s",
                message.channel.name, message.author.name, message.content[:80])

    async with message.channel.typing():
        try:
            server_context = await fetch_server_context(message.guild)
            logger.info("サーバー履歴取得完了 (%d文字)", len(server_context))
            answer = await generate_answer(message.content, server_context)
        except Exception:
            logger.exception("回答生成に失敗しました")
            await message.reply("申し訳ありません。回答の生成中にエラーが発生しました。")
            return

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
