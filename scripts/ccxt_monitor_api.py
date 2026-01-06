#!/usr/bin/env python3
from __future__ import annotations

import os
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ccxt
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from freqtrade.ai_iteration import DEFAULT_SEGMENT, AiIterationTuner


app = FastAPI()
_MARKET_CACHE: dict[str, Any] = {"ts": 0.0, "data": {}, "error": None}


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


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize_symbol(raw_symbol: str) -> str:
    symbol = raw_symbol.strip() if raw_symbol else "BTC"
    if "/" in symbol:
        return symbol
    quote = os.getenv("MONITOR_QUOTE", "USDT").strip().upper()
    base = symbol.upper()
    return f"{base}/{quote}"


def _build_exchange() -> ccxt.Exchange:
    exchange_id = os.getenv("MONITOR_EXCHANGE", "binance").strip().lower()
    if not hasattr(ccxt, exchange_id):
        raise ValueError(f"Unsupported exchange: {exchange_id}")
    config: dict[str, Any] = {"enableRateLimit": True}
    api_key = os.getenv("MONITOR_API_KEY", "").strip()
    api_secret = os.getenv("MONITOR_API_SECRET", "").strip()
    api_password = os.getenv("MONITOR_API_PASSWORD", "").strip()
    default_type = os.getenv("MONITOR_DEFAULT_TYPE", "").strip()
    if api_key:
        config["apiKey"] = api_key
    if api_secret:
        config["secret"] = api_secret
    if api_password:
        config["password"] = api_password
    if default_type:
        config.setdefault("options", {})["defaultType"] = default_type
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class(config)
    if _env_bool("MONITOR_SANDBOX", False):
        exchange.set_sandbox_mode(True)
    return exchange


def _ema(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    ema_value = sum(values[:period]) / period
    multiplier = 2 / (period + 1)
    for value in values[period:]:
        ema_value = (value * multiplier) + (ema_value * (1 - multiplier))
    return ema_value


def _atr_pct(ohlcv: list[list[float]], window: int) -> float:
    if len(ohlcv) < 2 or window <= 0:
        return 0.0
    trs: list[float] = []
    prev_close = ohlcv[0][4]
    for candle in ohlcv[1:]:
        high = candle[2]
        low = candle[3]
        close = candle[4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close
    if not trs:
        return 0.0
    window = min(window, len(trs))
    atr = sum(trs[-window:]) / window
    last_close = ohlcv[-1][4] or 0.0
    if not last_close:
        return 0.0
    return atr / last_close


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


def _fetch_market_metrics() -> tuple[dict[str, Any], str | None]:
    exchange = _build_exchange()
    try:
        symbol_raw = os.getenv("MONITOR_SYMBOL", "BTC")
        symbol = _normalize_symbol(symbol_raw)
        timeframe = os.getenv("MONITOR_TIMEFRAME", "5m").strip() or "5m"
        limit = int(os.getenv("MONITOR_LIMIT", "120"))
        price_type = os.getenv("MONITOR_PRICE_TYPE", "").strip()
        params: dict[str, Any] = {}
        if price_type:
            params["price"] = price_type
        exchange.load_markets()
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, None, limit, params)
        if not ohlcv:
            raise RuntimeError("No OHLCV data returned.")

        closes = [candle[4] for candle in ohlcv]
        last_close = closes[-1]

        ema_fast_period = int(os.getenv("MONITOR_EMA_FAST", "9"))
        ema_slow_period = int(os.getenv("MONITOR_EMA_SLOW", "21"))
        momentum_bars = int(os.getenv("MONITOR_MOMENTUM_BARS", "12"))
        volatility_bars = int(os.getenv("MONITOR_VOLATILITY_BARS", "14"))

        ema_fast = _ema(closes, ema_fast_period)
        ema_slow = _ema(closes, ema_slow_period)
        trend_bias = 0.0
        if ema_fast is not None and ema_slow is not None and last_close:
            trend_bias = (ema_fast - ema_slow) / last_close

        momentum = 0.0
        if len(closes) > momentum_bars:
            reference = closes[-1 - momentum_bars]
            if reference:
                momentum = (last_close - reference) / reference

        volatility = _atr_pct(ohlcv, volatility_bars)
        updated_at = datetime.fromtimestamp(ohlcv[-1][0] / 1000, tz=UTC).isoformat()

        payload = {
            "trend_bias": trend_bias,
            "momentum": momentum,
            "volatility": volatility,
            "symbol": symbol,
            "exchange": exchange.id,
            "timeframe": timeframe,
            "updated_at": updated_at,
        }
        return payload, None
    except Exception as exc:
        return {}, str(exc)
    finally:
        if hasattr(exchange, "close"):
            exchange.close()


def _cached_market_metrics() -> tuple[dict[str, Any], str | None]:
    ttl = int(os.getenv("MONITOR_CACHE_TTL", "10"))
    now = time.monotonic()
    if _MARKET_CACHE["data"] and now - _MARKET_CACHE["ts"] < ttl:
        return _MARKET_CACHE["data"], _MARKET_CACHE["error"]
    data, error = _fetch_market_metrics()
    if data:
        _MARKET_CACHE["data"] = data
    _MARKET_CACHE["error"] = error
    _MARKET_CACHE["ts"] = now
    return _MARKET_CACHE["data"], _MARKET_CACHE["error"]


@app.get("/api/monitor")
def monitor() -> JSONResponse:
    refresh_window = int(os.getenv("MONITOR_REFRESH", "15"))
    stats = _load_iteration_stats()
    market_data, market_error = _cached_market_metrics()
    trend_bias = float(market_data.get("trend_bias", 0.0) or 0.0)
    momentum = float(market_data.get("momentum", 0.0) or 0.0)
    volatility = float(market_data.get("volatility", 0.0) or 0.0)
    win_rate = stats["wins"] / max(1, stats["wins"] + stats["losses"])

    alerts = []
    if market_error:
        alerts.append("Market data unavailable")
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
        "symbol": market_data.get("symbol"),
        "exchange": market_data.get("exchange"),
        "timeframe": market_data.get("timeframe"),
        "market_updated_at": market_data.get("updated_at"),
        "market_error": market_error,
    }
    return JSONResponse(payload)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("MONITOR_HOST", "127.0.0.1")
    port = int(os.getenv("MONITOR_PORT", "9010"))
    uvicorn.run(app, host=host, port=port)
