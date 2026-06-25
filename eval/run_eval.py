#!/usr/bin/env python3
"""run_eval — 合成インシデントで diagnose/decide を評価し指標を出力する（GCP 不要・hermetic）。

- 正診率(diagnosis accuracy): オフライン RuleBasedLLM の診断カテゴリが期待と一致した割合。
- 正アクション率(action acc): 期待診断を入力した decide のアクションが期待と一致した割合（判断ロジックの正しさ）。
- 誤動作率(false-action): 期待が rollback でないのに自動ロールバック(requires_human=False)してしまった割合。
- 想定MTTR: 自動復旧=短時間 / エスカレーション=人対応 と仮定した平均（分）。

`python eval/run_eval.py` で実行。CI 合格ラインを満たさなければ非ゼロ終了。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))            # agent.* を import 可能に

# Windows コンソール(cp932)でも落ちないよう UTF-8 出力に統一（CI/Linux はそのまま）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import yaml  # noqa: E402

from agent.config import Config  # noqa: E402
from agent.decide import decide  # noqa: E402
from agent.diagnose import diagnose  # noqa: E402
from agent.gemini_client import RuleBasedLLM  # noqa: E402
from agent.models import Diagnosis  # noqa: E402
from mocks import ScriptedLLM, build_observation  # noqa: E402

AUTO_MTTR_MIN = 1.5
MANUAL_MTTR_MIN = 30.0

# CI 合格ライン
MIN_DIAG_ACC = 0.70
MIN_ACTION_ACC = 0.95
MAX_FALSE_ACTION = 0.0


def load_cases():
    with open(ROOT / "eval" / "incidents.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["incidents"]


def run() -> int:
    cfg = Config()
    cases = load_cases()
    rules = RuleBasedLLM()

    diag_hits = act_hits = false_actions = 0
    mttr_total = 0.0
    rows = []

    for c in cases:
        obs = build_observation(c["observation"])

        diag_pred = diagnose(obs, rules)
        diag_ok = diag_pred.category.value == c["expected_category"]

        scripted = ScriptedLLM(
            Diagnosis(category=c["expected_category"],
                      confidence=float(c["diagnosis_confidence"]))
        )
        dec = decide(diagnose(obs, scripted), obs, cfg)
        act_ok = dec.action.value == c["expected_action"]
        is_auto_rollback = dec.action.value == "rollback" and not dec.requires_human
        false_action = is_auto_rollback and c["expected_action"] != "rollback"

        diag_hits += int(diag_ok)
        act_hits += int(act_ok)
        false_actions += int(false_action)
        mttr_total += AUTO_MTTR_MIN if (is_auto_rollback and act_ok) else MANUAL_MTTR_MIN

        rows.append((c["name"], diag_pred.category.value, c["expected_category"], diag_ok,
                     dec.action.value, c["expected_action"], act_ok, false_action))

    n = len(cases)
    diag_acc, act_acc, false_rate = diag_hits / n, act_hits / n, false_actions / n
    mttr = mttr_total / n

    print(f"\nRunGuard eval — {n} 合成インシデント")
    print("-" * 78)
    for name, dc, dexp, dok, ac, aexp, aok, fa in rows:
        d = "OK" if dok else "XX"
        a = "OK" if aok else "XX"
        flag = " !FALSE-ACTION" if fa else ""
        print(f"  {name:26} diag {dc:14}[{d}]  act {ac:9}->{aexp:9}[{a}]{flag}")
    print("-" * 78)
    print(f"  正診率(diagnosis accuracy) : {diag_acc:.1%}  ({diag_hits}/{n})")
    print(f"  正アクション率(action acc) : {act_acc:.1%}  ({act_hits}/{n})")
    print(f"  誤動作率(false-action)     : {false_rate:.1%}  ({false_actions}/{n})")
    print(f"  想定MTTR(RunGuard)         : {mttr:.1f} 分  "
          f"(全件手動 {MANUAL_MTTR_MIN:.0f} 分 → {(1 - mttr / MANUAL_MTTR_MIN):.0%} 短縮)")

    ok = (diag_acc >= MIN_DIAG_ACC and act_acc >= MIN_ACTION_ACC and false_rate <= MAX_FALSE_ACTION)
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'} "
          f"(基準: 正診率>={MIN_DIAG_ACC:.0%} / 正アクション率>={MIN_ACTION_ACC:.0%} / 誤動作率<={MAX_FALSE_ACTION:.0%})\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
