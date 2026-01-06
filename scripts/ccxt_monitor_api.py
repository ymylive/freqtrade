#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from freqtrade.ai_iteration import DEFAULT_SEGMENT, AiIterationTuner


app = FastAPI()


def _env_list(value: str) -> list[str]:
    if not value:
        return []
    raw = value.replace(";", ",").replace("\n", ",")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _load_tuners(paths: Iterable[str]) -> list[AiIterationTuner]:
    tuners: list[AiIterationTuner] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        tuners.append(AiIterationTuner(path, min_trades=1, profit_threshold=0.0))
    return tuners


def _extract_tuned_params(state: dict[str, Any]) -> int:
    segments = state.get("segments") or {}
    keys: set[str] = set()
    for segment in segments.values():
        if not isinstance(segment, dict):
            continue
        for side in ("long", "short"):
            stats = segment.get(side, {})
            if not isinstance(stats, dict):
                continue
            keys.update((stats.get("mean_win") or {}).keys())
            keys.update((stats.get("mean_loss") or {}).keys())
    return len(keys)


def _load_iteration_stats() -> dict[str, Any]:
    state_files = _env_list(os.getenv("MONITOR_STATE_FILES", ""))
    if not state_files:
        state_files = [
            "user_data/ai_iteration_state_main.json",
            "user_data/ai_iteration_state_alt.json",
        ]
    total_wins = 0
    total_losses = 0
    tuned_params = 0
    updated_at = None

    for tuner in _load_tuners(state_files):
        summary = tuner.get_segment_summary(DEFAULT_SEGMENT)
        total_wins += summary["wins"]
        total_losses += summary["losses"]
        tuned_params = max(tuned_params, _extract_tuned_params(tuner._state))
        if summary.get("updated_at"):
            updated_at = summary.get("updated_at")

    return {
        "wins": total_wins,
        "losses": total_losses,
        "tuned_params": tuned_params,
        "updated_at": updated_at,
    }


def _read_last_feedback(paths: Iterable[str]) -> dict[str, Any]:
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            if not lines:
                continue
            payload = json.loads(lines[-1])
            if isinstance(payload, dict):
                return payload
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return {}


@app.get("/api/monitor")
def monitor() -> JSONResponse:
    refresh_window = int(os.getenv("MONITOR_REFRESH", "15"))
    feedback_files = _env_list(os.getenv("MONITOR_FEEDBACK_FILES", ""))
    if not feedback_files:
        feedback_files = [
            "user_data/ai_iteration_feedback_main.jsonl",
            "user_data/ai_iteration_feedback_alt.jsonl",
        ]
    stats = _load_iteration_stats()
    last_feedback = _read_last_feedback(feedback_files)
    if isinstance(last_feedback, dict):
        entry_features = last_feedback.get("entry_features", {})
    else:
        entry_features = {}

    trend_bias = float(entry_features.get("ema_diff", 0.0) or 0.0)
    momentum = float(entry_features.get("price_change_1h", 0.0) or 0.0)
    volatility = float(entry_features.get("atr_pct", 0.0) or 0.0)
    win_rate = stats["wins"] / max(1, stats["wins"] + stats["losses"])

    alerts = []
    if abs(trend_bias) > 0.003:
        alerts.append("Trend shift detected")
    if volatility > 0.02:
        alerts.append("Volatility spike")
    if not alerts:
        alerts.append("Signals stable")

    payload = {
        "trend_bias": trend_bias,
        "momentum": momentum,
        "volatility": volatility,
        "signal_health": round(win_rate * 100, 2),
        "refresh_window": refresh_window,
        "tuned_params": stats["tuned_params"],
        "trades_evaluated": stats["wins"] + stats["losses"],
        "alerts": alerts,
    }
    return JSONResponse(payload)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("MONITOR_HOST", "127.0.0.1")
    port = int(os.getenv("MONITOR_PORT", "9010"))
    uvicorn.run(app, host=host, port=port)
