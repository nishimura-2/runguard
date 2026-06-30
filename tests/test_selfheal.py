"""selfheal の hermetic 単体テスト（実 Gemini 不要）。

- generate_fix: LLM が有効な CodeFix を返せばそれを使う / 失敗・空ならフォールバック修正へ退避。
- compute_diff: 0除算ガード追加が unified diff に現れる。
- RuleBasedLLM（診断用）に CodeFix スキーマを渡しても安全にフォールバックする。
"""
import unittest

from agent.gemini_client import RuleBasedLLM
from agent.models import CodeFix
from agent.selfheal import (
    BUGGY_FEATURE_SOURCE,
    EXPECTED_FIXED_SOURCE,
    FEATURE_BUG_LOGS,
    build_fix_and_diff,
    compute_diff,
    generate_fix,
)


class FixLLM:
    def __init__(self, fix):
        self._f = fix

    def generate_structured(self, *, prompt, schema, system_instruction=None):
        return self._f


class BoomLLM:
    def generate_structured(self, **kw):
        raise RuntimeError("gemini down")


class TestComputeDiff(unittest.TestCase):
    def test_diff_shows_guard(self):
        diff = compute_diff(BUGGY_FEATURE_SOURCE, EXPECTED_FIXED_SOURCE)
        self.assertIn("+", diff)
        self.assertIn("qty <= 0", diff)            # 追加されたガード行が現れる
        self.assertTrue(any(l.startswith("@@") for l in diff.splitlines()))

    def test_no_diff_when_identical(self):
        self.assertEqual(compute_diff("x\n", "x\n"), "")


class TestGenerateFix(unittest.TestCase):
    def test_uses_valid_llm_fix(self):
        good = CodeFix(summary="s", bug_explanation="b",
                       fixed_source="def handle_price(a, b):\n    return a\n", kept_feature=True)
        out = generate_fix(BUGGY_FEATURE_SOURCE, FEATURE_BUG_LOGS, FixLLM(good))
        self.assertEqual(out.fixed_source, good.fixed_source)
        self.assertTrue(out.kept_feature)

    def test_fallback_on_llm_error(self):
        out = generate_fix(BUGGY_FEATURE_SOURCE, FEATURE_BUG_LOGS, BoomLLM())
        self.assertEqual(out.fixed_source, EXPECTED_FIXED_SOURCE)
        self.assertTrue(out.kept_feature)

    def test_fallback_on_empty_fix(self):
        empty = CodeFix(summary="", bug_explanation="", fixed_source="   ", kept_feature=True)
        out = generate_fix(BUGGY_FEATURE_SOURCE, FEATURE_BUG_LOGS, FixLLM(empty))
        self.assertEqual(out.fixed_source, EXPECTED_FIXED_SOURCE)

    def test_fallback_on_unchanged_fix(self):
        same = CodeFix(fixed_source=BUGGY_FEATURE_SOURCE)
        out = generate_fix(BUGGY_FEATURE_SOURCE, FEATURE_BUG_LOGS, FixLLM(same))
        self.assertEqual(out.fixed_source, EXPECTED_FIXED_SOURCE)

    def test_rulebased_llm_falls_back_safely(self):
        # 診断用 RuleBasedLLM に CodeFix を求めても落ちず、フォールバック修正になる。
        out = generate_fix(BUGGY_FEATURE_SOURCE, FEATURE_BUG_LOGS, RuleBasedLLM())
        self.assertEqual(out.fixed_source, EXPECTED_FIXED_SOURCE)

    def test_build_fix_and_diff(self):
        fix, diff = build_fix_and_diff(BUGGY_FEATURE_SOURCE, FEATURE_BUG_LOGS, BoomLLM())
        self.assertEqual(fix.fixed_source, EXPECTED_FIXED_SOURCE)
        self.assertIn("qty <= 0", diff)


if __name__ == "__main__":
    unittest.main()
