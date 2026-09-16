# hr-chat-ui — 従業員向け Web チャット UI

SDD [§1.2 Scope Boundaries](../sdd.md) / [§1.3 体験層](../sdd.md) で定義された `hr-chat-ui`（自前 Web チャット）の実装です。

## 起動方法

```bash
GOOGLE_API_USE_CLIENT_CERTIFICATE=false uv run uvicorn ui.server:api --host 0.0.0.0 --port 8090
```

ブラウザで `http://localhost:8090/`（Cloudtop の場合は `http://<hostname>:8090/`）を開きます。

## SDD UI 要件との対応

| SDD 要件 | 実装 |
| :--- | :--- |
| 引用のクリック遷移（FR-5.3） | 回答本文中の `Section X.Y` を自動でリンク化。クリックでドロワーが開き、ハンドブック該当条文の逐語テキストを表示（`GET /api/policy`） |
| HITL 確認カード（Principle P3） | ツールが `CONFIRMATION_REQUIRED` を返すと、更新内容を表形式で提示する承認カードを描画。「承認して実行」で `user_confirmed=True` の再実行を送信 |
| ストリーミング表示 | Server-Sent Events（`POST /api/chat`）。`session` / `tool_call` / `tool_result` / `delta` / `message` / `done` / `error` の各イベントを逐次描画 |
| テストユーザ認証（BRD 第6章制約） | 左レールで `EMP-1001` / `EMP-1002` / `EMP-1003` を切り替え。切替時にセッションを再作成し、以降のツール呼び出しの `caller_id` に反映 |
| 監査カバー率 100%（Principle P7） | 右パネル「監査」タブに統一監査ログを表示。許可・拒否の両方を件数サマリ付きで可視化（`GET /api/audit`） |
| ツール層ガードレール（P1 / P2） | 実行トレースに各ツールの対象システム・参照/書込区分・判定結果バッジを表示。拒否時はガードレールコード付きのコールアウトを描画 |
| 補償トランザクション（UC-2.x） | Saga 失敗時に `SAGA_COMPENSATED_ROLLED_BACK` を検知し、WorkWeek の復元内容を明示するロールアウトを表示 |

## 構成

```
ui/
├── server.py           FastAPI バックエンド（SSE / 監査 / 企業状態 / 規程条文 API）
└── static/
    ├── index.html      画面構造
    ├── styles.css      デザインシステム（カラートークン・コンポーネント）
    └── app.js          SSE 受信、トレース描画、HITL カード、引用ドロワー
```

## API

| エンドポイント | 用途 |
| :--- | :--- |
| `GET /api/bootstrap` | テストユーザ一覧、シナリオ例、エージェントメタデータ |
| `POST /api/chat` | SSE ストリーミングでエージェントのターンを実行 |
| `GET /api/state?employee_id=` | WorkWeek プロフィール・休暇残高・申請、ServiceImmediately チケット |
| `GET /api/audit?limit=` | 統一監査ログ（許可・拒否の両方） |
| `GET /api/policy?section=` | 引用クリック時のハンドブック条文全文 |
| `POST /api/reset` | デモ用の企業データと監査ログを初期化 |
