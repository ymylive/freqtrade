#!/usr/bin/env python3
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.ai_iteration import AiFeedbackStore, AiIterationTuner
from freqtrade.strategy import DecimalParameter, IntParameter, IStrategy


logger = logging.getLogger(__name__)


class CcxtSegmentedStrategyAI(IStrategy):
    """
    CCXT-driven strategy with segmented AI iteration (mainstream vs altcoin).
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

    rsi_long = IntParameter(50, 70, default=56, space="buy")
    rsi_short = IntParameter(30, 50, default=44, space="sell")
    macd_hist_threshold = DecimalParameter(-0.01, 0.05, default=0.0, space="buy")
    ema_diff_threshold = DecimalParameter(0.0005, 0.01, default=0.002, space="buy")
    atr_pct_threshold = DecimalParameter(0.001, 0.02, default=0.004, space="buy")
    volume_zscore_threshold = DecimalParameter(-0.5, 2.0, default=0.0, space="buy")

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        ai_cfg = config.get("ai_iteration", {})
        if isinstance(ai_cfg, dict):
            self._iteration_segment = str(ai_cfg.get("segment", "default") or "default")
            self._iteration_offsets = ai_cfg.get("threshold_offsets", {})
        else:
            self._iteration_segment = "default"
            self._iteration_offsets = {}
        if isinstance(ai_cfg, dict):
            self._tuning_enabled = bool(ai_cfg.get("enabled", False))
            self._tuning_refresh_minutes = int(ai_cfg.get("refresh_minutes", 20))
        else:
            self._tuning_enabled = False
            self._tuning_refresh_minutes = 20
        self._tuning_last_refresh: datetime | None = None
        self._tuner: AiIterationTuner | None = None
        self._feedback_store: AiFeedbackStore | None = None
        self._feature_cache: dict[str, dict[str, float]] = {}

        user_data_dir = self.config.get("user_data_dir", Path.cwd() / "user_data")
        self._user_data_dir = (
            user_data_dir if isinstance(user_data_dir, Path) else Path(user_data_dir)
        )
        self._init_tuning(ai_cfg if isinstance(ai_cfg, dict) else {})

    def bot_start(self, **kwargs) -> None:
        self._refresh_tuned_thresholds(force=True)

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        self._refresh_tuned_thresholds(now=current_time)

    def _init_tuning(self, cfg: dict) -> None:
        if not self._tuning_enabled:
            return
        state_path = self._resolve_user_path(
            cfg.get("state_file"),
            "ai_iteration_state.json",
        )
        feedback_path = self._resolve_user_path(
            cfg.get("feedback_file"),
            "ai_iteration_feedback.jsonl",
        )
        self._tuner = AiIterationTuner(
            state_path,
            min_trades=int(cfg.get("min_trades", 10)),
            profit_threshold=float(cfg.get("profit_threshold", 0.0)),
        )
        self._feedback_store = AiFeedbackStore(feedback_path)

    def _resolve_user_path(self, value: str | None, fallback: str) -> Path:
        path = Path(value or fallback)
        if not path.is_absolute():
            path = self._user_data_dir / path
        return path

    def _apply_offsets(
        self,
        thresholds: dict[str, float],
        bounds: dict[str, tuple[float, float]],
    ) -> None:
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
            "rsi_long": float(self.rsi_long.value),
            "rsi_short": float(self.rsi_short.value),
            "macd_hist": float(self.macd_hist_threshold.value),
            "ema_diff": float(self.ema_diff_threshold.value),
            "atr_pct": float(self.atr_pct_threshold.value),
            "volume_zscore": float(self.volume_zscore_threshold.value),
        }
        bounds = {
            "rsi_long": (float(self.rsi_long.low), float(self.rsi_long.high)),
            "rsi_short": (float(self.rsi_short.low), float(self.rsi_short.high)),
            "macd_hist": (
                float(self.macd_hist_threshold.low),
                float(self.macd_hist_threshold.high),
            ),
            "ema_diff": (
                float(self.ema_diff_threshold.low),
                float(self.ema_diff_threshold.high),
            ),
            "atr_pct": (
                float(self.atr_pct_threshold.low),
                float(self.atr_pct_threshold.high),
            ),
            "volume_zscore": (
                float(self.volume_zscore_threshold.low),
                float(self.volume_zscore_threshold.high),
            ),
        }
        tuned = self._tuner.compute_thresholds(
            defaults=defaults,
            bounds=bounds,
            segment=self._iteration_segment,
        )
        self._apply_offsets(tuned, bounds)
        self._tuned_thresholds = tuned
        self._tuning_last_refresh = now

    def _current_thresholds(self) -> dict[str, float]:
        thresholds = {
            "rsi_long": float(self.rsi_long.value),
            "rsi_short": float(self.rsi_short.value),
            "macd_hist": float(self.macd_hist_threshold.value),
            "ema_diff": float(self.ema_diff_threshold.value),
            "atr_pct": float(self.atr_pct_threshold.value),
            "volume_zscore": float(self.volume_zscore_threshold.value),
        }
        thresholds.update(getattr(self, "_tuned_thresholds", {}))
        return thresholds

    @staticmethod
    def _feature_snapshot(row: pd.Series) -> dict[str, float]:
        def safe_float(value: float | int | None) -> float:
            if value is None or np.isnan(value):
                return 0.0
            return float(value)

        return {
            "rsi": safe_float(row.get("rsi")),
            "macd_hist": safe_float(row.get("macd_hist")),
            "ema_diff": safe_float(row.get("ema_diff")),
            "atr_pct": safe_float(row.get("atr_pct")),
            "volume_zscore": safe_float(row.get("volume_zscore")),
            "price_change_1h": safe_float(row.get("price_change_1h")),
            "price_change_4h": safe_float(row.get("price_change_4h")),
        }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema_diff"] = (dataframe["ema_fast"] - dataframe["ema_slow"]) / dataframe["close"]
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macd_hist"] = macd["macdhist"]
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]

        volume_mean = dataframe["volume"].rolling(window=30).mean()
        volume_std = dataframe["volume"].rolling(window=30).std()
        volume_std = volume_std.replace(0, np.nan)
        dataframe["volume_zscore"] = ((dataframe["volume"] - volume_mean) / volume_std).fillna(0)

        dataframe["price_change_1h"] = dataframe["close"].pct_change(1).fillna(0)
        dataframe["price_change_4h"] = dataframe["close"].pct_change(4).fillna(0)

        if not dataframe.empty:
            last_row = dataframe.iloc[-1]
            self._feature_cache[metadata["pair"]] = self._feature_snapshot(last_row)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        thresholds = self._current_thresholds()
        macd_hist_threshold = thresholds["macd_hist"]
        ema_diff_threshold = thresholds["ema_diff"]

        base_long = (
            (dataframe["ema_fast"] > dataframe["ema_slow"])
            & (dataframe["ema_diff"] > ema_diff_threshold)
            & (dataframe["macd_hist"] > macd_hist_threshold)
            & (dataframe["rsi"] > thresholds["rsi_long"])
            & (dataframe["atr_pct"] > thresholds["atr_pct"])
            & (dataframe["volume_zscore"] > thresholds["volume_zscore"])
        )
        base_short = (
            (dataframe["ema_fast"] < dataframe["ema_slow"])
            & (dataframe["ema_diff"] < -ema_diff_threshold)
            & (dataframe["macd_hist"] < -macd_hist_threshold)
            & (dataframe["rsi"] < thresholds["rsi_short"])
            & (dataframe["atr_pct"] > thresholds["atr_pct"])
            & (dataframe["volume_zscore"] > thresholds["volume_zscore"])
        )

        dataframe.loc[base_long, "enter_long"] = 1
        dataframe.loc[base_short, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        exit_long = (
            (dataframe["ema_fast"] < dataframe["ema_slow"])
            | (dataframe["rsi"] > 75)
            | (dataframe["macd_hist"] < 0)
        )
        exit_short = (
            (dataframe["ema_fast"] > dataframe["ema_slow"])
            | (dataframe["rsi"] < 25)
            | (dataframe["macd_hist"] > 0)
        )

        dataframe.loc[exit_long, "exit_long"] = 1
        dataframe.loc[exit_short, "exit_short"] = 1
        return dataframe

    def order_filled(self, pair: str, trade, order, current_time: datetime, **kwargs) -> None:
        if not self._tuning_enabled:
            return
        if not hasattr(trade, "set_custom_data"):
            return
        try:
            if order.ft_order_side == trade.entry_side:
                entry_features = self._feature_cache.get(pair, {})
                if entry_features:
                    trade.set_custom_data("ai_entry", entry_features)
                    trade.set_custom_data("ai_segment", self._iteration_segment)
                    trade.set_custom_data("ai_entry_ts", current_time.isoformat())
            elif order.ft_order_side == trade.exit_side:
                entry_features = trade.get_custom_data("ai_entry", {}) or {}
                if not entry_features:
                    entry_features = self._feature_cache.get(pair, {})
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
                            "exit_features": self._feature_cache.get(pair, {}),
                        }
                        self._feedback_store.append_trade_feedback(payload)
        except Exception as exc:
            logger.warning("AI tuning feedback failed for %s: %s", pair, exc)

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
