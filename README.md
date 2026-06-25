# RunGuard — Cloud Run 当直 SRE エージェント

Cloud Run サービスの「当直の SRE」を務める AI エージェント。
監視対象の Cloud Run サービスの健康状態（エラー率・レイテンシ・ログ）を見張り、
異常（特に **新しいデプロイ直後の不調**）を検知すると、Gemini がログを読んで原因を診断し、
**実際に復旧アクション（直前の正常リビジョンへの自動ロールバック等）を実行**して、復旧を確認し、学習する。

自律ループ: `observe → diagnose → decide → act → verify → learn`

> DevOps × AI Agent Hackathon 2026 提出作品。タグ: `findy_hackathon`

## 特徴
- **本当に手を動かす**: Cloud Run Admin API で実際にトラフィックをロールバックする（GUI ツールと違いループが本当に閉じる）。
- **安全第一**: 取り消し可能な手から / 確信度ゲート / dry-run / allowlist（自己除外）/ ループ保護。
- **クリーンルーム**: 合成データのみ・個人情報ゼロ・特定企業の内部情報ゼロ。

## アーキテクチャ
3 つの Cloud Run サービス + 永続ストア + ダッシュボード。詳細は [ARCHITECTURE.md](ARCHITECTURE.md)。

- `runguard-agent` — エージェント本体（ダッシュボード配信 / `/api/*` / 監視ループ）
- `sample-service` — 監視対象の合成アプリ（`FAULT` で障害モード切替・正常/不調の 2 リビジョン）
- Firestore — インシデント記録・プレイブックの永続化
- Cloud Scheduler → `/api/tick` — 監視ループの駆動（スケールゼロ両立）
- Elasticsearch（任意）— 過去インシデントのログ署名を `semantic_text` で索引し、診断時に類似検索（スポンサー枠）

技術: Python 3.12 / ADK / Gemini API（`gemini-3.5-flash`）/ Cloud Run Admin API / Cloud Logging / Cloud Monitoring / Firestore / Elasticsearch（任意）。

## セットアップ（概要）
> 詳細手順は各フェーズ完了時に追記する。**秘密情報はコミットしない**（`env.sh` は `.gitignore` 済）。

1. **Python 3.12** をインストール: `winget install Python.Python.3.12`
2. **個人用の新規 GCP プロジェクト**を作成し、課金を有効化。
3. `env.sh.example` を `env.sh` にコピーして実値を設定 → `source env.sh`。
4. 依存をインストール: `python -m pip install -r requirements.txt`
5. （Phase 1 以降）`./deploy.sh` でデプロイ。

## ローカルで試す（GCP 不要・オフライン sim デモ）
GCP/Gemini が無くても、シミュレーションモードで「障害注入 → 自動診断 → ロールバック → 復旧」が動きます。
```bash
python -m venv .venv
source .venv/Scripts/activate        # Windows(Git Bash)。PowerShell は .venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn agent.main:app --port 8080
# ブラウザで http://127.0.0.1:8080 → 「⚠️ 障害を注入」→ 自動巡回が検知し診断・ロールバック・復旧
```
テスト: `python -m unittest discover -s tests`

## 2 分デモ（公開後）
1. ダッシュボードを開く（エラー率はフラット・正常リビジョンに 100%）。
2. 「障害を注入」を押す → エラー率が跳ね上がる。
3. RunGuard が検知 → ログを読んで「悪いデプロイ」と診断（診断文を表示）。
4. 自動ロールバック実行 → エラー率が回復。
5. インシデント記録とプレイブック更新が表示される。

## セーフティ
- 自動実行はロールバック等の **取り消し可能な操作**に限定。削除や恒久的スケール変更は自動実行しない。
- `confidence ≥ AUTO_ACT_THRESHOLD`（既定 0.8）かつ既知カテゴリのときだけ自動実行。さもなくば人へエスカレーション。
- `DRY_RUN=1` で副作用なし。クールダウン / 1 インシデント最大アクション数 / 連続ロールバック禁止。
- 操作対象は `TARGET_SERVICES`（allowlist）のみ。**`runguard-agent` 自身は絶対に操作しない**。

## 開発状況
- [x] Phase 0 — 土台（スキャフォールド・config）
- [x] Phase 1 — 監視対象サンプル + 障害注入（ローカル検証済 / GCP デプロイ確認は接続後）
- [x] Phase 2 — observe + diagnose（hermetic テスト済 / observe ライブは接続後）
- [x] Phase 3 — decide + act + verify（dry-run/確信度ゲート/allowlist/ループ保護を検証）
- [x] Phase 4 — loop + learn + ADK 配線（in-memory ストアで検証 / Firestore は接続後）
- [x] Phase 5 — ダッシュボード + オフライン sim デモ（uvicorn で E2E 動作確認）
- [x] Phase 6 — eval + CI（hermetic・正診率/正アクション率/誤動作率/想定MTTR を算出、GitHub Actions）
- [ ] Phase 7 — デプロイ + ドキュメント（GCP 接続後）

## 注意
合成データのみを扱う教育・デモ目的のプロジェクト。実在の個人・顧客データは一切扱わない。
仕様の全文は [SPEC.md](SPEC.md) を参照。
