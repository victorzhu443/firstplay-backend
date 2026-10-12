from app.autofill.tune import sweep


def test_sweep_reports_precision_and_recall_per_threshold_and_excludes_unjudged():
    rows = [
        {"confidence": 0.95, "verdict": "correct"}, {"confidence": 0.90, "verdict": "correct"},
        {"confidence": 0.80, "verdict": "wrong"}, {"confidence": 0.70, "verdict": "correct"},
        {"confidence": 0.99, "verdict": None},
    ]
    table, judged, unjudged = sweep(rows, thresholds=(0.6, 0.85))
    assert (judged, unjudged) == (4, 1)
    low, high = table
    assert (low["accepted"], low["correct"], low["wrong"]) == (4, 3, 1) and not low["meets_target"]
    assert (high["accepted"], high["correct"], high["wrong"]) == (2, 2, 0) and high["meets_target"]
    assert abs(high["recall_of_correct"] - 2 / 3) < 1e-9
