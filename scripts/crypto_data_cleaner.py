#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import ccxt
import numpy as np
import pandas as pd


logger = logging.getLogger(__name__)


class OrderbookSequenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class GapConfig:
    no_trade_max_minutes: int = 3
    maintenance_min_minutes: int = 30


@dataclass(frozen=True)
class TradeSegmentationConfig:
    window: str = "60min"
    min_periods: int = 20
    whale_zscore: float = 3.5
    min_notional: float = 0.0


class CryptoDataCleaner:
    """
    Production-grade ETL pipeline for crypto market data.
    """

    def __init__(
        self,
        exchange_id: str,
        *,
        gap_config: GapConfig | None = None,
        dust_threshold: float = 1e-18,
        mad_threshold: float = 3.5,
    ) -> None:
        self.exchange_id = exchange_id
        self.exchange = getattr(ccxt, exchange_id)()
        self.exchange.load_markets()
        self.gap_config = gap_config or GapConfig()
        self.dust_threshold = float(dust_threshold)
        self.mad_threshold = float(mad_threshold)

    def clean_ohlcv(self, raw: pd.DataFrame) -> pd.DataFrame:
        required = {"timestamp", "symbol", "open", "high", "low", "close", "volume"}
        missing = required - set(raw.columns)
        if missing:
            raise ValueError(f"Missing required columns: {sorted(missing)}")

        df = raw.copy()
        df["symbol"] = df["symbol"].map(self._map_symbol)
        df["ts_ms"] = pd.to_numeric(df["timestamp"], errors="coerce")
        df["timestamp"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
        df = df.dropna(subset=["timestamp", "symbol"]).sort_values(["symbol", "timestamp"])

        df = self._ensure_float64(df, ["open", "high", "low", "close", "volume"])
        df = self._filter_dust(df, ["open", "high", "low", "close", "volume"])

        cleaned = []
        for symbol, chunk in df.groupby("symbol", sort=False):
            repaired = self._repair_gaps(chunk)
            repaired["symbol"] = symbol
            repaired = self._detect_outliers(repaired)
            cleaned.append(repaired)

        out = pd.concat(cleaned, ignore_index=False).sort_index()
        out = out.reset_index(drop=False).rename(columns={"index": "timestamp"})
        out["ts_ms"] = (out["timestamp"].astype("int64") // 1_000_000).astype("int64")
        out = out.set_index("ts_ms", drop=True)
        return out

    def to_decimal_frame(
        self,
        df: pd.DataFrame,
        *,
        price_columns: Iterable[str] = ("open", "high", "low", "close"),
        volume_columns: Iterable[str] = ("volume",),
        quantize: str = "0.00000001",
    ) -> pd.DataFrame:
        q = Decimal(quantize)
        out = df.copy()
        for col in list(price_columns) + list(volume_columns):
            out[f"decimal_{col}"] = out[col].apply(
                lambda value: self._to_decimal(value, q)
            )
        return out

    def summarize_quality(self, df: pd.DataFrame) -> dict[str, Any]:
        def safe_sum(frame: pd.DataFrame, column: str) -> int:
            if column not in frame.columns:
                return 0
            return int(frame[column].fillna(False).astype(bool).sum())

        summary = {
            "rows": len(df),
            "symbols": sorted(df["symbol"].dropna().unique().tolist()),
            "missing_data": safe_sum(df, "is_missing_data"),
            "no_trade": safe_sum(df, "is_no_trade"),
            "outlier_ohlc": safe_sum(df, "is_outlier_ohlc"),
        }
        if not df.empty:
            summary["range_start"] = df["timestamp"].min().isoformat()
            summary["range_end"] = df["timestamp"].max().isoformat()
        else:
            summary["range_start"] = None
            summary["range_end"] = None

        per_symbol = []
        for symbol, chunk in df.groupby("symbol", sort=False):
            per_symbol.append(
                {
                    "symbol": symbol,
                    "rows": len(chunk),
                    "missing_data": safe_sum(chunk, "is_missing_data"),
                    "no_trade": safe_sum(chunk, "is_no_trade"),
                    "outlier_ohlc": safe_sum(chunk, "is_outlier_ohlc"),
                    "range_start": chunk["timestamp"].min().isoformat(),
                    "range_end": chunk["timestamp"].max().isoformat(),
                }
            )
        summary["per_symbol"] = per_symbol
        return summary

    def summarize_trade_quality(self, df: pd.DataFrame) -> dict[str, Any]:
        summary = {
            "rows": len(df),
            "symbols": sorted(df["symbol"].dropna().unique().tolist()),
            "whale_trades": int(df.get("is_whale", False).fillna(False).sum()),
            "retail_trades": int(df.get("is_retail", False).fillna(False).sum()),
            "range_start": None,
            "range_end": None,
        }
        if not df.empty:
            summary["range_start"] = df["timestamp"].min().isoformat()
            summary["range_end"] = df["timestamp"].max().isoformat()
        return summary

    def write_quality_report(self, summary: dict[str, Any], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, ensure_ascii=True, indent=2), encoding="utf-8")

    def to_freqtrade_ohlcv(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        data = df.copy()
        data["date"] = data["ts_ms"].astype("int64")
        return data.loc[:, ["date", "open", "high", "low", "close", "volume"]]

    def clean_trades(
        self,
        raw: pd.DataFrame,
        *,
        config: TradeSegmentationConfig | None = None,
    ) -> pd.DataFrame:
        required = {"timestamp", "symbol", "price", "amount", "side"}
        missing = required - set(raw.columns)
        if missing:
            raise ValueError(f"Missing required columns: {sorted(missing)}")

        cfg = config or TradeSegmentationConfig()
        df = raw.copy()
        df["symbol"] = df["symbol"].map(self._map_symbol)
        df["ts_ms"] = pd.to_numeric(df["timestamp"], errors="coerce")
        df["timestamp"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
        df = df.dropna(subset=["timestamp", "symbol"])
        df = df.sort_values(["symbol", "timestamp"])

        df = self._ensure_float64(df, ["price", "amount"])
        df = self._filter_dust(df, ["price", "amount"])
        df["side"] = df["side"].map(self._normalize_side)
        df["notional"] = (df["price"] * df["amount"]).astype("float64")
        df = df[df["notional"].notna() & (df["notional"] >= 0)]

        segmented = []
        for symbol, chunk in df.groupby("symbol", sort=False):
            enriched = self._segment_trades(chunk, cfg)
            enriched["symbol"] = symbol
            segmented.append(enriched)

        result = pd.concat(segmented, ignore_index=True)
        return result

    def _segment_trades(
        self,
        df: pd.DataFrame,
        config: TradeSegmentationConfig,
    ) -> pd.DataFrame:
        chunk = df.set_index("timestamp").sort_index()
        notional = chunk["notional"]
        rolling_median = notional.rolling(
            config.window,
            min_periods=config.min_periods,
            closed="left",  # avoid look-ahead bias
        ).median()
        rolling_mad = self._rolling_mad(
            notional,
            window=config.window,
            min_periods=config.min_periods,
        )
        expanding_median = notional.expanding(min_periods=config.min_periods).median().shift(1)
        expanding_mad = (
            (notional - expanding_median).abs().expanding(min_periods=config.min_periods).median()
        ).shift(1)

        median = rolling_median.combine_first(expanding_median)
        mad = rolling_mad.combine_first(expanding_mad)
        mad_safe = mad.replace(0, np.nan)
        modified_z = 0.6745 * (notional - median) / mad_safe

        chunk["whale_zscore"] = modified_z
        chunk["is_whale"] = modified_z.abs() >= config.whale_zscore
        chunk["is_retail"] = ~chunk["is_whale"]
        if config.min_notional > 0:
            chunk["is_retail"] = chunk["is_retail"] | (chunk["notional"] < config.min_notional)
            chunk["is_whale"] = chunk["is_whale"] & (chunk["notional"] >= config.min_notional)
        return chunk.reset_index()

    def aggregate_whale_flow(
        self,
        trades: pd.DataFrame,
        *,
        timeframe: str = "1min",
    ) -> pd.DataFrame:
        df = trades[trades.get("is_whale", False)].copy()
        if df.empty:
            return pd.DataFrame(
                columns=[
                    "timestamp",
                    "whale_notional",
                    "whale_net_notional",
                    "whale_trade_count",
                    "whale_buy_ratio",
                ]
            )
        df = df.set_index("timestamp").sort_index()
        df["buy_notional"] = np.where(df["side"] == "buy", df["notional"], 0.0)
        df["sell_notional"] = np.where(df["side"] == "sell", df["notional"], 0.0)
        bucket = df.resample(timeframe).agg(
            whale_notional=("notional", "sum"),
            whale_trade_count=("notional", "count"),
            whale_buy_notional=("buy_notional", "sum"),
            whale_sell_notional=("sell_notional", "sum"),
        )
        total = bucket["whale_buy_notional"] + bucket["whale_sell_notional"]
        bucket["whale_net_notional"] = bucket["whale_buy_notional"] - bucket["whale_sell_notional"]
        bucket["whale_buy_ratio"] = np.where(total > 0, bucket["whale_buy_notional"] / total, 0.0)
        bucket = bucket.drop(columns=["whale_buy_notional", "whale_sell_notional"])
        return bucket.reset_index()

    def aggregate_retail_fomo(
        self,
        trades: pd.DataFrame,
        *,
        timeframe: str = "1min",
        window: str = "30min",
        min_periods: int = 10,
    ) -> pd.DataFrame:
        df = trades[trades.get("is_retail", False)].copy()
        if df.empty:
            return pd.DataFrame(
                columns=[
                    "timestamp",
                    "retail_notional",
                    "retail_trade_count",
                    "retail_buy_ratio",
                    "fomo_index",
                ]
            )
        df = df.set_index("timestamp").sort_index()
        df["buy_notional"] = np.where(df["side"] == "buy", df["notional"], 0.0)
        df["sell_notional"] = np.where(df["side"] == "sell", df["notional"], 0.0)
        bucket = df.resample(timeframe).agg(
            retail_notional=("notional", "sum"),
            retail_trade_count=("notional", "count"),
            retail_buy_notional=("buy_notional", "sum"),
            retail_sell_notional=("sell_notional", "sum"),
            retail_price=("price", "last"),
        )
        total = bucket["retail_buy_notional"] + bucket["retail_sell_notional"]
        bucket["retail_buy_ratio"] = np.where(
            total > 0,
            bucket["retail_buy_notional"] / total,
            0.0,
        )
        bucket["retail_price_change"] = bucket["retail_price"].pct_change().fillna(0.0)

        volume_z = self._rolling_mad_z(
            bucket["retail_notional"],
            window=window,
            min_periods=min_periods,
        )
        count_z = self._rolling_mad_z(
            bucket["retail_trade_count"].astype("float64"),
            window=window,
            min_periods=min_periods,
        )
        price_z = self._rolling_mad_z(
            bucket["retail_price_change"],
            window=window,
            min_periods=min_periods,
        )

        bucket["retail_volume_z"] = volume_z
        bucket["retail_count_z"] = count_z
        bucket["retail_price_z"] = price_z

        def positive(series: pd.Series) -> pd.Series:
            return np.clip(series, 0.0, None)
        bucket["fomo_index"] = (
            0.5 * positive(volume_z)
            + 0.3 * positive(count_z)
            + 0.2 * positive(price_z)
        )
        return bucket.reset_index()

    def write_freqtrade_parquet(
        self,
        df: pd.DataFrame,
        *,
        data_dir: Path,
        timeframe: str,
        candle_type: str = "spot",
    ) -> None:
        from freqtrade.data.history.datahandlers import get_datahandler
        from freqtrade.enums import CandleType

        handler = get_datahandler(data_dir, data_format="parquet")
        candle = CandleType.from_string(candle_type)

        for symbol, chunk in df.groupby("symbol", sort=False):
            payload = self.to_freqtrade_ohlcv(chunk)
            handler.ohlcv_store(symbol, timeframe, payload, candle)

    def _map_symbol(self, raw_symbol: Any) -> str:
        if raw_symbol is None:
            raise ValueError("Symbol is null.")
        symbol = str(raw_symbol).strip().upper()
        if symbol in self.exchange.markets:
            return self.exchange.markets[symbol]["symbol"]
        market_by_id = self.exchange.markets_by_id.get(symbol)
        if market_by_id:
            return market_by_id[0]["symbol"]
        if "/" in symbol:
            base, quote = symbol.split("/", maxsplit=1)
            if quote not in {"USD", "USDT", "USDC"}:
                logger.warning("Unknown quote currency for symbol %s", symbol)
            return f"{base}/{quote}"
        raise ValueError(f"Unknown symbol: {raw_symbol}")

    def _ensure_float64(self, df: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
        for col in columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
        return df

    def _filter_dust(self, df: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
        for col in columns:
            df[col] = df[col].mask(df[col].abs() < self.dust_threshold, 0.0)
        return df

    @staticmethod
    def _normalize_side(raw: Any) -> str | None:
        if raw is None:
            return None
        value = str(raw).strip().lower()
        if value in {"buy", "bid"}:
            return "buy"
        if value in {"sell", "ask"}:
            return "sell"
        return None

    def _repair_gaps(self, df: pd.DataFrame) -> pd.DataFrame:
        chunk = df.set_index("timestamp").sort_index()
        agg = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
        resampled = chunk.resample("1min").agg(agg)
        resampled["row_count"] = chunk["close"].resample("1min").count()

        missing = resampled["row_count"].eq(0)
        resampled["is_missing_data"] = False
        resampled["is_no_trade"] = False

        if missing.any():
            groups = (missing != missing.shift()).cumsum()
            for group_id, group_mask in missing.groupby(groups):
                if not group_mask.iloc[0]:
                    continue
                length = int(group_mask.sum())
                indexer = group_mask.index
                if length >= self.gap_config.maintenance_min_minutes:
                    resampled.loc[indexer, "is_missing_data"] = True
                else:
                    resampled.loc[indexer, "is_no_trade"] = True

        no_trade = resampled["is_no_trade"]
        if no_trade.any():
            resampled.loc[no_trade, "close"] = resampled["close"].ffill()
            resampled.loc[no_trade, "open"] = resampled.loc[no_trade, "close"]
            resampled.loc[no_trade, "high"] = resampled.loc[no_trade, "close"]
            resampled.loc[no_trade, "low"] = resampled.loc[no_trade, "close"]
            resampled.loc[no_trade, "volume"] = 0.0

        missing_data = resampled["is_missing_data"]
        if missing_data.any():
            resampled.loc[missing_data, ["open", "high", "low", "close", "volume"]] = np.nan

        resampled = resampled.drop(columns=["row_count"])
        return resampled

    def _detect_outliers(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["is_outlier_close"] = self._mad_outlier_mask(df["close"])
        df["is_outlier_ohlc"] = (
            self._mad_outlier_mask(df["open"])
            | self._mad_outlier_mask(df["high"])
            | self._mad_outlier_mask(df["low"])
            | df["is_outlier_close"]
        )
        return df

    def _mad_outlier_mask(self, series: pd.Series) -> pd.Series:
        values = series.dropna()
        if values.empty:
            return pd.Series(False, index=series.index)
        median = values.median()
        mad = (values - median).abs().median()
        if mad == 0:
            return pd.Series(False, index=series.index)
        # Modified Z-score to avoid std-dev assumptions.
        modified_z = 0.6745 * (series - median) / mad
        return modified_z.abs() > self.mad_threshold

    def _rolling_mad(
        self,
        series: pd.Series,
        *,
        window: str,
        min_periods: int,
    ) -> pd.Series:
        def mad(values: np.ndarray) -> float:
            median = np.median(values)
            return float(np.median(np.abs(values - median)))

        return series.rolling(window, min_periods=min_periods, closed="left").apply(
            mad,
            raw=True,
        )

    def _rolling_mad_z(
        self,
        series: pd.Series,
        *,
        window: str,
        min_periods: int,
    ) -> pd.Series:
        rolling_median = series.rolling(
            window,
            min_periods=min_periods,
            closed="left",  # avoid look-ahead bias
        ).median()
        rolling_mad = self._rolling_mad(series, window=window, min_periods=min_periods)
        median = rolling_median.combine_first(
            series.expanding(min_periods=min_periods).median().shift(1)
        )
        mad = rolling_mad.combine_first(
            (series - median).abs().expanding(min_periods=min_periods).median().shift(1)
        )
        mad_safe = mad.replace(0, np.nan)
        return 0.6745 * (series - median) / mad_safe

    @staticmethod
    def _to_decimal(value: Any, quant: Decimal) -> Decimal | None:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return None
        try:
            return Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def rebuild_orderbook(
        snapshot: dict[str, Any],
        diffs: Iterable[dict[str, Any]],
    ) -> dict[str, list[list[float]]]:
        bids = {float(price): float(size) for price, size in snapshot.get("bids", [])}
        asks = {float(price): float(size) for price, size in snapshot.get("asks", [])}
        last_u = snapshot.get("u") or snapshot.get("lastUpdateId")
        if last_u is None:
            raise ValueError("Snapshot missing last update id.")

        for diff in diffs:
            u = diff.get("u")
            pu = diff.get("pu")
            if u is None or pu is None:
                raise ValueError("Diff missing sequence ids.")
            if u != pu + 1:
                raise OrderbookSequenceError("Orderbook sequence mismatch.")
            for price, size in diff.get("b", []):
                price = float(price)
                size = float(size)
                if size == 0.0:
                    bids.pop(price, None)
                else:
                    bids[price] = size
            for price, size in diff.get("a", []):
                price = float(price)
                size = float(size)
                if size == 0.0:
                    asks.pop(price, None)
                else:
                    asks[price] = size
            last_u = u

        bids_sorted = sorted(bids.items(), key=lambda x: x[0], reverse=True)
        asks_sorted = sorted(asks.items(), key=lambda x: x[0])
        return {"bids": [[p, s] for p, s in bids_sorted], "asks": [[p, s] for p, s in asks_sorted]}

    @staticmethod
    def align_sources(
        base: pd.DataFrame,
        others: dict[str, pd.DataFrame],
        *,
        tolerance_seconds: int = 10,
    ) -> pd.DataFrame:
        aligned = base.sort_values("timestamp")
        for name, df in others.items():
            df_sorted = df.sort_values("timestamp")
            aligned = pd.merge_asof(
                aligned,
                df_sorted,
                on="timestamp",
                direction="backward",  # avoid look-ahead bias
                tolerance=pd.Timedelta(seconds=tolerance_seconds),
                suffixes=("", f"_{name}"),
            )
        return aligned

    @staticmethod
    def write_parquet(df: pd.DataFrame, path: str) -> None:
        df.to_parquet(path, compression="snappy", index=True)
