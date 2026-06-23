# RunGuard — Cloud Run 当直エージェント 実装指示書（Claude Code 用）

> このファイルは、AIエージェント開発ハッカソン（DevOps × AI Agent Hackathon 2026）の提出作品
> **RunGuard** を Claude Code に実装してもらうための、自己完結した仕様書です。
> 会話の文脈がなくても、このファイルだけで実装を進められるように書いています。

---

## 0. このドキュメントの使い方（最初に読む）
- 君（Claude Code）は、このファイルの仕様に沿って **Phase 0 から順番に** 実装する。
- 各フェーズの終わりに必ず「動作確認の手順」を提示し、**確認が取れてから commit** する。
- 進捗は毎回 `## 現在地の整理` の見出しで、番号付きステップ＋`← 今ここ` の矢印で示す。
- 不明点や設計判断が必要な箇所は、勝手に進めず先に質問する。
- **重要**: 後述の「絶対制約」と「運用ルール」は例外なく守ること。

---

## 1. 目的（何を作るか・なぜ）

### 作るもの
Cloud Run サービスの **"当直の SRE"** を務める AI エージェント **RunGuard**。
監視対象の Cloud Run サービスの健康状態（エラー率・レイテンシ・ログ）を見張り、
異常 — 特に **新しいデプロイ直後の不調** — を検知したら、Gemini がログを読んで原因を診断し、
**実際に復旧アクション（直前の正常リビジョンへの自動ロールバック等）を実行**して、復旧を確認し、学習する。

### なぜエージェントなのか（必然性）
本番障害は、コードが完璧でも環境要因（アクセス急増・依存先 API ダウン・メモリ不足・設定ミス・悪いデプロイ）で必ず起きる。
従来は人間が「ログを読む→原因を見立てる→対応する→戻ったか確認する」を毎回手作業でやる。
RunGuard はこの一連を **観測→診断→アクション選択→実行→確認→学習** という自律ループとして閉じる。
ポイントは、Cloud Run は API で完全に操作できるため、エージェントが **本当に手を動かせる**（GUI ツールと違いループが本当に閉じる）こと。

### 想定ユーザー
個人開発者・小規模チームの SRE / インフラ担当。専任の当直を置けない現場の「一次対応の自動化」。

---

## 2. 絶対制約（クリーンルーム & セキュリティ）
これはハッカソンの参加規約と、作者のコンプライアンス方針に基づく **最優先の制約**。違反は不可。

1. **クリーンルーム**: 特定企業の内部情報・実データ・社内リポジトリのコード・内部識別子（プロジェクトID/チャンネルID/Bot ID/従業員名等）・社内固有の業務ルールを **一切含めない**。一般に公開されている技術知識のみで、ゼロから書く。
2. **個人情報ゼロ**: 監視対象・ログ・メトリクスはすべて **合成データ**。実在の個人・顧客データは扱わない。
3. **個人リソース前提**: GCP プロジェクト・GitHub アカウントは作者の **個人用 / 新規** を使う想定。コード内に特定のプロジェクトID等を **ハードコードしない**。すべて環境変数で受け取る。
4. **秘密情報をソース/コマンド引数に書かない**: APIキー・トークン・サービスアカウント鍵は `env.sh`（`.gitignore` 済）または Secret Manager で管理する。リポジトリにコミットしない。
5. **本番安全**: 後述の「セーフティ設計」（取り消し可能な手から / 確信度ゲート / dry-run / allowlist）を最初から組み込む。

---

## 3. 技術スタック（ハッカソン必須要件への対応）

| 区分 | 採用技術 | 必須要件への対応 |
| --- | --- | --- |
| 実行プロダクト（必須） | **Cloud Run**（エージェント本体・監視対象サンプル・ダッシュボードをホスト） | ✅ Google Cloud アプリ実行プロダクト |
| Google Cloud AI（必須） | **Gemini API**（診断の構造化出力）＋ **ADK (Agent Development Kit)** | ✅ Google Cloud AI 技術 |
| 操作・観測 | Cloud Run Admin API（リビジョン操作・ロールバック）/ Cloud Logging（ログ取得）/ Cloud Monitoring（メトリクス） | Google Cloud ネイティブ色を強化 |
| 永続化 | Firestore（ネイティブモード）または Cloud Storage の JSON（インシデント記録・プレイブック） | 軽量でよい |
| 任意（スポンサー） | Elasticsearch（**v1 では使わない**。将来「過去インシデントのログ署名を類似検索」で挟む余地として設計に穴を空けておくだけ） | 任意 |

実装言語: **Python 3.12**。

> **注意（重要）**: ADK・Gemini・各 Cloud クライアントライブラリは更新が速い。
> 実装着手時に **現在のライブラリ名・バージョン・API シグネチャを公式ドキュメントで確認** し、
> このファイルの記載と差異があれば最新に合わせること（記憶の API 名を当てにしない）。
> Gemini モデルは `GEMINI_MODEL` 環境変数で受け取り、デフォルトは **現行の Gemini flash 系**（利用可能な最新版を確認して設定）。構造化出力は Pydantic スキーマ（response schema）で行う。
> 推奨経路は **Vertex AI 経由（入力が学習に使われない）**。ローカル開発時のみ API キー方式も可。経路は env で切替可能にする。

---

## 4. システム全体像
3 つの Cloud Run サービス＋永続ストア＋ダッシュボードで構成する。

- **runguard-agent**（Cloud Run Service / スケールゼロ）
  - HTTP エンドポイント（`/` でダッシュボード配信、`/api/*` で状態取得・障害注入）。
  - 監視ループを内部スケジュールで回す（Cloud Scheduler から叩く方式でも、内部タイマーでも可。デモしやすい方を選ぶ）。
- **sample-service**（Cloud Run Service / 監視対象の合成アプリ）
  - 正常時は 200 を返す軽量アプリ。環境変数 `FAULT` で障害モードを切替（後述）。
  - 「正常リビジョン」と「不調リビジョン」を **複数リビジョンとして用意**し、トラフィックの振り分けで障害注入/ロールバックを再現する。
- **永続ストア**（Firestore / GCS）
  - インシデント記録（信号→診断→対応→結果→人の上書き）とプレイブック（既知の障害署名→効いた対応）。
- **ダッシュボード**（runguard-agent が配信する静的 UI）
  - 公開デモの顔。誰でも「障害注入→自動ロールバック→復旧」を 2 分で再現できる。

データの流れ:
`observe（Monitoring/Logging）→ diagnose（Gemini）→ decide（確信度ゲート）→ act（Cloud Run Admin API）→ verify（メトリクス再確認）→ learn（ストア更新）`

> 本リポジトリでの採用: **永続化 = Firestore（ネイティブモード）** / **監視ループ駆動 = Cloud Scheduler → `/api/tick`**。

---

## 5. 自律ループ仕様（核心）
ループは **純粋ロジック（副作用なし）と副作用（GCP 操作）を分離**して実装する。
これにより CI でロジックだけを GCP なしでテストできる（後述 eval）。
各ステップを ADK の **FunctionTool** として定義し、`LlmAgent`（Gemini）がオーケストレーションする。
監視サイクル自体は ADK の LoopAgent もしくは明示的なループで回す。

### 5.1 observe（観測）— 副作用あり（読み取り）
- Cloud Monitoring から監視対象の **5xx 率・レイテンシ・メモリ使用・インスタンス数** を取得。
- Cloud Logging から **直近のエラーログ**（重大度 ERROR 以上、件数上限つき）を取得。
- 直近の **デプロイ（新リビジョン作成）の有無と時刻** を取得。
- 出力: `Observation`（Pydantic）。例: `{ service, window, error_rate, p95_latency_ms, memory_ratio, instances, recent_error_logs[], last_deploy_at, current_revision, last_healthy_revision }`。
- ループ起動条件: 例「5xx 率が N 分連続でしきい値超過」。しきい値は env / config。

> Monitoring の取得が重い場合は、ログ件数からエラー率を概算するフォールバックを用意してよい。ただし第一選択は Monitoring API。

### 5.2 diagnose（診断）— 純粋関数（観測→診断）
- `Observation` を Gemini に渡し、**構造化出力**で `Diagnosis` を得る。
- `Diagnosis`（Pydantic）: `{ category, confidence(0-1), evidence_log_lines[], reasoning, recommended_action }`。
- `category` の例: `bad_deploy` / `out_of_memory` / `dependency_5xx` / `crash_loop` / `traffic_spike` / `unknown`。
- **診断の肝**: 「エラー急増が **新リビジョンのデプロイ直後** に始まったか」の相関を最重視する（直後の急増 = 悪いデプロイ → ロールバックが正解）。
- Gemini 呼び出しは差し替え可能なインターフェイスにし、テストではモックを注入できるようにする。

### 5.3 decide（アクション選択）— 純粋関数（診断→決定）
- 入力 `Diagnosis` から `Decision`（Pydantic）: `{ action, target_service, target_revision, reason, requires_human }` を決める。
- **確信度ゲート**: `confidence ≥ AUTO_ACT_THRESHOLD`（例 0.8）かつ `category ∈ 自動対応可能集合` のときだけ自動実行。さもなくば `requires_human = true`（通知して待機）。
- **アクション対応表（初期）**:
  - `bad_deploy` → `rollback`（直前の正常リビジョンへトラフィックを戻す）
  - `out_of_memory` → `escalate`（メモリ上限の引き上げは設定変更=人の確認推奨。将来は提案 Issue）
  - `dependency_5xx` → `escalate`（ロールバックしても無意味なのでアラートのみ）
  - `crash_loop` → `rollback`（直近デプロイ起因なら戻す）/ それ以外は `escalate`
  - `unknown` / 低確信 → `escalate`
- **allowlist**: 操作対象は env で指定した監視対象サービスのみ。**runguard-agent 自身は絶対に操作しない**。

### 5.4 act（実行）— 副作用あり（書き込み）
- `rollback`: Cloud Run Admin API で、対象サービスの **トラフィックを `last_healthy_revision` に 100% 戻す**。
- `escalate`: Issue 起票（GitHub API、任意）/ Slack 通知（任意・Webhook）/ 最低限ダッシュボードとログに記録。
- **セーフティ**: `DRY_RUN=1` のときは実行せず意図だけ記録。同一インシデントで連続ロールバックしない（クールダウン＋1インシデント最大アクション数）。

### 5.5 verify（確認）— 副作用あり（読み取り）
- アクション後、数分メトリクスを観測し、**エラー率がベースラインに戻ったか**確認。
- 戻った → 解決として記録。戻らない → 人へエスカレーション（自動ループの暴走を防ぐ）。

### 5.6 learn（学習）— 副作用あり（書き込み）
- 各インシデントを `Incident`（Pydantic）としてストアに保存: `{ id, timestamp, observation, diagnosis, decision, outcome, human_override }`。
- **プレイブック**を更新: 「観測の署名（例: 直後デプロイ＋5xx 急増のパターン）→ 効いた対応」を蓄積。次サイクルの診断・確信度判断にこの履歴を文脈として渡す。
- これが「まわす（継続的改善）」の実体。回を追うごとに正診率が上がる設計にする。

---

## 6. 障害タイプとデモ（fault injection）

### sample-service の障害モード（env `FAULT`）
- `ok`（デフォルト）: 全リクエスト 200。
- `http500`: 一定割合 or 全件 500 を返す（悪いデプロイの再現）。
- `memory`: リクエストごとに大きなメモリを確保して OOM 気味にする。
- `crash`: 起動直後にクラッシュ／一定確率で異常終了（クラッシュループ再現）。

### 障害注入とロールバックの仕組み（リアルに見せる）
- sample-service に **「正常リビジョン」と「不調リビジョン」を両方デプロイ**しておく（不調リビジョンは `FAULT=http500` 等でビルド）。
- ダッシュボードの **「障害を注入」ボタン** = トラフィックを **不調リビジョンへ振り替える**（実際の Cloud Run トラフィック操作）。
- RunGuard が 5xx 急増を観測 → 診断（bad_deploy）→ **トラフィックを正常リビジョンへ戻す（本物のロールバック）** → 復旧をダッシュボードで可視化。
- これにより **実際の Cloud Run リビジョン/トラフィック API を使った本物の復旧** をデモできる。ログ・サービスは合成、個人情報ゼロ。

### 公開デモの 2 分シナリオ（README にも記載）
1. ダッシュボードを開く（エラー率はフラット、正常リビジョンに 100%）。
2. 「障害を注入」を押す → エラー率が跳ね上がる。
3. RunGuard が検知 → ログを読んで「悪いデプロイ」と診断（診断文を表示）。
4. 自動ロールバック実行 → エラー率が回復。
5. インシデント記録とプレイブック更新が表示される。

---

## 7. セーフティ設計（本番に触るエージェントの説得力 = 審査の実装力）
最初から組み込む:

1. **取り消し可能な手から**: 自動実行はロールバック等の即座に戻せる操作に限定。削除・恒久的スケール変更などの不可逆操作は自動実行しない。
2. **確信度ゲート**: 既知カテゴリ × 高確信のときだけ自動実行。迷えば人へ。
3. **dry-run モード**: `DRY_RUN=1` で副作用なし（CI と初期テスト用）。公開デモは実行モードで動かす。
4. **allowlist**: 操作対象は env で明示した監視対象のみ。自分自身は対象外。
5. **ループ保護**: クールダウン、1 インシデントあたり最大アクション数、連続ロールバック禁止。

---

## 8. リポジトリ構成
```
runguard/
├─ README.md                  # 概要・セットアップ・2分デモ手順（提出用）
├─ SPEC.md                    # この指示書のコピー
├─ ARCHITECTURE.md            # 構成図(画像)＋説明（ProtoPedia流用）
├─ .gitignore                 # env.sh, *.json鍵, .env, __pycache__ などを除外
├─ env.sh.example             # 環境変数テンプレ（実値は書かない）
├─ deploy.sh                  # Cloud Runデプロイ（agent / sample(正常) / sample(不調)）
├─ requirements.txt
├─ agent/
│  ├─ __init__.py
│  ├─ main.py                 # Cloud Runエントリ（/ ダッシュボード, /api/*, ループ起動）
│  ├─ config.py               # env読込（PROJECT_ID/REGION/TARGETS/閾値/DRY_RUN/モデル）
│  ├─ models.py               # Pydantic: Observation/Diagnosis/Decision/Incident
│  ├─ observe.py              # Monitoring/Logging→Observation（副作用・読み取り）
│  ├─ diagnose.py             # Gemini構造化出力→Diagnosis（純粋・Gemini差し替え可）
│  ├─ decide.py               # Diagnosis→Decision（純粋・確信度ゲート・allowlist）
│  ├─ actions.py              # Cloud Run Admin API: rollback等（副作用・書き込み）
│  ├─ verify.py               # 実行後メトリクス確認（副作用・読み取り）
│  ├─ learn.py                # Incident記録・プレイブック更新（Firestore/GCS）
│  ├─ loop.py                 # observe→diagnose→decide→act→verify→learn
│  ├─ adk_app.py              # ADK: LlmAgent + FunctionTools 配線
│  └─ gemini_client.py        # Gemini呼び出し（Vertex/開発APIを切替・モック可能）
├─ sample_service/
│  ├─ main.py                 # FAULT=ok|http500|memory|crash で挙動を切替
│  ├─ Dockerfile
│  └─ requirements.txt
├─ dashboard/
│  ├─ index.html              # health可視化/障害注入ボタン/インシデント/プレイブック/eval結果
│  └─ app.js
├─ eval/
│  ├─ incidents.yaml          # 合成インシデント（観測信号→期待診断→期待アクション）
│  ├─ mocks.py                # GCP/Geminiクライアントのモック（hermetic）
│  └─ run_eval.py             # 観測モック→diagnose/decide→指標計算
└─ .github/workflows/
   └─ eval.yml                # push毎にrun_eval（GCP不要・hermetic）
```

---

## 9. 実装フェーズ（順番に。各フェーズ末に動作確認→commit）

### Phase 0 — 土台
- リポジトリ初期化、`requirements.txt`、`.gitignore`、`env.sh.example`、`README.md` 骨子。
- `config.py` で env を一元読込（`PROJECT_ID` / `REGION`（既定 `asia-northeast1`）/ `AGENT_SERVICE` / `TARGET_SERVICES`（allowlist）/ `AUTO_ACT_THRESHOLD` / `DRY_RUN` / `GEMINI_MODEL` / Gemini 経路）。
- **確認**: `python -c "import agent.config"` が通る。秘密情報がコミット対象に無い（`git status` 確認）。

### Phase 1 — 監視対象サンプル＋障害注入
- `sample_service` を実装（`FAULT` で挙動切替）＋ `Dockerfile`。
- 「正常」「不調(`FAULT=http500`)」の 2 リビジョンをデプロイし、トラフィック振替で障害注入/復旧できることを確認。
- **確認**: 正常時 `curl` が 200、不調リビジョンへ振替で 500。`gcloud run services update-traffic` で両方向に切替できる（**現行コマンド構文を確認して使う**）。

### Phase 2 — observe＋diagnose
- `models.py`（Pydantic）、`observe.py`（Monitoring/Logging→`Observation`）、`gemini_client.py`、`diagnose.py`（構造化出力→`Diagnosis`）。
- **確認**: サンプルのエラーログを与えると、妥当な `Diagnosis`（category/confidence/evidence）が返る。Gemini をモックした **hermetic な単体テスト** を 1 本用意。

### Phase 3 — decide＋act＋verify（セーフティ込み）
- `decide.py`（確信度ゲート・allowlist・アクション対応表）、`actions.py`（Cloud Run Admin API で `last_healthy_revision` へロールバック）、`verify.py`。
- `DRY_RUN` / クールダウン / 自己サービス除外を実装。
- **確認**: `DRY_RUN=1` で意図ログのみ。実行モードで実際にトラフィックが正常リビジョンへ戻る。`verify` が復旧を検知。

### Phase 4 — loop＋learn＋ADK 配線
- `loop.py` でサイクルを回す（内部タイマー or Cloud Scheduler）。`learn.py` で `Incident`／プレイブックを永続化。`adk_app.py` で各ツールを `LlmAgent` に配線。
- **確認**: sample-service にエンドツーエンド — 障害注入 → ループが検知 → 診断 → ロールバック → 復旧確認 → 記録、が一気通貫で動く。

### Phase 5 — ダッシュボード（公開デモの顔）
- `dashboard/`：エラー率スパークライン、インシデントのタイムライン、**障害注入ボタン**、エージェントの現在の診断/アクション表示、プレイブック表示、eval 結果表示。
- UI はシンプルで直感的に（審査の「ユーザビリティ」）。過度な装飾は不要。
- **確認**: ローカル/デプロイ先で開き、ボタン操作で 2 分シナリオが再現できる。

### Phase 6 — eval ＋ CI（「まわす」の裏付け・作り込みすぎない）
- `eval/incidents.yaml`（観測信号→期待診断→期待アクション）、`mocks.py`、`run_eval.py`（**正診率 / 正アクション率 / 誤動作率 / 想定 MTTR** を算出）。
- `.github/workflows/eval.yml` で push 毎に `run_eval.py` を実行（**GCP 不要・hermetic**：observe/act をモック、diagnose の Gemini もモック or 小規模実呼び出し）。
- **確認**: `python eval/run_eval.py` が指標を出力。CI が緑。ルールを 1 つ足すと指標が動く。

### Phase 7 — デプロイ＋ドキュメント＋公開
- `deploy.sh`（agent / sample 正常 / sample 不調 をデプロイ）。すべて **スケールゼロ**、`runguard-agent` と `sample-service` は **公開（無認証）**（合成データのみなので可）。
- `README.md`（セットアップ＋2 分デモ手順）、`ARCHITECTURE.md`（構成図画像＋説明）。
- **確認**: 第三者が公開 URL を開いてデモを再現できる。リポジトリは公開準備 OK（秘密情報なし）。

---

## 10. 完了条件（Definition of Done）
- [ ] 公開 URL で、誰でも「障害注入 → 自動ロールバック → 復旧」を再現できる。
- [ ] GitHub 公開リポジトリ（秘密情報なし、README / ARCHITECTURE あり）。
- [ ] CI（eval）が緑で、指標が出力される。
- [ ] **ADK ＋ Gemini ＋ Cloud Run ＋ Cloud Run Admin / Logging / Monitoring** を使用している。
- [ ] 合成データのみ・個人情報ゼロ・特定企業の内部情報ゼロ。
- [ ] セーフティ（取り消し可能 / 確信度ゲート / dry-run / allowlist / ループ保護）が効いている。

---

## 11. 提出物（ハッカソン・参考）
提出は次の 3 点を Google フォームに登録（締切: **2026/7/10 23:59**）:
1. 公開 GitHub リポジトリ URL
2. 動作するデプロイ URL（runguard-agent のダッシュボード）
3. ProtoPedia 作品 URL

ProtoPedia 側で必須になるもの（実装と並行して素材を用意）:
- **デモ動画**（2 分シナリオの画面録画）
- **システムアーキテクチャ図**（`ARCHITECTURE.md` の図を流用）
- **ストーリー**: ①課題と背景 ②想定ユーザー ③プロダクトの特徴
- **タグ**に `findy_hackathon` を必ず付与

---

## 12. Claude Code が守る運用ルール
- **秘密情報をソース/コマンド引数に書かない**。`env.sh`（`.gitignore` 済）/ Secret Manager で管理。
- ファイルを編集するときは **全文** を書く（曖昧な部分 diff にしない）。
- **動作確認が取れてから commit** する。動いていないものをコミットしない。
- コマンドは常に **コピペで実行できる完全な形** で提示する。
- 変更後は **番号付きの完全な手順**（デプロイ・実行・環境変数設定を含む）を提示する。
- コードコメントは **最小限**（日本語可）。
- 進捗は `## 現在地の整理` ＋ `← 今ここ` で示す。
- ライブラリ/API は **着手時に最新仕様を確認** し、記憶に頼らない。

---

## 13. 最初の一手
- まず Phase 0 のスキャフォールドを作り、動作確認の手順を提示する。
- 各フェーズは「実装 → 動作確認の手順提示 → 確認 → commit」の順で進める。
- 不明点や設計判断が要る箇所は、勝手に進めず先に質問する。
- 「絶対制約」と「運用ルール」は必ず守る。

事前に用意するもの:
- 個人用 GCP プロジェクトID / リージョン（既定 asia-northeast1）
- Gemini の利用経路（Vertex AI 推奨 / 開発API）と GEMINI_MODEL
- 監視対象サービス名（allowlist）
