#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd


def _load_cleaner() -> type:
    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    from crypto_data_cleaner import CryptoDataCleaner

    return CryptoDataCleaner


CryptoDataCleaner = _load_cleaner()


logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean raw OHLCV data and write Parquet outputs for Freqtrade.",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Comma-separated input files (csv/csv.gz/parquet).",
    )
    parser.add_argument("--exchange", required=True, help="CCXT exchange id.")
    parser.add_argument("--timeframe", required=True, help="Timeframe for the candles.")
    parser.add_argument(
        "--data-dir",
        default="user_data/data",
        help="Freqtrade data directory to write parquet files to.",
    )
    parser.add_argument(
        "--candle-type",
        default="spot",
        help="Candle type (spot/futures/mark/index/funding_rate).",
    )
    parser.add_argument(
        "--cleaned-dir",
        default="user_data/etl/cleaned",
        help="Directory to store full cleaned parquet outputs.",
    )
    parser.add_argument(
        "--report-dir",
        default="user_data/etl/reports",
        help="Directory to write quality reports (json).",
    )
    return parser.parse_args()


def _read_input(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if path.suffix.lower() in {".csv", ".gz"} or path.name.endswith(".csv.gz"):
        return pd.read_csv(path)
    raise ValueError(f"Unsupported input format: {path}")


def _infer_symbol_from_path(path: Path) -> str | None:
    name = path.stem
    parts = name.split("-")
    if len(parts) >= 2:
        tail = parts[-1].lower()
        candle_types = {"spot", "futures", "mark", "index", "premiumindex", "funding_rate"}
        if tail in candle_types:
            parts = parts[:-1]
        if parts:
            last = parts[-1]
            if last.endswith(("m", "h", "d", "w")) and any(ch.isdigit() for ch in last):
                parts = parts[:-1]
    base = "-".join(parts)
    if not base:
        return None
    tokens = base.split("_")
    if len(tokens) >= 3:
        base_asset = tokens[0]
        quote = tokens[1]
        settle = tokens[2]
        return f"{base_asset}/{quote}:{settle}"
    if len(tokens) == 2:
        return f"{tokens[0]}/{tokens[1]}"
    return None


def _normalize_ohlcv_frame(df: pd.DataFrame, *, path: Path) -> pd.DataFrame:
    frame = df.copy()
    if "symbol" not in frame.columns:
        symbol = _infer_symbol_from_path(path)
        if symbol:
            frame["symbol"] = symbol
    if "timestamp" not in frame.columns and "date" in frame.columns:
        series = frame["date"]
        if pd.api.types.is_datetime64_any_dtype(series):
            frame["timestamp"] = (series.astype("int64") // 1_000_000).astype("int64")
        else:
            frame["timestamp"] = pd.to_numeric(series, errors="coerce")
    return frame


def main() -> None:
    args = parse_args()
    input_paths = [Path(item.strip()) for item in args.input.split(",") if item.strip()]
    if not input_paths:
        raise RuntimeError("No input files provided.")

    raw_frames = []
    for path in input_paths:
        if not path.exists():
            raise FileNotFoundError(path)
        raw_frames.append(_normalize_ohlcv_frame(_read_input(path), path=path))
    raw = pd.concat(raw_frames, ignore_index=True)

    cleaner = CryptoDataCleaner(args.exchange)
    cleaned = cleaner.clean_ohlcv(raw)

    data_dir = Path(args.data_dir)
    cleaner.write_freqtrade_parquet(
        cleaned,
        data_dir=data_dir,
        timeframe=args.timeframe,
        candle_type=args.candle_type,
    )

    cleaned_dir = Path(args.cleaned_dir) / args.exchange
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    for symbol, chunk in cleaned.groupby("symbol", sort=False):
        symbol_key = symbol.replace("/", "_").replace(":", "_")
        output_path = cleaned_dir / f"{symbol_key}-{args.timeframe}.parquet"
        cleaner.write_parquet(chunk, str(output_path))

    report_dir = Path(args.report_dir) / args.exchange
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"quality-{args.timeframe}.json"
    summary = cleaner.summarize_quality(cleaned)
    cleaner.write_quality_report(summary, report_path)

    logger.info("ETL complete. Cleaned rows: %s", len(cleaned))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    main()
