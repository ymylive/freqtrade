from pathlib import Path

import pytest

from freqtrade.valuescan_api.ai_tuner import ValueScanAITuner, ValueScanFeedbackStore


def test_ai_tuner_thresholds(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    tuner = ValueScanAITuner(state_path, min_trades=2, profit_threshold=0.0)
    defaults = {
        "buy_signal_strength": 0.25,
        "sell_signal_strength": -0.25,
        "bullish_ratio_threshold": 55,
        "bearish_ratio_threshold": 55,
    }
    bounds = {
        "buy_signal_strength": (0.1, 0.7),
        "sell_signal_strength": (-0.7, -0.05),
        "bullish_ratio_threshold": (45, 75),
        "bearish_ratio_threshold": (45, 75),
    }

    thresholds = tuner.compute_thresholds(defaults=defaults, bounds=bounds)
    assert thresholds == defaults

    tuner.update_from_trade(
        "long",
        0.05,
        {"signal_strength": 0.6, "bullish_ratio": 70, "total_inflow_1h": 120},
        segment="main",
    )
    tuner.update_from_trade(
        "long",
        -0.02,
        {"signal_strength": 0.2, "bullish_ratio": 45, "total_inflow_1h": -50},
        segment="main",
    )
    tuner.update_from_trade(
        "short",
        0.04,
        {"signal_strength": -0.6, "bearish_ratio": 70, "total_inflow_1h": -90},
        segment="main",
    )
    tuner.update_from_trade(
        "short",
        -0.03,
        {"signal_strength": -0.2, "bearish_ratio": 45, "total_inflow_1h": 30},
        segment="main",
    )

    thresholds = tuner.compute_thresholds(defaults=defaults, bounds=bounds, segment="main")
    assert thresholds["buy_signal_strength"] == pytest.approx(0.4)
    assert thresholds["sell_signal_strength"] == pytest.approx(-0.4)
    assert thresholds["bullish_ratio_threshold"] == pytest.approx(57.5)
    assert thresholds["bearish_ratio_threshold"] == pytest.approx(57.5)

    summary = tuner.get_segment_summary("main")
    assert summary["wins"] == 2
    assert summary["losses"] == 2


def test_feedback_store_append(tmp_path: Path) -> None:
    feedback_path = tmp_path / "feedback.jsonl"
    store = ValueScanFeedbackStore(feedback_path)
    store.append_trade_feedback({"pair": "BTC/USDT", "profit_ratio": 0.02})
    content = feedback_path.read_text(encoding="utf-8").strip()
    assert content.startswith("{")
    assert '"pair": "BTC/USDT"' in content
