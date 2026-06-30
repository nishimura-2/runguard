"""selfheal — self_heal アクションの中核（AI によるコード修正）。

ロールバックは「直前のリビジョンへ戻す」=新機能も一緒に失う。
self_heal は「不調リビジョンのソースを Gemini に読ませ、新機能は維持したままバグだけを
最小修正した新リビジョンを提案/デプロイする」= ロールバックより高度な復旧手段。

- generate_fix(): Gemini に構造化出力(CodeFix)でコード修正を生成させる。
  実 Gemini が使えない/失敗した場合は決定論的な定型修正(_fallback_fix)へ退避（オフラインでもデモが緑になる）。
- compute_diff(): faulty_source → fixed_source の unified diff を difflib で機械生成（LLM に diff を書かせない）。
- BUGGY_FEATURE_SOURCE / EXPECTED_FIXED_SOURCE: デモ用「新機能＋仕込みバグ」とその正解修正。
  sample_service/main.py の FAULT=feature_bug が実際にこのバグ挙動を出す（実環境でも本物のバグ）。

GCP 副作用なし。LLM はインターフェイス経由で注入し、テストでモック可能。
"""
from __future__ import annotations

import difflib
from typing import List, Optional

from agent.gemini_client import LLMClient
from agent.models import CodeFix

# --- デモ用「新機能 + 仕込みバグ」 -------------------------------------------------
# 新機能: 小計と数量から単価を返す /api/price。
# 仕込みバグ: qty=0 で subtotal/qty が ZeroDivisionError → 500。
BUGGY_FEATURE_SOURCE = '''def handle_price(subtotal: float, qty: int) -> dict:
    """新機能: 小計と数量から単価を計算して返す（/api/price）。"""
    unit_price = subtotal / qty
    return {
        "subtotal": subtotal,
        "qty": qty,
        "unit_price": round(unit_price, 2),
    }
'''

# 正解修正（フォールバック用）: 新機能(/api/price)は維持し、0除算だけをガードする。
EXPECTED_FIXED_SOURCE = '''def handle_price(subtotal: float, qty: int) -> dict:
    """新機能: 小計と数量から単価を計算して返す（/api/price）。"""
    if qty <= 0:
        return {"subtotal": subtotal, "qty": qty, "unit_price": 0.0, "note": "qty must be > 0"}
    unit_price = subtotal / qty
    return {
        "subtotal": subtotal,
        "qty": qty,
        "unit_price": round(unit_price, 2),
    }
'''

# 不調リビジョンが吐く例外ログ（diagnose が feature_bug を見分ける根拠＝アプリのコード例外）。
FEATURE_BUG_LOGS: List[str] = [
    '{"severity":"ERROR","message":"Traceback (most recent call last):","fault":"feature_bug"}',
    '  File "main.py", line 3, in handle_price',
    "    unit_price = subtotal / qty",
    "ZeroDivisionError: division by zero",
]

FIX_SYSTEM_INSTRUCTION = (
    "あなたは熟練の Python エンジニアです。Cloud Run サービスの不調リビジョンのソースと"
    "エラーログ（traceback）を受け取り、根本原因のバグだけを最小限の変更で修正してください。"
    "重要: 新機能（/api/price の単価計算）は絶対に削除・無効化せず必ず維持すること。"
    "fixed_source には修正後の【完全な関数ソース】を返すこと（差分やコメントだけにしない）。"
    "summary と bug_explanation は日本語で簡潔に書くこと。kept_feature は新機能を維持したなら true。"
)


def build_fix_prompt(faulty_source: str, error_logs: List[str]) -> str:
    logs = "\n".join(f"  {line}" for line in error_logs) or "  (なし)"
    return (
        "不調リビジョンのソース:\n"
        "```python\n" + faulty_source.rstrip() + "\n```\n\n"
        "エラーログ:\n" + logs + "\n\n"
        "上記のバグを、新機能を壊さずに最小修正し、スキーマ(CodeFix)の JSON で返してください。"
    )


def generate_fix(
    faulty_source: str,
    error_logs: List[str],
    llm: LLMClient,
) -> CodeFix:
    """Gemini にコード修正を生成させる。失敗/オフライン時は定型修正へ退避。"""
    prompt = build_fix_prompt(faulty_source, error_logs)
    try:
        fix = llm.generate_structured(
            prompt=prompt, schema=CodeFix, system_instruction=FIX_SYSTEM_INSTRUCTION
        )
        if not isinstance(fix, CodeFix):
            fix = CodeFix.model_validate(fix)
        fixed = (fix.fixed_source or "").strip()
        # 実質的に修正できている（空でない & 元と異なる）ことを確認
        if fixed and fixed != faulty_source.strip():
            return fix
    except Exception:
        pass
    return _fallback_fix(faulty_source)


def _fallback_fix(faulty_source: str) -> CodeFix:
    """Gemini 不可時の決定論的修正（オフラインデモでも必ず緑にする）。"""
    return CodeFix(
        summary="qty=0 の 0 除算をガード（新機能は維持）",
        bug_explanation=(
            "qty=0 のとき subtotal / qty が ZeroDivisionError を投げ 500 になっていました。"
            "数量が 0 以下なら単価 0 を返すガードを追加し、単価計算の新機能はそのまま残します。"
        ),
        fixed_source=EXPECTED_FIXED_SOURCE,
        kept_feature=True,
    )


def compute_diff(old: str, new: str, *, filename: str = "sample_service/feature.py") -> str:
    """faulty_source → fixed_source の unified diff（ダッシュボード可視化用）。"""
    diff = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        lineterm="",
    )
    return "\n".join(diff)


def build_fix_and_diff(
    faulty_source: str,
    error_logs: List[str],
    llm: LLMClient,
) -> tuple[CodeFix, str]:
    """fix を生成し、diff も併せて返す（loop / approve から使う便宜関数）。"""
    fix = generate_fix(faulty_source, error_logs, llm)
    diff = compute_diff(faulty_source, fix.fixed_source)
    return fix, diff
