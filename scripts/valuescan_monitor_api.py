#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from freqtrade.valuescan_api import get_provider
from freqtrade.valuescan_api.ai_tuner import DEFAULT_SEGMENT, ValueScanAITuner


app = FastAPI()


def _env_list(value: str) -> list[str]:
    if not value:
        return []
    raw = value.replace(";", ",").replace("\n", ",")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _load_tuners(paths: Iterable[str]) -> list[ValueScanAITuner]:
    tuners: list[ValueScanAITuner] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        tuners.append(ValueScanAITuner(path, min_trades=1, profit_threshold=0.0))
    return tuners


def _sum_stats(stats: Dict[str, Any]) -> tuple[int, int]:
    wins = int(stats.get("wins", 0))
    losses = int(stats.get("losses", 0))
    return wins, losses


def _extract_tuned_params(state: Dict[str, Any]) -> int:
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
            "user_data/valuescan_iteration_state_main.json",
            "user_data/valuescan_iteration_state_alt.json",
        ]
    total_wins = 0
    total_losses = 0
    tuned_params = 0
    updated_at = None

    for tuner in _load_tuners(state_files):
        summary = tuner.get_segment_summary(DEFAULT_SEGMENT)
        total_wins += summary["wins"]
        total_losses += summary["losses"]
        tuned_params = max(tuned_params, _extract_tuned_params(tuner._state))  # noqa: SLF001
        if summary.get("updated_at"):
            updated_at = summary.get("updated_at")

    return {
        "wins": total_wins,
        "losses": total_losses,
        "tuned_params": tuned_params,
        "updated_at": updated_at,
    }


@app.get("/api/monitor")
def monitor() -> JSONResponse:
    symbol = os.getenv("MONITOR_SYMBOL", "BTC").upper()
    refresh_window = int(os.getenv("MONITOR_REFRESH", "15"))

    provider = get_provider()
    signal_strength = provider.get_signal_strength(symbol)
    ai = provider.get_coin_ai_analysis(symbol)
    bullish_ratio = 0.0
    bearish_ratio = 0.0
    if isinstance(ai, dict) and ai.get("code") == 200:
        ai_data = ai.get("data", {})
        bullish_ratio = float(ai_data.get("bullishRatio", 0.0) or 0.0)
        bearish_ratio = float(ai_data.get("bearishRatio", 0.0) or 0.0)

    stats = _load_iteration_stats()
    alerts = []
    if signal_strength > 0.4:
        alerts.append("Flow bias positive")
    if bullish_ratio >= 60:
        alerts.append("Bullish ratio elevated")
    if bearish_ratio >= 60:
        alerts.append("Bearish ratio elevated")
    if not alerts:
        alerts.append("Signals stable")

    payload = {
        "flow_bias": signal_strength,
        "whale_pressure": bullish_ratio / 100.0,
        "sentiment": bullish_ratio,
        "signal_health": 100.0 if abs(signal_strength) > 0 else 60.0,
        "refresh_window": refresh_window,
        "tuned_params": stats["tuned_params"],
        "trades_evaluated": stats["wins"] + stats["losses"],
        "alerts": alerts,
    }
    return JSONResponse(payload)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("MONITOR_PORT", "9010"))
    uvicorn.run(app, host="0.0.0.0", port=port)
