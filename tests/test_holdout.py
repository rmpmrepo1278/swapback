"""evals/holdout3.json: the patterns-only score is a regression floor, not a target (see evals/run_holdout.py)."""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evals"))
import run_holdout  # noqa: E402


def test_holdout3_patterns_only():
    s = run_holdout.summary(run_holdout.score())
    assert s["cases"] == 64 and s["positives"] == 42 and s["negatives"] == 22
    assert s["negatives_untouched"] == 22            # no over-redaction of the negative cases
    assert s["round_trip"] == 64 and s["utility"] == 64
    assert s["caught"] >= 16                         # 0.1.1 published score; a drop is a regression
