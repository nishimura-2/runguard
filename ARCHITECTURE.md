# RunGuard アーキテクチャ

> 構成図（画像）と詳細説明は Phase 7 で完成させる。以下は骨子。

## 全体像
3 つの Cloud Run サービス + 永続ストア + ダッシュボードで構成する。

```text
        ┌──────────────────┐  tick(/api/tick)   ┌────────────────────────────┐
        │ Cloud Scheduler  │ ─────────────────► │      runguard-agent        │
        └──────────────────┘                    │       (Cloud Run)          │
                                                 │                            │
   ┌── observe ──────────────────────────────── │  observe → diagnose →      │
   │   Cloud Monitoring / Cloud Logging          │  decide → act → verify →   │
   │   (sample-service のメトリクス/ログ)         │  learn                     │
   │                                             └───────┬───────────┬────────┘
   │                                       diagnose(Gemini)          │ act: rollback
   │                                                                 ▼
   │                                                   ┌────────────────────────┐
   └─────────────────────────────────────────────────►│  Cloud Run Admin API   │
                                                       └───────────┬────────────┘
                                                                   ▼
                                                       ┌────────────────────────┐
                                                       │     sample-service     │
                                                       │  (正常/不調 2リビジョン) │
                                                       └────────────────────────┘
   learn ──► ┌──────────────────────────────┐
             │ Firestore: incidents/playbook │
             └──────────────────────────────┘

   Dashboard (static, runguard-agent が /api/* で配信) ◄──► 状態取得・障害注入
```

## データの流れ
`observe (Monitoring/Logging) → diagnose (Gemini) → decide (確信度ゲート) → act (Cloud Run Admin API) → verify (メトリクス再確認) → learn (Firestore)`

## コンポーネント
| コンポーネント | 役割 |
| --- | --- |
| `runguard-agent` | エージェント本体。ダッシュボード配信 / `/api/*` / 監視ループ。スケールゼロ。 |
| `sample-service` | 監視対象の合成アプリ。`FAULT` で障害モード切替。正常/不調の 2 リビジョン。 |
| Firestore | インシデント記録・プレイブックの永続化。 |
| Cloud Scheduler | `/api/tick` を定期的に叩いて 1 サイクル実行（スケールゼロ両立）。 |

## セーフティ設計
取り消し可能な手から / 確信度ゲート / dry-run / allowlist（自己除外）/ ループ保護（クールダウン・最大アクション数・連続ロールバック禁止）。
