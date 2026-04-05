# Discord 自動回答ボット

特定のDiscordチャンネルで質問を検出し、Claude APIを使って自動で回答するボットです。

## セットアップ

### 1. 依存パッケージのインストール

```bash
pip install -r requirements.txt
```

### 2. 環境変数の設定

`.env.example` をコピーして `.env` を作成し、各値を設定してください。

```bash
cp .env.example .env
```

| 変数名 | 説明 |
|---|---|
| `DISCORD_BOT_TOKEN` | Discord Botトークン |
| `ANTHROPIC_API_KEY` | Anthropic APIキー |
| `TARGET_CHANNEL_IDS` | 監視対象チャンネルID（カンマ区切り） |
| `CLAUDE_MODEL` | 使用するClaudeモデル（デフォルト: `claude-sonnet-4-20250514`） |
| `SYSTEM_PROMPT` | ボットの振る舞いを制御するプロンプト |

### 3. Discord Bot の作成

1. [Discord Developer Portal](https://discord.com/developers/applications) でアプリケーションを作成
2. Bot タブでトークンを取得
3. **Privileged Gateway Intents** で **Message Content Intent** を有効化
4. OAuth2 > URL Generator で `bot` スコープ + `Send Messages` / `Read Message History` 権限を選択し、サーバーに招待

### 4. 起動

```bash
python bot.py
```

## 動作

- 指定チャンネルのメッセージを監視
- `?`、`？`、`教えて`、`どう` などの質問マーカーを含むメッセージを検出
- Claude API で回答を生成し、リプライで返信
