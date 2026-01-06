#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.exceptions import OperationalException
from freqtrade.strategy import DecimalParameter, IntParameter, IStrategy
from freqtrade.valuescan_api.ai_tuner import ValueScanAITuner, ValueScanFeedbackStore

try:
    from freqtrade.valuescan_api import ValueScanProvider, get_provider
except ImportError:
    ValueScanProvider = None
    get_provider = None

logger = logging.getLogger(__name__)


class ValueScanSegmentedStrategyAI(IStrategy):
    """
    ValueScan-driven strategy with segmented AI iteration (mainstream vs altcoin).
    """

    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = True
    process_only_new_candles = True
    startup_candle_count = 200

    minimal_roi = {"0": 0.08, "120": 0.03, "360": 0.0}
    stoploss = -0.12
    trailing_stop = True
    trailing_stop_positive = 0.02
    trailing_stop_positive_offset = 0.04
    trailing_only_offset_is_reached = True

    buy_signal_strength = DecimalParameter(0.1, 0.8, default=0.28, space="buy")
    sell_signal_strength = DecimalParameter(-0.8, -0.1, default=-0.28, space="sell")
    bullish_ratio_threshold = IntParameter(45, 80, default=55, space="buy")
    bearish_ratio_threshold = IntParameter(45, 80, default=55, space="sell")
    adx_threshold = IntParameter(15, 45, default=20, space="buy")
    min_atr_pct = DecimalParameter(0.002, 0.03, default=0.007, space="buy")
    min_volume_ratio = DecimalParameter(0.6, 1.8, default=0.9, space="buy")

    vs_provider: Optional[ValueScanProvider] = None

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        valuescan_cfg = config.get("valuescan", {})
        token_file = valuescan_cfg.get("token_file")
        if token_file and not os.getenv("VALUESCAN_TOKEN_FILE"):
            os.environ["VALUESCAN_TOKEN_FILE"] = token_file
        self._valuescan_allow_backtest = bool(valuescan_cfg.get("enable_backtest", False))
        self._valuescan_require_live = bool(valuescan_cfg.get("require_live", True))
        self._valuescan_snapshot: dict[str, dict[str, float]] = {}
        self._tuned_thresholds: dict[str, float] = {}
        if isinstance(valuescan_cfg, dict):
            self._tuning_cfg = valuescan_cfg.get("tuning", {})
            iteration_cfg = valuescan_cfg.get("iteration", {})
        else:
            self._tuning_cfg = {}
            iteration_cfg = {}
        self._iteration_segment = str(iteration_cfg.get("segment", "default") or "default")
        self._iteration_offsets = iteration_cfg.get("threshold_offsets", {}) if isinstance(iteration_cfg, dict) else {}
        self._tuning_enabled = bool(self._tuning_cfg.get("enabled", False))
        self._tuning_refresh_minutes = int(self._tuning_cfg.get("refresh_minutes", 20))
        self._tuning_last_refresh: datetime | None = None
        self._tuner: ValueScanAITuner | None = None
        self._feedback_store: ValueScanFeedbackStore | None = None
        user_data_dir = self.config.get("user_data_dir", Path.cwd() / "user_data")
        self._user_data_dir = (
            user_data_dir if isinstance(user_data_dir, Path) else Path(user_data_dir)
        )
        if get_provider is not None:
            proxy = valuescan_cfg.get("proxy")
            self.vs_provider = get_provider(proxy)
            logger.info("ValueScan provider enabled for segment %s", self._iteration_segment)
        else:
            logger.warning("ValueScan provider unavailable")
        self._init_tuning()

    def bot_start(self, **kwargs) -> None:
        runmode = self.config.get("runmode")
        runmode_value = getattr(runmode, "value", str(runmode)).lower()
        if (
            self._valuescan_require_live
            and not self._valuescan_allow_backtest
            and runmode_value in ("backtest", "hyperopt")
        ):
            raise OperationalException(
                "ValueScan strategies require live or dry-run mode. "
                "ValueScan has no historical API, so backtesting is disabled. "
                "Set valuescan.enable_backtest to True only if you accept the limitation."
            )
        self._refresh_tuned_thresholds(force=True)

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        self._refresh_tuned_thresholds(now=current_time)

    def _valuescan_enabled(self) -> bool:
        if self.vs_provider is None:
            return False
        if self._valuescan_allow_backtest:
            return True
        if not self.dp or not getattr(self.dp, "runmode", None):
            return True
        return self.dp.runmode.value not in ("backtest", "hyperopt")

    @staticmethod
    def _symbol_from_pair(pair: str) -> str:
        return pair.split("/")[0].upper()

    def _get_valuescan_data(self, symbol: str) -> dict:
        if self.vs_provider is None:
            return {}
        try:
            data = {
                "signal_strength": self.vs_provider.get_signal_strength(symbol),
                "is_bullish": self.vs_provider.is_bullish_signal(symbol),
                "is_bearish": self.vs_provider.is_bearish_signal(symbol),
            }
            fund_flow = self.vs_provider.get_fund_flow(symbol)
            if isinstance(fund_flow, dict) and fund_flow.get("code") == 200:
                flow_data = fund_flow.get("data", {})
                for period in ("1h", "4h", "24h"):
                    period_data = flow_data.get(period, {}) if isinstance(flow_data, dict) else {}
                    spot = period_data.get("stopTradeInflow", 0)
                    contract = period_data.get("contractTradeInflow", 0)
                    data[f"total_inflow_{period}"] = spot + contract
            ai = self.vs_provider.get_coin_ai_analysis(symbol)
            if isinstance(ai, dict) and ai.get("code") == 200:
                ai_data = ai.get("data", {})
                data["bullish_ratio"] = ai_data.get("bullishRatio", 0)
                data["bearish_ratio"] = ai_data.get("bearishRatio", 0)
            exchange_flow = self.vs_provider.get_exchange_flow(symbol)
            if isinstance(exchange_flow, dict) and exchange_flow.get("code") == 200:
                ex_data = exchange_flow.get("data", {})
                h24 = ex_data.get("24h", {}) if isinstance(ex_data, dict) else {}
                data["exchange_net_flow"] = h24.get("inFlowValue", 0)
            return data
        except Exception as exc:
            logger.warning("ValueScan fetch failed for %s: %s", symbol, exc)
            return {}

    def _init_tuning(self) -> None:
        if not self._tuning_enabled:
            return
        state_path = self._resolve_user_path(
            self._tuning_cfg.get("state_file"), "valuescan_iteration_state.json"
        )
        feedback_path = self._resolve_user_path(
            self._tuning_cfg.get("feedback_file"), "valuescan_iteration_feedback.jsonl"
        )
        self._tuner = ValueScanAITuner(
            state_path,
            min_trades=int(self._tuning_cfg.get("min_trades", 10)),
            profit_threshold=float(self._tuning_cfg.get("profit_threshold", 0.0)),
        )
        self._feedback_store = ValueScanFeedbackStore(feedback_path)

    def _resolve_user_path(self, value: str | None, fallback: str) -> Path:
        path = Path(value or fallback)
        if not path.is_absolute():
            path = self._user_data_dir / path
        return path

    def _apply_offsets(self, thresholds: dict[str, float], bounds: dict[str, tuple[float, float]]) -> None:
        offsets = self._iteration_offsets if isinstance(self._iteration_offsets, dict) else {}
        for key, offset in offsets.items():
            if key not in thresholds:
                continue
            try:
                delta = float(offset)
            except (TypeError, ValueError):
                continue
            low, high = bounds.get(key, (thresholds[key], thresholds[key]))
            thresholds[key] = max(low, min(high, thresholds[key] + delta))

    def _refresh_tuned_thresholds(
        self,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> None:
        if not self._tuning_enabled or self._tuner is None:
            return
        if now is None:
            now = datetime.now(UTC)
        if not force and self._tuning_last_refresh is not None:
            if now - self._tuning_last_refresh < timedelta(minutes=self._tuning_refresh_minutes):
                return
        defaults = {
            "buy_signal_strength": float(self.buy_signal_strength.value),
            "sell_signal_strength": float(self.sell_signal_strength.value),
            "bullish_ratio_threshold": float(self.bullish_ratio_threshold.value),
            "bearish_ratio_threshold": float(self.bearish_ratio_threshold.value),
        }
        bounds = {
            "buy_signal_strength": (
                float(self.buy_signal_strength.low),
                float(self.buy_signal_strength.high),
            ),
            "sell_signal_strength": (
                float(self.sell_signal_strength.low),
                float(self.sell_signal_strength.high),
            ),
            "bullish_ratio_threshold": (
                float(self.bullish_ratio_threshold.low),
                float(self.bullish_ratio_threshold.high),
            ),
            "bearish_ratio_threshold": (
                float(self.bearish_ratio_threshold.low),
                float(self.bearish_ratio_threshold.high),
            ),
        }
        self._tuned_thresholds = self._tuner.compute_thresholds(
            defaults=defaults,
            bounds=bounds,
            segment=self._iteration_segment,
        )
        self._apply_offsets(self._tuned_thresholds, bounds)
        self._tuning_last_refresh = now

    def _current_thresholds(self) -> dict[str, float]:
        thresholds = {
            "buy_signal_strength": float(self.buy_signal_strength.value),
            "sell_signal_strength": float(self.sell_signal_strength.value),
            "bullish_ratio_threshold": float(self.bullish_ratio_threshold.value),
            "bearish_ratio_threshold": float(self.bearish_ratio_threshold.value),
        }
        thresholds.update(self._tuned_thresholds)
        return thresholds

    @staticmethod
    def _format_valuescan_snapshot(vs_data: dict) -> dict[str, float]:
        return {
            "signal_strength": float(vs_data.get("signal_strength", 0.0) or 0.0),
            "bullish_ratio": float(vs_data.get("bullish_ratio", 0.0) or 0.0),
            "bearish_ratio": float(vs_data.get("bearish_ratio", 0.0) or 0.0),
            "total_inflow_1h": float(vs_data.get("total_inflow_1h", 0.0) or 0.0),
            "total_inflow_4h": float(vs_data.get("total_inflow_4h", 0.0) or 0.0),
            "total_inflow_24h": float(vs_data.get("total_inflow_24h", 0.0) or 0.0),
            "exchange_net_flow": float(vs_data.get("exchange_net_flow", 0.0) or 0.0),
        }

    def _get_valuescan_snapshot(self, pair: str) -> dict[str, float]:
        cached = self._valuescan_snapshot.get(pair)
        if cached:
            return cached
        if not self._valuescan_enabled():
            return {}
        symbol = self._symbol_from_pair(pair)
        vs_data = self._get_valuescan_data(symbol)
        if not vs_data:
            return {}
        snapshot = self._format_valuescan_snapshot(vs_data)
        self._valuescan_snapshot[pair] = snapshot
        return snapshot

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["volume_sma"] = dataframe["volume"].rolling(window=20).mean()
        vol_base = dataframe["volume_sma"].replace(0, np.nan)
        dataframe["volume_ratio"] = (dataframe["volume"] / vol_base).fillna(0)

        vs_columns = [
            "vs_signal_strength",
            "vs_is_bullish",
            "vs_is_bearish",
            "vs_bullish_ratio",
            "vs_bearish_ratio",
            "vs_total_inflow_1h",
            "vs_total_inflow_4h",
            "vs_total_inflow_24h",
            "vs_exchange_net_flow",
        ]
        for col in vs_columns:
            dataframe[col] = 0.0

        if self._valuescan_enabled() and len(dataframe) > 0:
            symbol = self._symbol_from_pair(metadata["pair"])
            vs_data = self._get_valuescan_data(symbol)
            if vs_data:
                idx = dataframe.index[-1]
                dataframe.loc[idx, "vs_signal_strength"] = vs_data.get("signal_strength", 0.0)
                dataframe.loc[idx, "vs_is_bullish"] = 1.0 if vs_data.get("is_bullish") else 0.0
                dataframe.loc[idx, "vs_is_bearish"] = 1.0 if vs_data.get("is_bearish") else 0.0
                dataframe.loc[idx, "vs_bullish_ratio"] = vs_data.get("bullish_ratio", 0.0)
                dataframe.loc[idx, "vs_bearish_ratio"] = vs_data.get("bearish_ratio", 0.0)
                dataframe.loc[idx, "vs_total_inflow_1h"] = vs_data.get("total_inflow_1h", 0.0)
                dataframe.loc[idx, "vs_total_inflow_4h"] = vs_data.get("total_inflow_4h", 0.0)
                dataframe.loc[idx, "vs_total_inflow_24h"] = vs_data.get("total_inflow_24h", 0.0)
                dataframe.loc[idx, "vs_exchange_net_flow"] = vs_data.get("exchange_net_flow", 0.0)
                self._valuescan_snapshot[metadata["pair"]] = self._format_valuescan_snapshot(vs_data)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        base_long = (
            (dataframe["ema_fast"] > dataframe["ema_slow"])
            & (dataframe["adx"] > self.adx_threshold.value)
            & (dataframe["rsi"] > 52)
            & (dataframe["atr_pct"] > self.min_atr_pct.value)
            & (dataframe["volume_ratio"] > self.min_volume_ratio.value)
        )
        base_short = (
            (dataframe["ema_fast"] < dataframe["ema_slow"])
            & (dataframe["adx"] > self.adx_threshold.value)
            & (dataframe["rsi"] < 48)
            & (dataframe["atr_pct"] > self.min_atr_pct.value)
            & (dataframe["volume_ratio"] > self.min_volume_ratio.value)
        )

        thresholds = self._current_thresholds()
        buy_signal_strength = thresholds["buy_signal_strength"]
        sell_signal_strength = thresholds["sell_signal_strength"]
        bullish_ratio_threshold = thresholds["bullish_ratio_threshold"]
        bearish_ratio_threshold = thresholds["bearish_ratio_threshold"]

        valuescan_live = self._valuescan_enabled()
        if valuescan_live:
            vs_long = (
                (dataframe["vs_signal_strength"] > buy_signal_strength)
                & (dataframe["vs_bullish_ratio"] > bullish_ratio_threshold)
                & (dataframe["vs_total_inflow_1h"] > 0)
                & (dataframe["vs_total_inflow_4h"] > 0)
            )
            vs_short = (
                (dataframe["vs_signal_strength"] < sell_signal_strength)
                & (dataframe["vs_bearish_ratio"] > bearish_ratio_threshold)
                & (dataframe["vs_total_inflow_1h"] < 0)
                & (dataframe["vs_total_inflow_4h"] < 0)
            )
        else:
            vs_long = pd.Series(True, index=dataframe.index)
            vs_short = pd.Series(True, index=dataframe.index)

        dataframe.loc[base_long & vs_long, "enter_long"] = 1
        dataframe.loc[base_short & vs_short, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        exit_long = (
            (dataframe["ema_fast"] < dataframe["ema_slow"])
            | (dataframe["rsi"] > 75)
            | (dataframe["vs_total_inflow_1h"] < 0)
        )
        exit_short = (
            (dataframe["ema_fast"] > dataframe["ema_slow"])
            | (dataframe["rsi"] < 25)
            | (dataframe["vs_total_inflow_1h"] > 0)
        )

        if self._valuescan_enabled():
            thresholds = self._current_thresholds()
            sell_signal_strength = thresholds["sell_signal_strength"]
            exit_long = exit_long | (
                (dataframe["vs_is_bearish"] == 1.0)
                | (dataframe["vs_signal_strength"] < sell_signal_strength)
                | (dataframe["vs_bearish_ratio"] > thresholds["bearish_ratio_threshold"])
            )
            exit_short = exit_short | (
                (dataframe["vs_is_bullish"] == 1.0)
                | (dataframe["vs_signal_strength"] > abs(sell_signal_strength))
                | (dataframe["vs_bullish_ratio"] > thresholds["bullish_ratio_threshold"])
            )

        dataframe.loc[exit_long, "exit_long"] = 1
        dataframe.loc[exit_short, "exit_short"] = 1
        return dataframe

    def order_filled(self, pair: str, trade, order, current_time: datetime, **kwargs) -> None:
        if not self._tuning_enabled or not self._valuescan_enabled():
            return
        if not hasattr(trade, "set_custom_data"):
            return
        try:
            if order.ft_order_side == trade.entry_side:
                entry_features = self._get_valuescan_snapshot(pair)
                if entry_features:
                    trade.set_custom_data("valuescan_entry", entry_features)
                    trade.set_custom_data("valuescan_segment", self._iteration_segment)
                    trade.set_custom_data("valuescan_entry_ts", current_time.isoformat())
            elif order.ft_order_side == trade.exit_side:
                entry_features = trade.get_custom_data("valuescan_entry", {}) or {}
                if not entry_features:
                    entry_features = self._get_valuescan_snapshot(pair)
                if entry_features:
                    profit_ratio = trade.calc_profit_ratio(order.safe_price)
                    side = "short" if trade.is_short else "long"
                    if self._tuner:
                        self._tuner.update_from_trade(
                            side,
                            profit_ratio,
                            entry_features,
                            segment=self._iteration_segment,
                        )
                        self._refresh_tuned_thresholds(force=True)
                    if self._feedback_store:
                        payload = {
                            "timestamp": current_time.replace(tzinfo=UTC).isoformat(),
                            "trade_id": trade.id,
                            "pair": pair,
                            "side": side,
                            "segment": self._iteration_segment,
                            "profit_ratio": profit_ratio,
                            "exit_reason": trade.exit_reason,
                            "entry_features": entry_features,
                            "exit_features": self._get_valuescan_snapshot(pair),
                        }
                        self._feedback_store.append_trade_feedback(payload)
        except Exception as exc:
            logger.warning("ValueScan tuning feedback failed for %s: %s", pair, exc)

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        side: str,
        **kwargs,
    ) -> float:
        return min(10.0, max_leverage)
