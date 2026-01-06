from pathlib import Path

from freqtrade.ai_iteration import AiFeedbackStore, AiIterationTuner


def test_ai_tuner_updates_segment_state(tmp_path: Path) -> None:
    state_path = tmp_path / "ai_state.json"
    tuner = AiIterationTuner(state_path, min_trades=2, profit_threshold=0.0)

    tuner.update_from_trade(
        "long",
        0.05,
        {
            "rsi": 58,
            "macd_hist": 0.02,
            "ema_diff": 0.004,
        },
        segment="main",
    )
    tuner.update_from_trade(
        "long",
        -0.02,
        {
            "rsi": 42,
            "macd_hist": -0.01,
            "ema_diff": -0.002,
        },
        segment="main",
    )

    thresholds = tuner.compute_thresholds(
        defaults={"rsi_long": 55, "rsi_short": 45, "macd_hist": 0.0, "ema_diff": 0.0},
        bounds={
            "rsi_long": (50, 70),
            "rsi_short": (30, 50),
            "macd_hist": (-0.05, 0.05),
            "ema_diff": (0.0, 0.01),
        },
        segment="main",
    )
    assert "rsi_long" in thresholds
    assert "macd_hist" in thresholds


def test_ai_feedback_store_writes_payload(tmp_path: Path) -> None:
    feedback_path = tmp_path / "feedback.jsonl"
    store = AiFeedbackStore(feedback_path)
    store.append_trade_feedback({"trade_id": 123, "profit_ratio": 0.03})

    content = feedback_path.read_text(encoding="utf-8").strip()
    assert "\"trade_id\": 123" in content


def test_ai_tuner_replay_feedback(tmp_path: Path) -> None:
    state_path = tmp_path / "ai_state.json"
    feedback_path = tmp_path / "feedback.jsonl"
    feedback_path.write_text(
        "\n".join(
            [
                '{"side":"long","profit_ratio":0.03,"segment":"main","entry_features":{"rsi":60}}',
                '{"side":"short","profit_ratio":-0.02,"segment":"main","entry_features":{"rsi":40}}',
            ]
        ),
        encoding="utf-8",
    )

    tuner = AiIterationTuner(state_path, min_trades=1, profit_threshold=0.0)
    updated = tuner.replay_feedback(feedback_path)
    assert updated == 2
    assert tuner.get_segment_summary("main")["wins"] + tuner.get_segment_summary("main")[
        "losses"
    ] == 2
