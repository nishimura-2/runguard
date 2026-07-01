# RunGuard — Cloud Run 当直 SRE エージェント

Cloud Run サービスの「当直の SRE」を務める AI エージェント。監視対象の健康状態（5xx 率・ログ・デプロイ経過）を見張り、
異常を検知すると **ADK エージェントが Gemini で原因を診断し、事象に応じた復旧を自分で選んで実行**する。
単なるロールバックに留まらず、**新機能のバグはコードを直して直す（自己修復）**のが特徴。

自律ループ: `observe → diagnose → decide → act → verify → learn`

> DevOps × AI Agent Hackathon 2026 提出作品。タグ: `findy_hackathon`

## 何がすごいか

**1. ロールバックだけじゃない「復旧手段の引き出し」** — 事象に応じて取り消し可能な実操作を使い分ける:

| 事象（診断） | エージェントの対応 | 実体 |
| --- | --- | --- |
| 悪いデプロイ / 直近デプロイ起因のクラッシュ | **ロールバック** | 正常リビジョンへトラフィック100%（自律） |
| メモリ不足(OOM) | **メモリ上限を引き上げ** | `run.services.update`（自律） |
| アクセス急増 | **max-instances を増やす** | `run.services.update`（自律） |
| クラッシュ（非デプロイ起因） | **再起動**（新リビジョン） | 同一イメージで再デプロイ（自律） |
| **新機能のバグ** | **🔧 AI がコードを修正（self-heal）** | Gemini がバグだけ直した新版を提案 → **人が承認** → デプロイ |

**2. 自己修復（self-heal）= ロールバックの一段上** — 新機能にバグ（例: `/api/price` の 0除算）が入って 5xx になったとき、
ロールバックすると**せっかくの新機能まで失う**。RunGuard は traceback を読んで「新機能のバグ」と判断し、
**新機能は残したままバグ箇所だけを修正した新リビジョンを提案**、ダッシュボードにコード差分（赤/緑）を表示、
**人が差分を承認**するとデプロイし、復旧を検証（直らなければ自動でロールバック退避）。

**3. ADK が主動線を駆動** — 監視・対応は ADK の LlmAgent がツール（observe / rollback / scale / restart / コード修正提案）を
呼んで実行する単一動線。Gemini/ADK が使えない環境では確定パイプラインへ**自動フォールバック**するのでデモが止まらない。

## セーフティ設計

- 自律実行は**取り消し可能な操作**（ロールバック / スケール / 再起動）に限定。**コードを出荷する self-heal は必ず人の承認ゲート**を通す。
- `confidence ≥ AUTO_ACT_THRESHOLD`（既定 0.8）＋ allowlist（`runguard-agent` 自身は絶対に操作しない）＋ ループ保護（クールダウン / 1インシデント最大アクション数）。
- self-heal は失敗時（修正後も未復旧）に**正常版へ自動ロールバック退避**。
- `DRY_RUN=1` で副作用なし。合成データのみ・個人情報ゼロ（クリーンルーム）。

## アーキテクチャ

`runguard-agent`（本体）＋ `sample-service`（監視対象の合成アプリ）＋ Firestore（記録/学習）＋ ダッシュボード。詳細は [ARCHITECTURE.md](ARCHITECTURE.md)。

技術: Python 3.12 / **Google ADK** / **Gemini（Vertex AI, `gemini-3.5-flash`）** / Cloud Run Admin API / Cloud Logging / Firestore / Cloud Scheduler / Elasticsearch（任意・類似インシデント検索）。

## ローカルで試す（GCP 不要・オフライン sim デモ）

GCP/Gemini が無くても、シミュレーションモードで全機能が動きます（診断は RuleBased、対応は確定パイプラインへ自動フォールバック）。

```bash
python -m venv .venv
source .venv/Scripts/activate        # Windows(Git Bash)。PowerShell は .venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn agent.main:app --port 8080
# ブラウザで http://127.0.0.1:8080
```

デモ手順（各シナリオの間にリセット不要）:
1. **🆕 新機能＋バグ** → `🤖 AIエージェントで点検` → コード差分を確認 → **✅ 承認してデプロイ** → 緑に復旧（＝目玉の自己修復）
2. **⚠️ 悪いデプロイ(http500)** → `点検` → 即ロールバックで復旧
3. **🧠 メモリ不足 / 📈 アクセス急増** → `点検` → メモリ/インスタンスを増やして復旧
4. タイムラインの各行をクリックすると、診断理由・判断・エラーログ・コード差分の詳細が開く

テスト & 評価（hermetic・GCP 不要）:
```bash
python -m unittest discover -s tests      # 単体テスト
python eval/run_eval.py                    # 合成インシデントで指標を算出
```

## 評価（eval）

7 件の合成インシデントで診断・判断の正しさを自動計測（CI で実行）:

- 正診率 85.7% / 正アクション率 100% / 誤動作率 0%
- 想定 MTTR: 手動30分 → **13.7分（54%短縮）**（自律復旧の手段が増えたぶん改善）

## デプロイ（GCP 接続時）

`deploy.sh` を参照（`env.sh.example` → `env.sh` に実値、`source env.sh` 後にデプロイ）。REAL モードでは
本物の Cloud Run 操作（トラフィック振替・スケール更新）を行う。self-heal の修正版は既定で**事前ビルド済みリビジョンへ振替**
（`SELF_HEAL_LIVE=1` でライブビルドも可・要 Cloud Build 権限）。**秘密情報はコミットしない**（`env.sh` は `.gitignore` 済）。

## 状態

完成・公開・CI 緑（GitHub Actions: hermetic 単体テスト + eval）。合成データのみを扱う教育・デモ目的のプロジェクト。仕様全文は [SPEC.md](SPEC.md)。
