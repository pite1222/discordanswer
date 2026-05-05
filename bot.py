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
MAX_CONTEXT_CHARS = int(os.environ.get("MAX_CONTEXT_CHARS", "80000"))

# --- Conductor FW リファレンス ---
FW_REFERENCE = """
## Conductor キーボード ファームウェア情報
リポジトリ: https://github.com/pite1222/conductor (branch: zephyr-4.1)

### ハードウェア構成
- MCU: Seeeduino Xiao BLE (nRF52840)
- 分割キーボード: 右手(monokey_R)=セントラル+トラックボール、左手(monokey_L)=ペリフェラル
- トラックボール: PMW3610 (SPI接続、CPI: 800)
- RGB LED: WS2812B (rgbled_adapter シールド経由)
- バッテリー監視: LED表示対応
- 充電検出: GPIO P0.17
- レイアウト: 40キー (4行×10列、スプリットスペースバー)

### キーマップレイヤー (7レイヤー)
| レイヤー | 名前 | 用途 |
|----------|------|------|
| 0 | default | QWERTY配列 + 修飾キー |
| 1 | FUNCTION | 記号・特殊文字・スクリーンショット (Space長押し) |
| 2 | NUM | 数字行(F1-F12) + テンキー (Backspace長押し) |
| 3 | ARROW | 矢印キー + Vim風ナビゲーション + メディア制御 (Enter長押し) |
| 4 | MOUSE | マウスボタン (トラックボール動作で自動切替: AML) |
| 5 | SCROLL | スクロール (トラックボールXY→スクロール変換) |
| 6 | Bluetooth | BT接続(5台) + ブートローダー + リセット |

### 主な機能
- Mod-tap: Z=左Shift長押し、/=右Shift長押し
- Layer-tap: Space=Layer1、Backspace=Layer2、Enter=Layer3
- コンボ: A+S → A (51msタイムアウト)
- エンコーダ: デフォルト=PageUp/Down、矢印レイヤー=Ctrl+PgUp/PgDn
- Auto Mouse Layer (AML): トラックボール動作で自動的にLayer4起動、5秒タイムアウト
- スクロールレイヤー: Layer5でトラックボール→スクロール変換 (1/8スケール)
- ZMK Studio: Chrome経由でリモートキーマップ編集可能

### RGB LED表示
- レイヤー色: Layer0=消灯、Layer1=色4、Layer2=色2、Layer3=色3
- バッテリー: 50%以上=緑、10-50%=黄、10%未満=赤、5%=点滅(4秒間隔)
- BLE接続中: 点滅(800ms間隔)

### キーマップ変更方法
1. ファイル: config/boards/shields/monokey/monokey.keymap
2. ZMKデバイスツリー形式で記述
3. 主なバインディング: &kp(通常キー)、&mt(モッドタップ)、&lt(レイヤータップ)、&mkp(マウスクリック)
4. ZMK Studioでリモート編集も可能

### ビルド・フラッシュ手順
1. GitHub Actionsで自動ビルド (pushで発火)
2. 出力: zmk.uf2 ファイル
3. フラッシュ: Xiao BLEのリセットボタン2回タップ → USBドライブにzmk.uf2をドラッグ&ドロップ
4. ビルドターゲット: monokey_R+rgbled_adapter、monokey_L+rgbled_adapter、settings_reset

### トラブルシューティング
| 症状 | 原因 | 解決方法 |
|------|------|----------|
| トラックボールが反応しない/暴走 | AMLタイムアウト or CPI設定 | input-processorsタイムアウト調整 or CPI値変更 |
| キーが反応しない | NFCピン未解放 | nfct-pins-as-gpios確認 |
| LEDのレイヤー色が出ない | RGBウィジェット未有効 | CONFIG_RGBLED_WIDGET=y確認 |
| BLE接続が不安定 | 接続間隔の不一致 | BT_PERIPHERAL_PREF_MIN_INT=6確認 |
| 左右が繋がらない | セントラル/ペリフェラル設定 | 右=central、左=peripheral確認、settings_resetフラッシュ |
| 設定がおかしくなった | 設定破損 | settings_resetビルドをフラッシュ後、通常FWを再フラッシュ |

### BLE設定
- デバイス名: "conductor"
- 最大接続数: 5台
- ペアリング記憶: 7台
- 拡張アドバタイジング対応 (低消費電力)
- 接続間隔: 7.5ms固定

### 依存関係
- ZMK: pite1222/zmk feat/pointing-studio ブランチ
- PMW3610ドライバ: badjeff/zmk-pmw3610-driver
- RGBウィジェット: zmk-rgbled-widget
- 充電インジケータ: zmk-feature-charge-indicator
"""

# Notion settings
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")

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


# --- Notion API helpers ---
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


def notion_search(query: str) -> str:
    if not NOTION_TOKEN:
        return "Error: NOTION_TOKEN が設定されていません"
    url = "https://api.notion.com/v1/search"
    body = {"query": query, "page_size": 10}
    resp = _http.post(url, headers=_notion_headers(), json=body)
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


def notion_get_page(page_id: str) -> str:
    if not NOTION_TOKEN:
        return "Error: NOTION_TOKEN が設定されていません"
    all_blocks = []
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    params = {"page_size": 100}
    while True:
        resp = _http.get(url, headers=_notion_headers(), params=params)
        if resp.status_code != 200:
            return f"Error: {resp.status_code} {resp.text[:200]}"
        data = resp.json()
        all_blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        params["start_cursor"] = data["next_cursor"]

    # Fetch children for blocks that have them (toggles, etc.)
    expanded = []
    for block in all_blocks:
        expanded.append(block)
        if block.get("has_children") and block["type"] not in ("child_page", "child_database"):
            child_url = f"https://api.notion.com/v1/blocks/{block['id']}/children"
            child_resp = _http.get(child_url, headers=_notion_headers(), params={"page_size": 100})
            if child_resp.status_code == 200:
                children = child_resp.json().get("results", [])
                expanded.extend(children)

    content = _blocks_to_text(expanded)
    if len(content) > 10000:
        content = content[:10000] + f"\n\n... (truncated, total {len(content)} chars)"
    return content if content else "(ページの内容が空です)"


def handle_tool_call(name: str, input_data: dict) -> str:
    logger.info("Tool call: %s(%s)", name, input_data)
    if name == "search_notion":
        return notion_search(input_data["query"])
    elif name == "get_notion_page":
        return notion_get_page(input_data["page_id"])
    elif name == "get_repo_tree":
        return github_get_tree(input_data.get("path", ""))
    elif name == "get_file_contents":
        return github_get_file(input_data["path"])
    elif name == "search_code":
        return github_search_code(input_data["query"])
    return f"Unknown tool: {name}"


# --- Claude クライアント ---
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
logger.info("Claude SDK v%s / model=%s / advisor=%s (max_uses=%d) / repo=%s@%s / notion=%s",
            anthropic.__version__, CLAUDE_MODEL, ADVISOR_MODEL, ADVISOR_MAX_USES,
            GITHUB_REPO, GITHUB_BRANCH, "enabled" if NOTION_TOKEN else "disabled")

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

## 回答の優先順位（厳守）
1. **最優先: Notionユーザーガイド** — 質問を受けたら、まずsearch_notionで関連ページを検索し、get_notion_pageで内容を取得してください。ユーザーガイドに書かれている情報を最も信頼できるソースとして回答してください。必ず最初にNotionを検索してください。
2. **第2優先: Discordサーバーの履歴** — ユーザーガイドに情報がない場合、過去のやり取りやトラブルシューティングの実例を参照してください。特に「トラブルシューティング」チャンネルには過去の全履歴が含まれています。
3. **第3優先: GitHubリポジトリ** — 上記で十分な情報が得られない場合のみ、GitHubツールを使ってリポジトリ（{GITHUB_REPO}@{GITHUB_BRANCH}）のソースコード・設定ファイルを参照してください。
4. **一般知識** — 上記すべてに該当しない場合のみ、一般的な知識で回答してください。

## 重要
- Notionのユーザーガイドが公式ドキュメントです。GitHubのコードと矛盾する場合はNotionを優先してください。
- 回答にはどのソースを根拠にしたか明記してください。

## Discordサーバー履歴
--- サーバー履歴 ---
{server_context}
--- 履歴ここまで ---"""

    all_tools = NOTION_TOOLS + GITHUB_TOOLS + [ADVISOR_TOOL]
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
