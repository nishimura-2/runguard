# RunGuard アーキテクチャ

## 全体像

`runguard-agent`（本体）＋ `sample-service`（監視対象の合成アプリ）＋ Firestore（記録/学習）＋ ダッシュボードで構成する。
点検の主動線は **ADK の LlmAgent** が駆動し、Gemini/ADK が使えない環境では確定パイプライン（`loop.run_cycle`）へ自動フォールバックする。

```text
   ┌──────────────────┐  point検(/api/agent)   ┌──────────────────────────────────┐
   │  Dashboard / 巡回 │ ─────────────────────► │          runguard-agent          │
   └──────────────────┘                        │            (Cloud Run)           │
                                                │  ADK LlmAgent（主動線）          │
                                                │   tools: observe / rollback /    │
   ┌── observe ──────────────────────────────  │   scale_memory / scale_instances │
   │   HTTP プローブ + Cloud Logging            │   / restart / propose_code_fix   │
   │   (sample-service の 5xx率/ログ)           │  └ ADK不可時 → run_cycle へfallback│
   │                                            │  observe→diagnose(Gemini)→decide │
   │                                            │  →act→verify→learn               │
   │                                            └───────┬───────────────┬──────────┘
   │                             diagnose(Gemini/Vertex)│               │ act
   │                                                    ▼               ▼
   │                                        ┌───────────────┐  ┌────────────────────┐
   └───────────────────────────────────────│ Cloud Run     │  │ Firestore          │
                                            │ Admin API     │  │ incidents/playbook │
                                            │ (traffic/scale)│ └────────────────────┘
                                            └──────┬────────┘
                                                   ▼
                              ┌───────────────────────────────────────────┐
                              │            sample-service                 │
                              │ リビジョン: healthy / bad(http500) /       │
                              │  feature-bug(新機能+0除算) / fixed(修正版)  │
                              └───────────────────────────────────────────┘
```

## データの流れ

`observe（HTTPプローブ + Cloud Logging）→ diagnose（Gemini 構造化出力）→ decide（カテゴリ→対応＋確信度ゲート）→ act（Cloud Run Admin API）→ verify（再観測）→ learn（Firestore プレイブック）`

- **decide のカテゴリ→対応表**: bad_deploy / crash_loop(直近)→`rollback`、out_of_memory→`scale_memory`、traffic_spike→`scale_instances`、crash_loop(それ以外)→`restart`、**feature_bug→`self_heal`（人の承認ゲート）**、それ以外→`escalate`。
- **self_heal の3段**: ①**即時ロールバックで止血**（正常版へ戻す＝承認待ちの間も健全）→ ②修正案生成（`selfheal.generate_fix` が Gemini 構造化出力でバグだけ修正した完全ソースを生成、`difflib` で差分化。オフラインは定型修正へ退避。outcome=`rolled_back_awaiting_fix`）→ ③**人が承認**後に修正版をデプロイ（既定は事前ビルド済み `fixed` リビジョンへ振替）、検証、失敗時はロールバック退避。

## コンポーネント

| コンポーネント | 役割 |
| --- | --- |
| `runguard-agent` | エージェント本体。ダッシュボード配信 / `/api/*` / ADK 主動線 + 確定パイプライン。スケールゼロ。 |
| `sample-service` | 監視対象の合成アプリ。`FAULT` で挙動切替。`healthy` / `bad`(http500) / `feature-bug`(新機能+0除算) / `feature_fixed`(修正版) を用意。 |
| Firestore | インシデント記録・プレイブック（障害署名→効いた対応）の永続化。 |
| Cloud Scheduler | `/api/agent`(または `/api/tick`) を定期実行して1サイクル（スケールゼロ両立）。 |
| Elasticsearch（任意） | 過去インシデントのログ署名を `semantic_text` で索引し、診断時に類似検索して文脈注入（スポンサー枠）。 |

## モジュール（agent/）

`config`（設定）/ `models`（Pydantic ドメイン: Observation/Diagnosis/Decision/CodeFix/Incident）/ `observe`（観測）/ `diagnose`（Gemini 診断）/ `decide`（対応決定＋ゲート）/ `actions`（実行・dry-run・ループ保護）/ `selfheal`（コード修正生成・差分）/ `loop`（run_cycle・execute_and_record・self_heal 提案/適用）/ `adk_app`（ADK LlmAgent 主動線）/ `sim`・`real_env`（Sim/Real バックエンド）/ `learn`（InMemory/Firestore ストア）/ `verify`（復旧確認）/ `elastic_store`（類似検索）/ `main`（FastAPI）。

## セーフティ設計

取り消し可能な操作のみ自律実行 / **self_heal は人の承認ゲート** / 確信度ゲート / dry-run / allowlist（自己除外）/ ループ保護（クールダウン・最大アクション数）/ self_heal 失敗時は自動ロールバック退避。
