import asyncio
import json
import os
import re
import logging
import time
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
ADVISOR_MODEL = os.environ.get("ADVISOR_MODEL", "claude-opus-4-8")
ADVISOR_MAX_USES = int(os.environ.get("ADVISOR_MAX_USES", "2"))
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "あなたはDiscordサーバーの親切なアシスタントです。質問に対して簡潔で分かりやすい日本語で回答してください。",
)
HISTORY_FETCH_LIMIT = int(os.environ.get("HISTORY_FETCH_LIMIT", "200"))
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "30"))
CONVERSATION_CONTEXT_LIMIT = int(os.environ.get("CONVERSATION_CONTEXT_LIMIT", "20"))
CONVERSATION_CONTEXT_MINUTES = int(os.environ.get("CONVERSATION_CONTEXT_MINUTES", "180"))
MAX_CONVERSATION_CONTEXT_CHARS = int(os.environ.get("MAX_CONVERSATION_CONTEXT_CHARS", "6000"))
SERVER_CONTEXT_TTL_SECONDS = int(os.environ.get("SERVER_CONTEXT_TTL_SECONDS", "120"))
PRIORITY_HISTORY_LIMIT = int(os.environ.get("PRIORITY_HISTORY_LIMIT", "1000"))
STUDIO_GUIDE_REFRESH_SECONDS = int(os.environ.get("STUDIO_GUIDE_REFRESH_SECONDS", "21600"))
GATE_MODEL = os.environ.get("GATE_MODEL", "claude-haiku-4-5")

# Notion settings
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_CORRECTIONS_DB_ID = os.environ.get("NOTION_CORRECTIONS_DB_ID", "")

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

DAILY_TOKEN_LIMIT = int(os.environ.get("DAILY_TOKEN_LIMIT", "2000000"))  # 0で無効
HEALTH_CHECK_INTERVAL_SECONDS = int(os.environ.get("HEALTH_CHECK_INTERVAL_SECONDS", "21600"))

# --- 共有HTTPクライアント (接続プール再利用) ---
http_client = httpx.AsyncClient(timeout=15, follow_redirects=True)

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

# --- GitHub API helpers (async) ---
def _github_headers() -> dict:
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    return headers


async def github_get_tree(path: str = "") -> str:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    params = {"ref": GITHUB_BRANCH}
    resp = await http_client.get(url, headers=_github_headers(), params=params)
    if resp.status_code != 200:
        return f"Error: {resp.status_code} {resp.text[:200]}"
    items = resp.json()
    if isinstance(items, dict):
        return f"{items['name']} ({items['type']}, {items.get('size', '?')} bytes)"
    lines = []
    for item in items:
        icon = "📁" if item["type"] == "dir" else "📄"
        size = f" ({item.get('size', '?')}B)" if item["type"] == "file" else ""
        lines.append(f"{icon} {item['path']}{size}")
    return "\n".join(lines)


async def github_get_file(path: str) -> str:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    params = {"ref": GITHUB_BRANCH}
    resp = await http_client.get(url, headers=_github_headers(), params=params)
    if resp.status_code != 200:
        return f"Error: {resp.status_code} — file not found or inaccessible"
    data = resp.json()
    if data.get("type") != "file":
        return f"Error: '{path}' is a directory, not a file. Use get_repo_tree instead."
    content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    if len(content) > 8000:
        content = content[:8000] + f"\n\n... (truncated, total {len(content)} chars)"
    return content


async def github_search_code(query: str) -> str:
    url = "https://api.github.com/search/code"
    params = {"q": f"{query} repo:{GITHUB_REPO}"}
    resp = await http_client.get(url, headers=_github_headers(), params=params)
    if resp.status_code != 200:
        return f"Error: {resp.status_code} {resp.text[:200]}"
    data = resp.json()
    if data["total_count"] == 0:
        return f"No results found for '{query}'"
    results = []
    for item in data["items"][:10]:
        results.append(f"📄 {item['path']}")
    return f"Found {data['total_count']} files:\n" + "\n".join(results)


# --- Notion Tools ---
NOTION_TOOLS = [
    {
        "name": "search_notion",
        "description": "Notionのユーザーガイドを検索する。キーワードでページを探し、タイトルとIDを返す。回答の最優先ソース。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "検索キーワード（例: 'キーマップ', 'LED設定', 'トラブルシューティング'）",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_notion_page",
        "description": "Notionページの内容を取得する。search_notionで見つけたページIDを指定して詳細を読む。",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "NotionページのID（search_notionの結果から取得）",
                }
            },
            "required": ["page_id"],
        },
    },
]


# --- Notion API helpers (async) ---
def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def _extract_rich_text(rich_text_array: list) -> str:
    return "".join(rt.get("plain_text", "") for rt in rich_text_array)


def _blocks_to_text(blocks: list) -> str:
    lines = []
    for block in blocks:
        btype = block.get("type", "")
        data = block.get(btype, {})
        text = ""
        if btype in ("paragraph", "bulleted_list_item", "numbered_list_item", "to_do", "toggle", "quote", "callout"):
            text = _extract_rich_text(data.get("rich_text", []))
        elif btype.startswith("heading_"):
            text = _extract_rich_text(data.get("rich_text", []))
            level = btype[-1]
            text = "#" * int(level) + " " + text
        elif btype == "code":
            code = _extract_rich_text(data.get("rich_text", []))
            lang = data.get("language", "")
            text = f"```{lang}\n{code}\n```"
        elif btype == "divider":
            text = "---"
        elif btype == "table_row":
            cells = data.get("cells", [])
            text = " | ".join(_extract_rich_text(cell) for cell in cells)

        prefix = ""
        if btype == "bulleted_list_item":
            prefix = "- "
        elif btype == "numbered_list_item":
            prefix = "1. "
        elif btype == "to_do":
            checked = "x" if data.get("checked") else " "
            prefix = f"[{checked}] "

        if text:
            lines.append(prefix + text)
    return "\n".join(lines)


async def notion_search(query: str) -> str:
    if not NOTION_TOKEN:
        return "Error: NOTION_TOKEN が設定されていません"
    url = "https://api.notion.com/v1/search"
    body = {"query": query, "page_size": 10}
    resp = await http_client.post(url, headers=_notion_headers(), json=body)
    if resp.status_code != 200:
        return f"Error: {resp.status_code} {resp.text[:200]}"
    data = resp.json()
    results = []
    for item in data.get("results", []):
        obj_type = item.get("object")
        item_id = item["id"]
        title = ""
        if obj_type == "page":
            props = item.get("properties", {})
            for prop in props.values():
                if prop.get("type") == "title":
                    title = _extract_rich_text(prop.get("title", []))
                    break
            if not title:
                title = "(無題)"
        elif obj_type == "database":
            title = _extract_rich_text(item.get("title", []))
            title = f"[DB] {title}"
        results.append(f"- {title} (id: {item_id})")
    if not results:
        return f"'{query}' に一致するページが見つかりません"
    return f"{len(results)}件見つかりました:\n" + "\n".join(results)


async def notion_get_page(page_id: str) -> str:
    if not NOTION_TOKEN:
        return "Error: NOTION_TOKEN が設定されていません"
    all_blocks = []
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    params = {"page_size": 100}
    while True:
        resp = await http_client.get(url, headers=_notion_headers(), params=params)
        if resp.status_code != 200:
            return f"Error: {resp.status_code} {resp.text[:200]}"
        data = resp.json()
        all_blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        params["start_cursor"] = data["next_cursor"]

    expanded = []
    for block in all_blocks:
        expanded.append(block)
        if block.get("has_children") and block["type"] not in ("child_page", "child_database"):
            child_url = f"https://api.notion.com/v1/blocks/{block['id']}/children"
            child_resp = await http_client.get(child_url, headers=_notion_headers(), params={"page_size": 100})
            if child_resp.status_code == 200:
                children = child_resp.json().get("results", [])
                expanded.extend(children)

    content = _blocks_to_text(expanded)
    if len(content) > 10000:
        content = content[:10000] + f"\n\n... (truncated, total {len(content)} chars)"
    return content if content else "(ページの内容が空です)"


# --- Corrections (訂正ナレッジ) ---
BUILTIN_CORRECTIONS = [
    {
        "topic": "リセット後のUF2コピーで容量不足やStudio未認識になる",
        "correction": (
            "リセットモードでR/LそれぞれのファームウェアUF2をコピーするときに容量不足が出る、"
            "電源Off/Onしないと接続されない、Conductor Studioでファームウェアバージョンが表示されない場合は、"
            "macOS側で作られる.DS_Storeなどの隠しファイルや、古いUF2ファイルがドライブ容量を圧迫している可能性が高いです。"
            "対処は、ブートローダーモードに入った後Finderでドライブを開き、Cmd+Shift+.で隠しファイルを表示して不要ファイルを削除してからUF2をコピーするか、"
            "ターミナルから `cp ~/Downloads/monokey_R-rgbled_adapter-xiao_ble-zmk.uf2 /Volumes/XIAO-SENSE/` のように直接コピーします。"
            "Mac固有の状態が原因の可能性があるため、別のPCで試すと切り分けしやすいです。"
        ),
        "question": (
            "リセットファイルとR/Lのファームウェアを入れたが容量不足が出て、電源Off/Onしないと接続されず、"
            "Conductor Studioでファームウェアバージョンが表示されない"
        ),
    }
]


def _extract_topic(text: str) -> str:
    for sep in ("。", "、", "\n", ". ", ", "):
        idx = text.find(sep)
        if idx > 0:
            return text[:idx]
    return text[:50]


async def load_corrections() -> list[dict] | None:
    # Notionエラー時はNoneを返し、呼び出し側で既存キャッシュを維持する
    results = [dict(item) for item in BUILTIN_CORRECTIONS]
    if not NOTION_TOKEN or not NOTION_CORRECTIONS_DB_ID:
        return results
    url = f"https://api.notion.com/v1/databases/{NOTION_CORRECTIONS_DB_ID}/query"
    body: dict = {"page_size": 100}
    try:
        while True:
            resp = await http_client.post(url, headers=_notion_headers(), json=body)
            if resp.status_code != 200:
                logger.error("訂正DB読み込み失敗: %d %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            for page in data.get("results", []):
                props = page.get("properties", {})
                topic = _extract_rich_text(props.get("Topic", {}).get("title", []))
                correction = _extract_rich_text(props.get("Correction", {}).get("rich_text", []))
                question = _extract_rich_text(props.get("Question", {}).get("rich_text", []))
                if correction:
                    results.append({
                        "topic": topic,
                        "correction": correction,
                        "question": question,
                    })
            if not data.get("has_more"):
                break
            body["start_cursor"] = data["next_cursor"]
    except Exception:
        logger.exception("訂正DB読み込みに失敗")
        return None
    logger.info("訂正データ %d件 読み込み完了", len(results))
    return results


async def save_correction(topic: str, correction: str, question: str, wrong_answer: str) -> bool:
    if not NOTION_TOKEN or not NOTION_CORRECTIONS_DB_ID:
        return False
    url = "https://api.notion.com/v1/pages"
    body = {
        "parent": {"database_id": NOTION_CORRECTIONS_DB_ID},
        "properties": {
            "Topic": {"title": [{"text": {"content": topic[:200]}}]},
            "Correction": {"rich_text": [{"text": {"content": correction[:2000]}}]},
            "Question": {"rich_text": [{"text": {"content": question[:2000]}}]},
            "WrongAnswer": {"rich_text": [{"text": {"content": wrong_answer[:2000]}}]},
        },
    }
    try:
        resp = await http_client.post(url, headers=_notion_headers(), json=body)
    except Exception:
        logger.exception("訂正保存に失敗")
        return False
    if resp.status_code != 200:
        logger.error("訂正保存失敗: %d %s", resp.status_code, resp.text[:200])
        return False
    logger.info("訂正保存完了: topic=%s", topic)
    return True


async def extract_knowledge(history: list[str], existing_topics: list[str]) -> list[dict]:
    history_text = "\n".join(history[-300:])
    existing_str = "\n".join(f"- {t}" for t in existing_topics) if existing_topics else "(なし)"

    prompt = f"""以下はDiscordの「トラブルシューティング」チャンネルの履歴です。
ここから、conductorキーボードに関する有用なナレッジを抽出してください。

## 抽出ルール
- 実際に解決に至った具体的な情報のみ
- conductorキーボード固有の知識（一般的なPC知識は除外）
- 単発の質問→回答だけで判断せず、前後のメッセージ、追加質問、試した結果、最終的な解決報告まで含めて判断する
- 回答部分だけでなく、症状・試行錯誤・最終的に有効だった対処を簡潔にまとめる
- 1件のナレッジは1トピックに集中

## 既に登録済みのトピック（重複を避けること）
{existing_str}

## チャンネル履歴
{history_text}"""

    response = await claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "topic": {"type": "string", "description": "トピック名（短く）"},
                                    "correction": {"type": "string", "description": "正確な情報・解決方法"},
                                    "question": {"type": "string", "description": "元の質問の要約"},
                                },
                                "required": ["topic", "correction", "question"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["items"],
                    "additionalProperties": False,
                },
            }
        },
    )
    add_usage(response.usage)
    if response.stop_reason == "max_tokens":
        logger.warning("ナレッジ抽出がmax_tokensで途中切断されました")
    text = "".join(block.text for block in response.content if hasattr(block, "text"))
    try:
        return json.loads(text)["items"]
    except (json.JSONDecodeError, KeyError):
        logger.error("ナレッジ抽出のJSON解析に失敗")
        return []


# --- Studio Guide Site Tool ---
STUDIO_BASE_URL = "https://studio.plotoftheprototype.com"
STUDIO_TOOLS = [
    {
        "name": "fetch_studio_guide",
        "description": "Studio (plotoftheprototype.com) の公式ガイド・FW更新情報サイトからページを取得する。Notion ユーザーガイドと同等の最優先ソース。FWの最新版・ダウンロード・使い方の情報源。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "取得するページ（'guide'=全機能ガイド, 'firmware'=FWリリース一覧・最新版・各バージョンの更新内容とダウンロードURL）",
                    "enum": ["guide", "firmware"],
                    "default": "guide",
                }
            },
        },
    },
]


# SPA本体はJSレンダリングでHTMLが空殻のため、ビルド時に生成されるテキスト版 (*.txt) を取得する
STUDIO_TEXT_PAGES = {"": "guide.txt", "guide": "guide.txt", "firmware": "firmware.txt"}


async def fetch_studio_guide(path: str = "") -> str:
    page = path.strip().strip("/")
    page = STUDIO_TEXT_PAGES.get(page.removesuffix(".txt"), page)
    url = f"{STUDIO_BASE_URL}/{page}"
    try:
        resp = await http_client.get(url)
    except Exception as e:
        return f"Error: {e}"
    if resp.status_code != 200:
        return f"Error: {resp.status_code} for {url}"
    html = resp.text
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "\n", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    text = "\n".join(lines)
    if len(text) > 16000:
        text = text[:16000] + f"\n\n... (truncated, total {len(text)} chars)"
    return f"URL: {url}\n\n{text}" if text else f"(空のページ: {url})"


async def handle_tool_call(name: str, input_data: dict) -> str:
    logger.info("Tool call: %s(%s)", name, input_data)
    try:
        if name == "search_notion":
            return await notion_search(input_data["query"])
        elif name == "get_notion_page":
            return await notion_get_page(input_data["page_id"])
        elif name == "fetch_studio_guide":
            return await fetch_studio_guide(input_data.get("path", ""))
        elif name == "get_repo_tree":
            return await github_get_tree(input_data.get("path", ""))
        elif name == "get_file_contents":
            return await github_get_file(input_data["path"])
        elif name == "search_code":
            return await github_search_code(input_data["query"])
        return f"Unknown tool: {name}"
    except Exception as e:
        logger.exception("ツール %s の実行に失敗", name)
        return f"Error: ツール実行に失敗しました ({e})"


# --- Claude クライアント (async) ---
claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
logger.info("Claude SDK v%s / model=%s / advisor=%s (max_uses=%d) / repo=%s@%s / notion=%s",
            anthropic.__version__, CLAUDE_MODEL, ADVISOR_MODEL, ADVISOR_MAX_USES,
            GITHUB_REPO, GITHUB_BRANCH, "enabled" if NOTION_TOKEN else "disabled")

# --- Discord ボット ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = discord.Client(intents=intents)

# --- 優先チャンネルの履歴キャッシュ ---
priority_cache: dict[int, list[str]] = {}
priority_cache_last_id: dict[int, int] = {}
corrections_cache: list[dict] = []
studio_guide_cache: str = ""
_studio_guide_fetched_at: float = 0.0
_server_context_cache: dict[int, tuple[float, str]] = {}
_server_context_lock = asyncio.Lock()
answer_semaphore = asyncio.Semaphore(2)
_initialized = False

JST = timezone(timedelta(hours=9))
paused = False
pause_reason = ""
stats = {"answers": 0, "gate_evals": 0, "gate_pass": 0, "cache_read": 0}
daily_usage = {"date": "", "tokens": 0, "auto_pause_done": False}
health_state: dict = {}
_last_health_failures: set[str] = set()
last_seen_firmware_version = ""


def is_priority_channel(channel: discord.TextChannel) -> bool:
    name = channel.name.lower()
    return any(keyword in name for keyword in PRIORITY_CHANNEL_NAMES)


def format_message(msg: discord.Message, *, current: bool = False) -> str:
    timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M")
    author = getattr(msg.author, "display_name", msg.author.name)
    if msg.author.bot:
        author += " (bot)"
    marker = " <今回の発言>" if current else ""
    text = f"[{timestamp}] {author}{marker}: {msg.content}"
    if msg.attachments:
        files = ", ".join(a.filename for a in msg.attachments)
        text += f" [添付: {files}]"
    return text


def trim_context(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return "... (前略)\n" + text[-max_chars:]


async def fetch_referenced_message(message: discord.Message) -> discord.Message | None:
    if not message.reference or not message.reference.message_id:
        return None
    resolved = getattr(message.reference, "resolved", None)
    if isinstance(resolved, discord.Message):
        return resolved
    try:
        return await message.channel.fetch_message(message.reference.message_id)
    except (discord.Forbidden, discord.NotFound):
        return None
    except Exception:
        logger.exception("返信先メッセージの取得に失敗: #%s", getattr(message.channel, "name", "?"))
        return None


async def fetch_conversation_context(message: discord.Message) -> str:
    channel = message.channel
    after = message.created_at - timedelta(minutes=CONVERSATION_CONTEXT_MINUTES)
    recent_messages = []

    try:
        async for msg in channel.history(
            limit=CONVERSATION_CONTEXT_LIMIT,
            after=after,
            before=message.created_at,
            oldest_first=False,
        ):
            recent_messages.append(msg)
    except discord.Forbidden:
        logger.warning("チャンネル #%s の直近会話取得権限がありません", getattr(channel, "name", "?"))
    except Exception:
        logger.exception("チャンネル #%s の直近会話取得に失敗", getattr(channel, "name", "?"))

    recent_messages.reverse()
    referenced_message = await fetch_referenced_message(message)

    lines = [f"=== 現在のチャンネル: #{getattr(channel, 'name', '?')} ==="]
    if referenced_message and referenced_message.id not in {msg.id for msg in recent_messages}:
        lines.extend(["", "=== 返信先メッセージ ===", format_message(referenced_message)])

    lines.extend(["", "=== 直近の会話 ==="])
    lines.extend(format_message(msg) for msg in recent_messages)
    lines.append(format_message(message, current=True))

    return trim_context("\n".join(lines), MAX_CONVERSATION_CONTEXT_CHARS)


async def load_full_history(channel: discord.TextChannel) -> list[str]:
    # 消費側はサーバーコンテキストで直近100件・ナレッジ抽出で直近300件しか使わないため、
    # 直近 PRIORITY_HISTORY_LIMIT 件だけを新しい順に取得して古い順に並べ直す
    messages = []
    try:
        async for msg in channel.history(limit=PRIORITY_HISTORY_LIMIT, oldest_first=False):
            if msg.author == bot.user:
                continue
            messages.append(format_message(msg))
    except discord.Forbidden:
        logger.warning("チャンネル #%s の履歴取得権限がありません", channel.name)
    except Exception:
        logger.exception("チャンネル #%s の履歴取得に失敗", channel.name)
    messages.reverse()
    logger.info("  #%s: 直近 %d件 取得完了", channel.name, len(messages))
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
        cache = priority_cache[channel.id]
        cache.extend(new_messages)
        if len(cache) > PRIORITY_HISTORY_LIMIT:
            del cache[:-PRIORITY_HISTORY_LIMIT]
        priority_cache_last_id[channel.id] = last_id
        logger.info("#%s: キャッシュに %d件 追加 (合計 %d件)",
                    channel.name, len(new_messages), len(cache))


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

    # 優先チャンネルを先に処理してコンテキスト冒頭に確実に含める
    priority_channels = [ch for ch in guild.text_channels if is_priority_channel(ch)]
    other_channels = [ch for ch in guild.text_channels if not is_priority_channel(ch)]
    ordered_channels = priority_channels + other_channels

    for channel in ordered_channels:
        remaining = MAX_CONTEXT_CHARS - total_chars
        if remaining <= 0:
            break
        permissions = channel.permissions_for(guild.me)
        if not permissions.read_messages or not permissions.read_message_history:
            continue
        if is_priority_channel(channel):
            await update_priority_cache(channel)
            history = priority_cache.get(channel.id, [])
            recent = history[-100:]
            header = f"=== #{channel.name} (直近{len(recent)}件 / 保持{len(history)}件) ==="
        else:
            recent = await fetch_channel_history(channel, limit=HISTORY_FETCH_LIMIT)
            header = f"=== #{channel.name} ==="
        if not recent:
            continue
        # 枠を超える場合は古いメッセージから削り、先頭の優先チャンネルを末尾切り詰めで失わないようにする
        while recent and len(header) + sum(len(m) + 1 for m in recent) > remaining:
            recent.pop(0)
        if not recent:
            break
        all_history.append(header)
        all_history.extend(recent)
        all_history.append("")
        total_chars += len(header) + sum(len(m) + 1 for m in recent)

    return "\n".join(all_history)


async def get_server_context(guild: discord.Guild) -> str:
    # SERVER_CONTEXT_TTL_SECONDS の間はDiscordから再取得せずキャッシュを返す。
    # ロックで再構築の並行実行(update_priority_cacheの競合含む)も防ぐ。
    cached = _server_context_cache.get(guild.id)
    if cached and time.monotonic() - cached[0] < SERVER_CONTEXT_TTL_SECONDS:
        return cached[1]
    async with _server_context_lock:
        cached = _server_context_cache.get(guild.id)
        if cached and time.monotonic() - cached[0] < SERVER_CONTEXT_TTL_SECONDS:
            return cached[1]
        context = await fetch_server_context(guild)
        _server_context_cache[guild.id] = (time.monotonic(), context)
        return context


async def refresh_studio_guide(force: bool = False):
    global studio_guide_cache, _studio_guide_fetched_at
    if not force and time.monotonic() - _studio_guide_fetched_at < STUDIO_GUIDE_REFRESH_SECONDS:
        return
    _studio_guide_fetched_at = time.monotonic()  # 失敗時も次の間隔まで再試行しない
    guide = await fetch_studio_guide("guide")
    if guide.startswith("Error:"):
        logger.warning("Studioガイド取得失敗 (既存キャッシュを維持): %s", guide[:100])
        return
    if len(guide) < 500:
        # SPAフォールバック(index.htmlの殻)を掴むと中身のない短文になる
        logger.warning("Studioガイドが短すぎます (%d文字, SPAフォールバックの疑い)。既存キャッシュを維持", len(guide))
        return
    studio_guide_cache = guide
    logger.info("Studioガイド更新 (%d文字)", len(studio_guide_cache))


def _roll_daily() -> None:
    global paused, pause_reason
    today = datetime.now(JST).date().isoformat()
    if today != daily_usage["date"]:
        daily_usage["date"] = today
        daily_usage["tokens"] = 0
        daily_usage["auto_pause_done"] = False
        if paused and pause_reason == "daily_limit":
            paused = False
            pause_reason = ""
            logger.info("日次ロールオーバーにより自動回答を再開")


def add_usage(usage) -> None:
    global paused, pause_reason
    if usage is None:
        return
    _roll_daily()
    input_tokens = usage.input_tokens or 0
    output_tokens = usage.output_tokens or 0
    cache_creation = usage.cache_creation_input_tokens or 0
    cache_read = usage.cache_read_input_tokens or 0
    stats["cache_read"] += cache_read
    daily_usage["tokens"] += input_tokens + output_tokens + cache_creation
    if (
        DAILY_TOKEN_LIMIT > 0
        and not paused
        and not daily_usage["auto_pause_done"]
        and daily_usage["tokens"] > DAILY_TOKEN_LIMIT
    ):
        paused = True
        pause_reason = "daily_limit"
        daily_usage["auto_pause_done"] = True
        logger.warning("日次トークン上限超過のため自動回答を停止 (%d / %d)",
                       daily_usage["tokens"], DAILY_TOKEN_LIMIT)
        asyncio.create_task(dm_owner(
            f"🛑 日次トークン上限を超過したため自動回答を一時停止しました\n"
            f"daily_tokens: {daily_usage['tokens']:,} / {DAILY_TOKEN_LIMIT:,}\n"
            f"`!resume` で再開できます (翌日0時JSTに自動解除)"
        ))


async def dm_owner(text: str) -> None:
    if not bot.guilds:
        logger.warning("guildが無いためオーナーDMを送れません")
        return
    try:
        guild = bot.guilds[0]
        owner = guild.owner or await bot.fetch_user(guild.owner_id)
        await owner.send(text)
    except Exception:
        logger.exception("オーナーDMの送信に失敗")


def _parse_firmware_version(text: str) -> str:
    m = re.search(r"最新バージョン:\s*(v[\d.]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"^## (v[\d.]+)", text, re.MULTILINE)
    if m:
        return m.group(1)
    return ""


async def run_health_check(*, notify: bool, force_guide: bool = False) -> dict:
    global last_seen_firmware_version, corrections_cache, _last_health_failures
    await refresh_studio_guide(force=force_guide)
    guide_chars = len(studio_guide_cache)

    fw_text = await fetch_studio_guide("firmware")
    fw_error = fw_text.startswith("Error:") or fw_text.startswith("(空")
    firmware_chars = 0 if fw_error else len(fw_text)
    firmware_version = "" if fw_error else _parse_firmware_version(fw_text)

    version_changed = False
    if firmware_version and firmware_version != last_seen_firmware_version:
        if last_seen_firmware_version:
            version_changed = True
            logger.info("FWバージョン変化検知: %s → %s", last_seen_firmware_version, firmware_version)
        else:
            logger.info("FWバージョン初回記録: %s", firmware_version)
        last_seen_firmware_version = firmware_version

    loaded = await load_corrections()
    if loaded is not None:
        corrections_cache = loaded
    notion_corrections = len(corrections_cache)

    failures: set[str] = set()
    if guide_chars < 500:
        failures.add("guide_chars_low")
    if fw_error:
        failures.add("firmware_fetch_failed")
    if not fw_error and firmware_chars < 500:
        failures.add("firmware_chars_low")
    if not fw_error and not firmware_version:
        failures.add("firmware_version_unparsed")
    if NOTION_TOKEN and loaded is None:
        failures.add("notion_fetch_failed")

    health_state["guide_chars"] = guide_chars
    health_state["firmware_version"] = firmware_version
    health_state["firmware_chars"] = firmware_chars
    health_state["notion_corrections"] = notion_corrections
    health_state["checked_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    if notify:
        new_failures = failures - _last_health_failures
        if new_failures:
            await dm_owner(
                "⚠️ ヘルスチェック異常\n"
                f"失敗項目: {', '.join(sorted(new_failures))}\n"
                f"guide_chars: {guide_chars}\n"
                f"firmware_version: {firmware_version or '(取得失敗)'}\n"
                f"firmware_chars: {firmware_chars}\n"
                f"notion_corrections: {notion_corrections}"
            )
        recovered = _last_health_failures - failures
        if recovered:
            logger.info("ヘルスチェック回復: %s", ", ".join(sorted(recovered)))
        _last_health_failures = failures

    return {
        "guide_chars": guide_chars,
        "firmware_version": firmware_version,
        "firmware_chars": firmware_chars,
        "notion_corrections": notion_corrections,
        "checked_at": health_state["checked_at"],
        "version_changed": version_changed,
        "failures": failures,
    }


async def health_check_loop():
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)
        try:
            await run_health_check(notify=True)
        except Exception:
            logger.exception("定期ヘルスチェックに失敗")


async def should_respond(message: discord.Message, conversation_context: str) -> bool:
    # メンション/ボットへの返信は無条件で回答。それ以外はHaikuで要返答判定する
    if bot.user in message.mentions:
        return True
    if message.reference:
        referenced = await fetch_referenced_message(message)
        if referenced and referenced.author == bot.user:
            return True
    try:
        gate = await claude.messages.create(
            model=GATE_MODEL,
            max_tokens=4,
            system=(
                "Discordサポートチャンネルの発言が、サポートボットの返答を必要とするか判定する。"
                "質問・トラブル報告・直前の会話の続きの相談(「試しました」「同じ症状です」など)なら yes、"
                "お礼・完了報告・雑談・相槌だけなら no とだけ出力する。判断に迷ったら yes。"
            ),
            messages=[{
                "role": "user",
                "content": f"直近の会話:\n{conversation_context[-1500:]}\n\n判定対象の発言: {message.content}",
            }],
        )
        verdict = "".join(block.text for block in gate.content if hasattr(block, "text")).strip().lower()
        logger.info("要返答判定: %s (%s)", verdict, message.content[:50])
        add_usage(gate.usage)
        stats["gate_evals"] += 1
        is_yes = not verdict.startswith("no")
        if is_yes:
            stats["gate_pass"] += 1
        return is_yes
    except Exception:
        logger.exception("要返答判定に失敗。回答にフォールバックします")
        return True


def _move_cache_marker(messages: list) -> None:
    # 直近メッセージの末尾ブロックにcache_controlを移し、messages内のbreakpointを常に1個に保つ
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    last = messages[-1]
    if isinstance(last, dict) and isinstance(last.get("content"), list):
        blocks = last["content"]
        if blocks and isinstance(blocks[-1], dict):
            blocks[-1]["cache_control"] = {"type": "ephemeral"}


async def generate_answer(question: str, conversation_context: str, server_context: str) -> str:
    """Agentic loop: Claude can call GitHub/Notion/Studio tools to fetch info."""
    corrections_block = ""
    if corrections_cache:
        lines = []
        for c in corrections_cache:
            entry = f"- **{c['topic']}**: {c['correction']}"
            if c.get("question"):
                entry += f"（元の質問: {c['question'][:100]}）"
            lines.append(entry)
        corrections_block = (
            "\n## 訂正済み情報（最優先 — 他のすべてのソースより優先）\n"
            "以下はサーバーオーナーが訂正した正確な情報です。これらのトピックに関する質問には必ずこの訂正内容に基づいて回答してください。\n\n"
            + "\n".join(lines)
            + "\n"
        )

    stable_system = f"""{SYSTEM_PROMPT}
{corrections_block}
## 回答の優先順位（厳守）
1. **最優先（同列）: Notionユーザーガイド と Studio公式サイト** — どちらも公式情報源。質問の内容に応じて以下のツールで取得してください:
   - `search_notion` → `get_notion_page`: Notionに書かれた詳細ガイド
   - `fetch_studio_guide`: Studioサイト（{STUDIO_BASE_URL}）。FW更新・ダウンロード・基本ガイドはここを最初に確認
   - 関連する話題なら **両方を確認** して総合的に回答してください。
2. **第2優先: Discordサーバーの履歴** — 公式情報になければ、過去のやり取りやトラブルシューティングの実例を参照してください。特に「トラブルシューティング」チャンネルには過去の全履歴が含まれています。
3. **第3優先: GitHubリポジトリ** — 上記で十分な情報が得られない場合のみ、GitHubツールを使ってリポジトリ（{GITHUB_REPO}@{GITHUB_BRANCH}）のソースコード・設定ファイルを参照してください。
4. **一般知識** — 上記すべてに該当しない場合のみ、一般的な知識で回答してください。

## 重要
- 公式情報源（Notion / Studioサイト）が最も信頼できます。GitHubのコードと矛盾する場合は公式を優先してください。
- 回答にはどのソースを根拠にしたか明記してください。
- 質問文だけを孤立して読まず、現在のDiscord会話文脈を最優先で解釈してください。
- 「それ」「同じです」「試しました」などの続きの発言は、返信先・直前の会話から主語や未解決点を補って返答してください。
- 直前の回答を繰り返さず、会話が進むように次に確認・実行すべきことを具体的に返してください。
- 文脈を見ても判断できない場合は、断定せずに必要な確認事項を1つか2つだけ聞いてください。

## ファームウェア（FW）の更新情報・ダウンロードについて
- FWの最新版・更新情報・ダウンロード方法に関する質問を受けたら、必ず以下のURLを案内してください:
  {STUDIO_BASE_URL}/firmware
- 「FW更新したい」「最新FWはどこ？」「ダウンロード先を教えて」などの問い合わせはこのページに誘導してください。
- 必要なら `fetch_studio_guide(path="firmware")` で最新版の内容を取得してから回答してください。

## Studio公式ガイド（常に参照すること）
--- Studio Guide ({STUDIO_BASE_URL}/guide) ---
{studio_guide_cache}
--- ガイドここまで ---"""

    history_system = f"""## Discordサーバー履歴
--- サーバー履歴 ---
{server_context}
--- 履歴ここまで ---"""

    # prompt caching: tools+安定部 / サーバー履歴 / 直近メッセージ の3breakpointで
    # agentic loopのiteration間と質問間のprefix再利用を効かせる
    system = [
        {"type": "text", "text": stable_system, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": history_system, "cache_control": {"type": "ephemeral"}},
    ]

    all_tools = NOTION_TOOLS + STUDIO_TOOLS + GITHUB_TOOLS + [ADVISOR_TOOL]
    user_prompt = f"""以下のDiscord会話文脈を読んだうえで、最後の「今回の発言」に返答してください。

## 現在のDiscord会話文脈
{conversation_context}

## 今回の発言本文
{question}"""
    messages = [{"role": "user", "content": [{"type": "text", "text": user_prompt}]}]
    max_iterations = 8

    for i in range(max_iterations):
        _move_cache_marker(messages)
        response = await claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8192,
            system=system,
            messages=messages,
            tools=all_tools,
            thinking={"type": "adaptive"},
            extra_headers=ADVISOR_HEADERS,
        )
        add_usage(response.usage)
        usage = response.usage
        logger.info(
            "Claude応答 [iter=%d]: stop_reason=%s, blocks=%d, in=%d, cache_read=%d, cache_write=%d, out=%d",
            i, response.stop_reason, len(response.content),
            usage.input_tokens, usage.cache_read_input_tokens or 0,
            usage.cache_creation_input_tokens or 0, usage.output_tokens,
        )

        if response.stop_reason == "pause_turn":
            # server-sideツール(advisor)の一時停止。そのまま再送して続行する
            messages.append({"role": "assistant", "content": response.content})
            continue

        if response.stop_reason != "tool_use":
            if response.stop_reason == "max_tokens":
                logger.warning("回答がmax_tokensで途中切断 (out=%d)", usage.output_tokens)
            return "".join(
                block.text for block in response.content if hasattr(block, "text")
            )

        tool_blocks = [block for block in response.content if block.type == "tool_use"]
        if not tool_blocks:
            return "".join(
                block.text for block in response.content if hasattr(block, "text")
            )
        results = await asyncio.gather(
            *(handle_tool_call(block.name, block.input) for block in tool_blocks)
        )
        tool_results = [
            {"type": "tool_result", "tool_use_id": block.id, "content": result}
            for block, result in zip(tool_blocks, results)
        ]

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    # ループ上限到達: ツール使用を打ち切り、得た情報だけで最終回答をまとめさせる
    logger.warning("max_iterations (%d) 到達。最終回答を生成します", max_iterations)
    messages.append({
        "role": "user",
        "content": [{"type": "text", "text": "ツール使用はここまでです。これまでに得た情報だけで最終回答をまとめてください。"}],
    })
    _move_cache_marker(messages)
    response = await claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8192,
        system=system,
        messages=messages,
        tools=all_tools,
        tool_choice={"type": "none"},
        thinking={"type": "adaptive"},
        extra_headers=ADVISOR_HEADERS,
    )
    add_usage(response.usage)
    return "".join(
        block.text for block in response.content if hasattr(block, "text")
    )


@bot.event
async def on_ready():
    global _initialized, corrections_cache
    logger.info("ボット起動: %s (ID: %s)", bot.user.name, bot.user.id)
    if _initialized:
        logger.info("再接続のため初期化をスキップ")
        return
    _initialized = True

    if TARGET_CHANNEL_IDS:
        logger.info("監視チャンネル: %s", TARGET_CHANNEL_IDS)
    else:
        logger.warning("TARGET_CHANNEL_IDS が未設定です。すべてのチャンネルで応答します。")

    logger.info("優先チャンネルの履歴を読み込み中...")
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if is_priority_channel(channel):
                permissions = channel.permissions_for(guild.me)
                if not permissions.read_messages or not permissions.read_message_history:
                    logger.warning("  #%s: 権限不足でスキップ", channel.name)
                    continue
                logger.info("  #%s の履歴を取得開始...", channel.name)
                history = await load_full_history(channel)
                priority_cache[channel.id] = history
                # 空チャンネルでも差分更新が動くよう、チャンネル作成時点のsnowflakeを起点にする
                last_id = channel.id
                try:
                    async for msg in channel.history(limit=1):
                        last_id = msg.id
                        break
                except Exception:
                    pass
                priority_cache_last_id[channel.id] = last_id
    logger.info("優先チャンネルの読み込み完了 (合計 %d件)",
                sum(len(v) for v in priority_cache.values()))

    loaded = await load_corrections()
    corrections_cache = loaded if loaded is not None else [dict(item) for item in BUILTIN_CORRECTIONS]

    logger.info("Studio ガイドページを取得中...")
    await refresh_studio_guide(force=True)

    await run_health_check(notify=True)
    asyncio.create_task(health_check_loop())

    logger.info("ボット準備完了！")


@bot.event
async def on_message(message: discord.Message):
    global corrections_cache, paused, pause_reason
    if message.author == bot.user:
        return
    if message.author.bot:
        return
    if message.guild is None:
        return

    logger.info("MSG [#%s] ch_id=%s %s: %s",
                getattr(message.channel, 'name', '?'), message.channel.id,
                message.author.name, message.content[:80])

    content = message.content.strip()
    if content in ("!pause", "!resume", "!status", "!release_check"):
        if message.author.id != message.guild.owner_id:
            await message.reply("このコマンドはサーバーオーナーのみ使用できます。")
            return
        if content == "!pause":
            paused = True
            pause_reason = "manual"
            await message.reply("⏸️ 自動回答を一時停止しました。`!resume` で再開します。")
        elif content == "!resume":
            paused = False
            pause_reason = ""
            await message.reply("▶️ 自動回答を再開しました。")
        elif content == "!status":
            _roll_daily()
            lines = []
            if paused:
                lines.append(f"paused: True ({pause_reason})")
            else:
                lines.append("paused: False")
            lines.append(f"daily_tokens: {daily_usage['tokens']:,} / {DAILY_TOKEN_LIMIT:,}")
            lines.append(f"answers: {stats['answers']}")
            if stats["gate_evals"] == 0:
                lines.append("gate_pass_rate: n/a")
            else:
                rate = stats["gate_pass"] / stats["gate_evals"] * 100
                lines.append(f"gate_pass_rate: {stats['gate_pass']}/{stats['gate_evals']} ({rate:.1f}%)")
            lines.append(f"cache_read: {stats['cache_read']:,}")
            if health_state:
                lines.append(f"guide_chars: {health_state['guide_chars']}")
                lines.append(f"firmware_version: {health_state['firmware_version']}")
                lines.append(f"firmware_chars: {health_state['firmware_chars']}")
                lines.append(f"notion_corrections: {health_state['notion_corrections']}")
                lines.append(f"last_health_check: {health_state['checked_at']}")
            else:
                lines.append("guide_chars: (未実施)")
                lines.append("firmware_version: (未実施)")
                lines.append("firmware_chars: (未実施)")
                lines.append("notion_corrections: (未実施)")
                lines.append("last_health_check: (未実施)")
            await message.reply("```\n" + "\n".join(lines) + "\n```")
        elif content == "!release_check":
            old_version = last_seen_firmware_version
            async with message.channel.typing():
                result = await run_health_check(notify=False, force_guide=True)
            if result["version_changed"]:
                fw_line = f"firmware_version: 🆕 {old_version} → {result['firmware_version']}"
            else:
                fw_line = f"firmware_version: {result['firmware_version']} (変化なし)"
            reply_lines = [
                "🔄 リリース確認完了",
                fw_line,
                f"firmware_chars: {result['firmware_chars']}",
                f"guide_chars: {result['guide_chars']}",
                f"notion_corrections: {result['notion_corrections']}",
            ]
            if result["failures"]:
                reply_lines.append(f"⚠️ 異常: {', '.join(sorted(result['failures']))}")
            await message.reply("\n".join(reply_lines))
        return

    if message.content.startswith("!訂正"):
        if message.author.id != message.guild.owner_id:
            await message.reply("このコマンドはサーバーオーナーのみ使用できます。")
            return

        correction_text = message.content[len("!訂正"):].strip()
        if not correction_text:
            await message.reply("使い方: ボットの誤回答に返信して `!訂正 正しい情報` と送信してください。")
            return

        if not message.reference:
            await message.reply("ボットの回答メッセージに返信する形で使ってください。")
            return

        try:
            bot_msg = await message.channel.fetch_message(message.reference.message_id)
        except discord.NotFound:
            await message.reply("返信先のメッセージが見つかりません。")
            return

        if bot_msg.author != bot.user:
            await message.reply("ボットの回答メッセージに返信してください。")
            return

        original_question = ""
        if bot_msg.reference:
            try:
                orig_msg = await message.channel.fetch_message(bot_msg.reference.message_id)
                original_question = orig_msg.content
            except Exception:
                pass

        topic = _extract_topic(correction_text)
        success = await save_correction(
            topic=topic,
            correction=correction_text,
            question=original_question,
            wrong_answer=bot_msg.content[:2000],
        )

        if success:
            loaded = await load_corrections()
            if loaded is not None:
                corrections_cache = loaded
            reply_lines = [
                "✅ 訂正を記録しました。\n",
                f"**トピック:** {topic}",
                f"**訂正内容:** {correction_text[:500]}",
            ]
            if original_question:
                reply_lines.append(f"**元の質問:** {original_question[:200]}")
            reply_lines.append(f"\n今後このトピックに関する質問ではこの情報を優先します。（訂正データ: {len(corrections_cache)}件）")
            await message.reply("\n".join(reply_lines))
        else:
            await message.reply("訂正の保存に失敗しました。Notion設定を確認してください。")
        return

    if message.content.startswith("!ナレッジ抽出"):
        if message.author.id != message.guild.owner_id:
            await message.reply("このコマンドはサーバーオーナーのみ使用できます。")
            return

        # ゲートでスキップされた完了報告等も拾えるよう、抽出前に差分を取り込む
        async with _server_context_lock:
            for ch_id in list(priority_cache.keys()):
                channel = bot.get_channel(ch_id)
                if isinstance(channel, discord.TextChannel):
                    await update_priority_cache(channel)

        all_history = []
        for ch_id, history in priority_cache.items():
            all_history.extend(history)

        if not all_history:
            await message.reply("トラブルシューティングチャンネルの履歴がありません。")
            return

        existing_topics = [c["topic"] for c in corrections_cache]
        await message.reply(f"ナレッジ抽出を開始します（直近 {min(len(all_history), 300)}件を分析中...）")

        async with message.channel.typing():
            knowledge = await extract_knowledge(all_history, existing_topics)

        if not knowledge:
            await message.reply("抽出できるナレッジが見つかりませんでした。")
            return

        saved = 0
        for item in knowledge:
            success = await save_correction(
                topic=item.get("topic", "")[:200],
                correction=item.get("correction", ""),
                question=item.get("question", ""),
                wrong_answer="",
            )
            if success:
                saved += 1

        loaded = await load_corrections()
        if loaded is not None:
            corrections_cache = loaded

        topics_list = "\n".join(f"- {item.get('topic', '')}" for item in knowledge[:10])
        await message.reply(
            f"ナレッジ抽出完了: {len(knowledge)}件抽出、{saved}件保存\n"
            f"（現在の訂正データ: {len(corrections_cache)}件）\n\n"
            f"**抽出されたトピック:**\n{topics_list}"
        )
        return

    if TARGET_CHANNEL_IDS and message.channel.id not in TARGET_CHANNEL_IDS:
        return

    # paused中はadd_usage経由のロールオーバーが走らないため、ここで日次解除を判定する
    _roll_daily()
    if paused:
        logger.info("一時停止中のためスキップ [#%s] %s", message.channel.name, message.author.name)
        return

    conversation_context = await fetch_conversation_context(message)
    if not await should_respond(message, conversation_context):
        logger.info("要返答でないと判定したためスキップ [#%s] %s: %s",
                    message.channel.name, message.author.name, message.content[:50])
        return

    logger.info("応答開始 [#%s] %s: %s",
                message.channel.name, message.author.name, message.content[:80])

    async with message.channel.typing():
        try:
            await refresh_studio_guide()
            server_context = await get_server_context(message.guild)
            logger.info("コンテキスト取得完了 (会話%d文字 / サーバー履歴%d文字)",
                        len(conversation_context), len(server_context))
            async with answer_semaphore:
                answer = await generate_answer(message.content, conversation_context, server_context)
        except Exception:
            logger.exception("回答生成に失敗しました")
            await message.reply("申し訳ありません。回答の生成中にエラーが発生しました。")
            return

    if not answer.strip():
        logger.warning("空の回答が生成されたためフォールバック文を返します")
        answer = "申し訳ありません。今回はうまく回答をまとめられませんでした。質問の内容を少し変えてもう一度試してください。"

    try:
        if len(answer) > 2000:
            for i in range(0, len(answer), 2000):
                await message.reply(answer[i : i + 2000])
        else:
            await message.reply(answer)
        stats["answers"] += 1
    except discord.HTTPException:
        logger.exception("回答の送信に失敗しました")


def main():
    if not DISCORD_BOT_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN が設定されていません。")
    if not ANTHROPIC_API_KEY:
        raise SystemExit("ANTHROPIC_API_KEY が設定されていません。")
    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
