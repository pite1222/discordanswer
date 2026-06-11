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
| `CONVERSATION_CONTEXT_LIMIT` | 回答時に読む同一チャンネルの直近メッセージ数（デフォルト: `20`） |
| `CONVERSATION_CONTEXT_MINUTES` | 回答時に読む直近会話の時間幅（デフォルト: `180`） |
| `MAX_CONVERSATION_CONTEXT_CHARS` | 現在の会話文脈の最大文字数（デフォルト: `6000`） |
| `NOTION_TOKEN` | Notion Internal Integration のシークレット |
| `NOTION_CORRECTIONS_DB_ID` | 訂正ナレッジDBのID |

### Notion 連携

1. [Notion Integrations](https://www.notion.so/my-integrations) で Internal Integration を作成し、`NOTION_TOKEN` にシークレットを設定
2. ユーザーガイドのページと訂正ナレッジDBを Integration に接続
3. 訂正ナレッジDBに以下のプロパティを作成

| プロパティ | 型 |
|---|---|
| `Topic` | Title |
| `Correction` | Text |
| `Question` | Text |
| `WrongAnswer` | Text |

`!訂正` と `!ナレッジ抽出` は、このDBに保存されます。ユーザーガイド検索は、Integration に接続されたNotionページを検索します。

### 3. Discord Bot の作成

1. [Discord Developer Portal](https://discord.com/developers/applications) でアプリケーションを作成
2. Bot タブでトークンを取得
3. **Privileged Gateway Intents** で **Message Content Intent** を有効化
4. OAuth2 > URL Generator で `bot` スコープ + `Send Messages` / `Read Message History` 権限を選択し、サーバーに招待

### 4. 起動

```bash
python bot.py
```

## オーナーコマンド

以下のコマンドはサーバーオーナーのみ使用できます。

| コマンド | 説明 |
|---|---|
| `!訂正` | ボットの誤回答に返信して `!訂正 正しい情報` と送ると、訂正内容をNotionに記録 |
| `!ナレッジ抽出` | トラブルシューティングチャンネルの履歴からQ&Aナレッジを自動抽出してNotionに保存 |
| `!pause` | 自動回答を一時停止 |
| `!resume` | 自動回答を再開 |
| `!status` | 稼働状態とメトリクス（一時停止状況・日次トークン・回答数・ゲート通過率・ヘルスチェック結果）を表示 |
| `!release_check` | Studioガイド/FWリリース情報を即時再取得し、最新版バージョンなどを確認 |

## 動作

- 指定チャンネルのメッセージを監視
- 返信先メッセージと同一チャンネルの直近会話を取得
- 今回の発言を前後の文脈込みで解釈
- Claude API で回答を生成し、リプライで返信
