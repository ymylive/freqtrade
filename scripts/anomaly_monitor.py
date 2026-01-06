#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import ccxt
import numpy as np
import requests
from telegram_notify import send_telegram_message


logger = logging.getLogger(__name__)


ALPHA_API_URL = (
    "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
)
FUTURES_API_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"


@dataclass
class AnomalySignal:
    symbol: str
    signal_type: str
    severity: str
    details: dict[str, Any]
    timestamp: str


class BinanceAlphaCache:
    def __init__(self, cache_file: Path, refresh_minutes: int = 60) -> None:
        self.cache_file = cache_file
        self.refresh_seconds = refresh_minutes * 60
        self._last_update = 0.0
        self._tokens: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.cache_file.exists():
            return
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            tokens = data.get("tokens", [])
            self._tokens = {str(token).upper() for token in tokens}
            self._last_update = float(data.get("timestamp", 0.0) or 0.0)
        except (OSError, json.JSONDecodeError, ValueError):
            logger.warning("Alpha cache load failed.")

    def _save(self) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": self._last_update,
            "tokens": sorted(self._tokens),
        }
        self.cache_file.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def refresh_if_needed(self) -> None:
        now = time.time()
        if self._tokens and (now - self._last_update) < self.refresh_seconds:
            return
        self.refresh()

    def refresh(self) -> None:
        alpha = self._fetch_alpha_tokens()
        futures = self._fetch_futures_tokens()
        if not alpha or not futures:
            logger.warning("Alpha cache refresh skipped (empty data).")
            return
        self._tokens = alpha & futures
        self._last_update = time.time()
        self._save()

    def is_alpha(self, symbol: str) -> bool:
        base = symbol.split("/")[0].split(":")[0].upper()
        return base in self._tokens

    def _fetch_alpha_tokens(self) -> set[str]:
        try:
            response = requests.get(ALPHA_API_URL, timeout=20, proxies=_get_proxies())
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            logger.warning("Alpha token fetch failed: %s", exc)
            return set()
        if data.get("code") != "000000":
            return set()
        tokens = set()
        for item in data.get("data", []):
            symbol = item.get("cexCoinName") or item.get("symbol")
            if symbol:
                tokens.add(str(symbol).upper().strip())
        return tokens

    def _fetch_futures_tokens(self) -> set[str]:
        try:
            response = requests.get(FUTURES_API_URL, timeout=20, proxies=_get_proxies())
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            logger.warning("Futures token fetch failed: %s", exc)
            return set()
        symbols = set()
        for item in data.get("symbols", []):
            status = item.get("status") or item.get("contractStatus")
            if status != "TRADING":
                continue
            contract_type = str(item.get("contractType", "")).upper()
            if contract_type != "PERPETUAL":
                continue
            base = item.get("baseAsset")
            if base:
                symbols.add(str(base).upper().strip())
        return symbols


class AnomalyMonitor:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.exchange_id = config.get("exchange", "binance")
        exchange_config = config.get("exchange_config", {})
        if not isinstance(exchange_config, dict):
            exchange_config = {}
        self.exchange = getattr(ccxt, self.exchange_id)(
            {
                "enableRateLimit": True,
                **exchange_config,
            }
        )
        market_cfg = config.get("market", {})
        default_type = (
            market_cfg.get("default_type")
            if isinstance(market_cfg, dict)
            else None
        )
        if default_type:
            self.exchange.options["defaultType"] = default_type
        self.exchange.load_markets()

        alpha_cfg = config.get("alpha", {})
        cache_path = Path(alpha_cfg.get("cache_file", "user_data/anomaly/binance_alpha_cache.json"))
        refresh_minutes = int(alpha_cfg.get("refresh_minutes", 60))
        self.alpha_cache = BinanceAlphaCache(cache_path, refresh_minutes=refresh_minutes)

        runtime = config.get("runtime", {})
        self.state_file = Path(runtime.get("state_file", "user_data/anomaly/anomaly_state.json"))
        self.report_state_file = Path(
            runtime.get("report_state_file", "user_data/anomaly/report_state.json")
        )
        self.log_file = Path(runtime.get("log_file", "user_data/anomaly/anomaly_log.jsonl"))
        self.alert_cooldown = int(runtime.get("alert_cooldown_seconds", 300))
        self.report_interval = int(runtime.get("report_interval_minutes", 20))
        self.reports_dir = Path(runtime.get("reports_dir", "user_data/anomaly/reports"))

    def run_once(self) -> None:
        self.alpha_cache.refresh_if_needed()
        whale_report = _load_json(self.config.get("data_sources", {}).get("whale_report", ""))
        retail_report = _load_json(self.config.get("data_sources", {}).get("retail_report", ""))
        symbols = self._resolve_symbols(whale_report, retail_report)

        anomalies: list[AnomalySignal] = []
        for symbol in symbols:
            anomalies.extend(self._detect_symbol(symbol, whale_report, retail_report))

        self._emit_alerts(anomalies)
        self._generate_reports(symbols, whale_report, retail_report, anomalies)

    def _resolve_symbols(
        self,
        whale_report: dict[str, Any],
        retail_report: dict[str, Any],
    ) -> list[str]:
        configured = [str(s).strip() for s in self.config.get("symbols", []) if str(s).strip()]
        if configured:
            return configured
        symbols = set()
        for source in (whale_report, retail_report):
            if isinstance(source, dict):
                symbols.update(source.keys())
        return sorted(symbols)

    def _detect_symbol(
        self,
        symbol: str,
        whale_report: dict[str, Any],
        retail_report: dict[str, Any],
    ) -> list[AnomalySignal]:
        now = datetime.now(UTC).isoformat()
        thresholds = self.config.get("thresholds", {})
        anomalies: list[AnomalySignal] = []

        alpha_hit = self.alpha_cache.is_alpha(symbol)
        if alpha_hit:
            anomalies.append(
                AnomalySignal(
                    symbol=symbol,
                    signal_type="alpha",
                    severity="info",
                    details={"alpha_intersection": True},
                    timestamp=now,
                )
            )

        whale = whale_report.get(symbol, {}) if isinstance(whale_report, dict) else {}
        whale_net_z = _safe_float(whale.get("whale_net_z"))
        whale_notional_z = _safe_float(whale.get("whale_notional_z"))
        whale_threshold = float(thresholds.get("whale_z", 2.5))
        if abs(whale_net_z) >= whale_threshold or whale_notional_z >= whale_threshold:
            anomalies.append(
                AnomalySignal(
                    symbol=symbol,
                    signal_type="fund_flow",
                    severity="high",
                    details={
                        "whale_net_z": whale_net_z,
                        "whale_notional_z": whale_notional_z,
                        "whale_buy_ratio": _safe_float(whale.get("whale_buy_ratio")),
                        "whale_net_notional": _safe_float(whale.get("whale_net_notional")),
                    },
                    timestamp=now,
                )
            )

        retail = retail_report.get(symbol, {}) if isinstance(retail_report, dict) else {}
        fomo_index = _safe_float(retail.get("fomo_index"))
        retail_buy_ratio = _safe_float(retail.get("retail_buy_ratio"))
        fomo_threshold = float(thresholds.get("fomo_index", 1.5))
        buy_ratio_threshold = float(thresholds.get("retail_buy_ratio", 0.7))
        if fomo_index >= fomo_threshold or retail_buy_ratio >= buy_ratio_threshold:
            anomalies.append(
                AnomalySignal(
                    symbol=symbol,
                    signal_type="fomo",
                    severity="high",
                    details={
                        "fomo_index": fomo_index,
                        "retail_buy_ratio": retail_buy_ratio,
                        "retail_volume_z": _safe_float(retail.get("retail_volume_z")),
                        "retail_count_z": _safe_float(retail.get("retail_count_z")),
                    },
                    timestamp=now,
                )
            )

        market = self._fetch_market_metrics(symbol)
        if market:
            momentum = market.get("momentum", 0.0)
            volume_z = market.get("volume_z", 0.0)
            momentum_threshold = float(thresholds.get("price_momentum", 0.02))
            volume_threshold = float(thresholds.get("volume_z", 2.0))
            if abs(momentum) >= momentum_threshold and abs(volume_z) >= volume_threshold:
                anomalies.append(
                    AnomalySignal(
                        symbol=symbol,
                        signal_type="momentum_volume",
                        severity="medium",
                        details={"momentum": momentum, "volume_z": volume_z},
                        timestamp=now,
                    )
                )

        return anomalies

    def _fetch_market_metrics(self, symbol: str) -> dict[str, float]:
        timeframe = self.config.get("market", {}).get("timeframe", "5m")
        limit = int(self.config.get("market", {}).get("limit", 120))
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, None, limit)
        except Exception as exc:
            logger.warning("Market fetch failed for %s: %s", symbol, exc)
            return {}
        if not ohlcv or len(ohlcv) < 5:
            return {}
        closes = [row[4] for row in ohlcv]
        volumes = [row[5] for row in ohlcv]
        momentum_bars = int(self.config.get("market", {}).get("momentum_bars", 12))
        if len(closes) <= momentum_bars:
            return {}
        last_close = closes[-1]
        prior = closes[-1 - momentum_bars]
        momentum = (last_close - prior) / prior if prior else 0.0
        volume_z = _last_mad_z(volumes)
        return {"momentum": float(momentum), "volume_z": float(volume_z)}

    def _emit_alerts(self, anomalies: list[AnomalySignal]) -> None:
        if not anomalies:
            return
        state = _load_json(self.state_file)
        now = time.time()
        sent = 0
        for signal in anomalies:
            key = f"{signal.symbol}:{signal.signal_type}"
            last_ts = float(state.get(key, 0.0) or 0.0)
            if now - last_ts < self.alert_cooldown:
                continue
            message = self._format_alert(signal)
            result = send_telegram_message(
                message,
                bot_token=self.config.get("telegram", {}).get("bot_token"),
                chat_id=self.config.get("telegram", {}).get("chat_id"),
            )
            if result:
                state[key] = now
                sent += 1
            _append_jsonl(self.log_file, signal.__dict__)
        if sent:
            _save_json(self.state_file, state)

    def _format_alert(self, signal: AnomalySignal) -> str:
        details = ", ".join(f"{key}={value}" for key, value in signal.details.items())
        return (
            f"[{signal.signal_type.upper()}] {signal.symbol} "
            f"severity={signal.severity} | {details}"
        )

    def _generate_reports(
        self,
        symbols: list[str],
        whale_report: dict[str, Any],
        retail_report: dict[str, Any],
        anomalies: list[AnomalySignal],
    ) -> None:
        ai_cfg = self.config.get("ai_report", {})
        if not ai_cfg.get("api_url") or not ai_cfg.get("api_key"):
            return
        state = _load_json(self.report_state_file)
        now = datetime.now(UTC)
        interval = timedelta(minutes=self.report_interval)
        anomaly_map: dict[str, list[AnomalySignal]] = {}
        for signal in anomalies:
            anomaly_map.setdefault(signal.symbol, []).append(signal)

        for symbol in symbols:
            last_ts = float(state.get(symbol, 0.0) or 0.0)
            last_dt = datetime.fromtimestamp(last_ts, tz=UTC) if last_ts else None
            if last_dt and now - last_dt < interval:
                continue
            report = self._build_report_prompt(
                symbol,
                whale_report.get(symbol, {}) if isinstance(whale_report, dict) else {},
                retail_report.get(symbol, {}) if isinstance(retail_report, dict) else {},
                anomaly_map.get(symbol, []),
            )
            summary = _call_ai_api(report, ai_cfg)
            if not summary:
                continue
            state[symbol] = now.timestamp()
            self._persist_report(symbol, summary)
            send_telegram_message(
                _truncate_message(summary),
                bot_token=self.config.get("telegram", {}).get("bot_token"),
                chat_id=self.config.get("telegram", {}).get("chat_id"),
            )
        _save_json(self.report_state_file, state)

    def _build_report_prompt(
        self,
        symbol: str,
        whale_data: dict[str, Any],
        retail_data: dict[str, Any],
        anomalies: list[AnomalySignal],
    ) -> str:
        market = self._fetch_market_metrics(symbol)
        anomaly_lines = [
            f"- {item.signal_type}: {item.details}" for item in anomalies
        ] or ["- None"]
        language = self.config.get("ai_report", {}).get("language", "zh")
        lang_line = (
            "Write the report in Simplified Chinese."
            if language == "zh"
            else "Write the report in English."
        )
        return "\n".join(
            [
                "You are a crypto market analyst.",
                lang_line,
                f"Symbol: {symbol}",
                f"Market snapshot: {market}",
                f"Whale metrics: {whale_data}",
                f"Retail metrics: {retail_data}",
                "Detected anomalies:",
                *anomaly_lines,
                "Provide: trend summary, risk level, and action notes.",
            ]
        )

    def _persist_report(self, symbol: str, report: str) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        safe_symbol = symbol.replace("/", "_").replace(":", "_")
        report_path = self.reports_dir / f"{safe_symbol}-{timestamp}.md"
        report_path.write_text(report, encoding="utf-8")
        latest_path = self.reports_dir / f"{safe_symbol}-latest.md"
        latest_path.write_text(report, encoding="utf-8")


def _call_ai_api(prompt: str, config: dict[str, Any]) -> str | None:
    api_url = config.get("api_url")
    api_key = config.get("api_key")
    model = config.get("model", "gpt-4o-mini")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a crypto market analyst."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    try:
        response = requests.post(api_url, headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.warning("AI report request failed: %s", exc)
        return None
    choices = data.get("choices") or []
    if not choices:
        return None
    message = choices[0].get("message", {})
    return message.get("content")


def _get_proxies() -> dict[str, str] | None:
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _load_json(path: str | Path) -> dict[str, Any]:
    if not path:
        return {}
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _last_mad_z(values: list[float], window: int = 60, min_periods: int = 20) -> float:
    if len(values) < min_periods + 1:
        return 0.0
    window_values = values[-(window + 1) : -1]
    if len(window_values) < min_periods:
        return 0.0
    median = float(np.median(window_values))
    mad = float(np.median(np.abs(np.array(window_values) - median)))
    if mad == 0:
        return 0.0
    last_value = values[-1]
    return 0.6745 * (last_value - median) / mad


def _truncate_message(text: str, limit: int = 3800) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (truncated)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Anomaly signal monitor.")
    parser.add_argument(
        "--config",
        default="user_data/anomaly/anomaly_config.json",
        help="Path to anomaly monitor config.",
    )
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = _load_json(args.config)
    if not config:
        raise RuntimeError(f"Missing or invalid config: {args.config}")
    monitor = AnomalyMonitor(config)

    if args.once:
        monitor.run_once()
        return

    poll_seconds = int(config.get("runtime", {}).get("poll_seconds", 60))
    while True:
        monitor.run_once()
        time.sleep(poll_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    main()
