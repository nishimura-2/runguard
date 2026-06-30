"""sample-service — RunGuard の監視対象となる合成アプリ（標準ライブラリのみ）。

環境変数 FAULT で挙動を切替える:
  ok            : 全リクエスト 200（既定）
  http500       : FAULT_RATE の割合で 500 を返す（悪いデプロイの再現）
  memory        : リクエストごとに大きなメモリを確保して保持（OOM 気味にする）
  crash         : 起動直後 / リクエスト時に確率で異常終了（クラッシュループ再現）
  feature_bug   : 新機能 /api/price（単価計算）に 0除算バグ。qty=0 で ZeroDivisionError → 500（self_heal デモ用）
  feature_fixed : 同じ新機能のバグ修正版（0除算ガードあり）。AI が直した『修正版リビジョン』を表す

- Cloud Run は $PORT でのリッスンを要求する。
- ログは stdout に JSON 1 行で出す（Cloud Logging が自動収集 → 後段の observe/diagnose で利用）。
- 合成データのみ。実在の個人情報・顧客データは一切扱わない。
"""
from __future__ import annotations

import json
import os
import random
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAULT = os.environ.get("FAULT", "ok").strip().lower()
FAULT_RATE = float(os.environ.get("FAULT_RATE", "1.0"))      # http500 / crash の発生確率
MEMORY_CHUNK_MB = int(os.environ.get("MEMORY_CHUNK_MB", "50"))  # FAULT=memory の 1 回確保量
PORT = int(os.environ.get("PORT", "8080"))
REVISION = os.environ.get("K_REVISION", "local")             # Cloud Run が自動注入
SERVICE = os.environ.get("K_SERVICE", "sample-service")      # Cloud Run が自動注入

# FAULT=memory のリーク用バッファ
_leak: list = []


def log(severity: str, message: str, **fields) -> None:
    entry = {
        "severity": severity,
        "message": message,
        "service": SERVICE,
        "revision": REVISION,
        "fault": FAULT,
    }
    entry.update(fields)
    print(json.dumps(entry, ensure_ascii=False), flush=True)


# crash モード: 起動直後に確率で異常終了（クラッシュループの再現）
if FAULT == "crash" and random.random() < FAULT_RATE:
    log("CRITICAL", "startup crash (FAULT=crash)")
    os._exit(1)


def handle_price(subtotal: float, qty: int) -> dict:
    """新機能: 小計と数量から単価を計算して返す（/api/price）。

    FAULT=feature_bug では 0除算ガードが無く、qty=0 で ZeroDivisionError → 500。
    FAULT=feature_fixed（AI 修正版）ではガードを追加し、新機能は維持したまま 200 を返す。
    """
    if FAULT == "feature_fixed" and qty <= 0:
        return {"subtotal": subtotal, "qty": qty, "unit_price": 0.0, "note": "qty must be > 0"}
    unit_price = subtotal / qty           # feature_bug: ガードなし → qty=0 で ZeroDivisionError
    return {"subtotal": subtotal, "qty": qty, "unit_price": round(unit_price, 2)}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # ヘルスチェックは常に 200（障害注入の影響を受けない）
        if self.path == "/healthz":
            self._send(200, {"status": "ok", "revision": REVISION})
            return

        # feature_bug / feature_fixed: 新機能(単価計算)を全リクエストで実行（既定 qty=0 でバグ発火）
        if FAULT in ("feature_bug", "feature_fixed"):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            subtotal = float(params.get("subtotal", ["1000"])[0])
            qty = int(params.get("qty", ["0"])[0])
            try:
                result = handle_price(subtotal, qty)
                self._send(200, {"status": "ok", "revision": REVISION, **result})
            except Exception as e:
                log("ERROR",
                    "Traceback (most recent call last):\n" + traceback.format_exc().strip(),
                    path=self.path, status=500, exception=type(e).__name__)
                self._send(500, {"error": type(e).__name__, "detail": str(e), "revision": REVISION})
            return

        # http500: 悪いデプロイの再現
        if FAULT == "http500" and random.random() < FAULT_RATE:
            log("ERROR", "synthetic 500 (FAULT=http500)", path=self.path, status=500)
            self._send(500, {
                "error": "synthetic_internal_error",
                "detail": "injected by FAULT=http500",
                "revision": REVISION,
            })
            return

        # memory: 大きめのメモリを確保して保持（OOM 気味に）
        if FAULT == "memory":
            _leak.append(bytearray(MEMORY_CHUNK_MB * 1024 * 1024))
            log("WARNING", "allocated memory chunk (FAULT=memory)",
                path=self.path, held_chunks=len(_leak), chunk_mb=MEMORY_CHUNK_MB)

        # crash: リクエスト時に確率で異常終了
        if FAULT == "crash" and random.random() < FAULT_RATE:
            log("CRITICAL", "request-time crash (FAULT=crash)", path=self.path)
            os._exit(1)

        self._send(200, {"status": "ok", "revision": REVISION, "served_at": time.time()})

    # 既定のアクセスログ（stderr）を抑制（log() に統一）
    def log_message(self, fmt, *args):
        return


def main():
    log("NOTICE", f"sample-service starting on :{PORT}", port=PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
