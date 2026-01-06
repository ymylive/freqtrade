#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pandas as pd
from crypto_data_cleaner import CryptoDataCleaner, TradeSegmentationConfig


logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean trade data, segment whale/retail flows, and emit metrics.",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Comma-separated input files (csv/csv.gz/parquet).",
    )
    parser.add_argument("--exchange", required=True, help="CCXT exchange id.")
    parser.add_argument(
        "--timeframe",
        default="1min",
        help="Resample timeframe for whale/retail metrics.",
    )
    parser.add_argument(
        "--cleaned-dir",
        default="user_data/etl/trades_cleaned",
        help="Directory to store cleaned trade parquet outputs.",
    )
    parser.add_argument(
        "--whale-dir",
        default="user_data/etl/trades_whale",
        help="Directory to store whale trade parquet outputs.",
    )
    parser.add_argument(
        "--retail-dir",
        default="user_data/etl/trades_retail",
        help="Directory to store retail trade parquet outputs.",
    )
    parser.add_argument(
        "--metrics-dir",
        default="user_data/etl/metrics",
        help="Directory to store whale/retail metrics parquet outputs.",
    )
    parser.add_argument(
        "--report-dir",
        default="user_data/etl/reports",
        help="Directory to write summary json reports.",
    )
    parser.add_argument("--window", default="60min", help="Rolling window for segmentation.")
    parser.add_argument("--min-periods", type=int, default=20)
    parser.add_argument("--whale-zscore", type=float, default=3.5)
    parser.add_argument("--min-notional", type=float, default=0.0)
    parser.add_argument("--fomo-window", default="30min")
    return parser.parse_args()


def _read_input(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if path.suffix.lower() in {".csv", ".gz"} or path.name.endswith(".csv.gz"):
        return pd.read_csv(path)
    raise ValueError(f"Unsupported input format: {path}")


def _write_latest_report(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if frame.empty:
        payload: dict[str, dict[str, object]] = {}
    else:
        payload = {}
        if "symbol" in frame.columns:
            for symbol, chunk in frame.groupby("symbol", sort=False):
                if chunk.empty:
                    continue
                payload[str(symbol)] = chunk.iloc[-1].to_dict()
        else:
            payload["__all__"] = frame.iloc[-1].to_dict()
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _write_symbol_parquet(frame: pd.DataFrame, out_dir: Path, suffix: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for symbol, chunk in frame.groupby("symbol", sort=False):
        symbol_key = symbol.replace("/", "_").replace(":", "_")
        out_path = out_dir / f"{symbol_key}-{suffix}.parquet"
        chunk.to_parquet(out_path, compression="snappy", index=False)


def main() -> None:
    args = parse_args()
    input_paths = [Path(item.strip()) for item in args.input.split(",") if item.strip()]
    if not input_paths:
        raise RuntimeError("No input files provided.")

    raw_frames = []
    for path in input_paths:
        if not path.exists():
            raise FileNotFoundError(path)
        raw_frames.append(_read_input(path))
    raw = pd.concat(raw_frames, ignore_index=True)

    cleaner = CryptoDataCleaner(args.exchange)
    seg_cfg = TradeSegmentationConfig(
        window=args.window,
        min_periods=args.min_periods,
        whale_zscore=args.whale_zscore,
        min_notional=args.min_notional,
    )
    cleaned = cleaner.clean_trades(raw, config=seg_cfg)

    cleaned_dir = Path(args.cleaned_dir) / args.exchange
    _write_symbol_parquet(cleaned, cleaned_dir, "cleaned")

    whale_trades = cleaned[cleaned.get("is_whale", False)].copy()
    retail_trades = cleaned[cleaned.get("is_retail", False)].copy()

    whale_dir = Path(args.whale_dir) / args.exchange
    retail_dir = Path(args.retail_dir) / args.exchange
    if not whale_trades.empty:
        _write_symbol_parquet(whale_trades, whale_dir, "whale")
    if not retail_trades.empty:
        _write_symbol_parquet(retail_trades, retail_dir, "retail")

    whale_flow = cleaner.aggregate_whale_flow(
        whale_trades,
        timeframe=args.timeframe,
        window=args.window,
        min_periods=args.min_periods,
    )
    retail_fomo = cleaner.aggregate_retail_fomo(
        retail_trades,
        timeframe=args.timeframe,
        window=args.fomo_window,
        min_periods=max(5, args.min_periods // 2),
    )

    metrics_dir = Path(args.metrics_dir) / args.exchange
    metrics_dir.mkdir(parents=True, exist_ok=True)
    whale_flow.to_parquet(
        metrics_dir / f"whale-flow-{args.timeframe}.parquet",
        compression="snappy",
        index=False,
    )
    retail_fomo.to_parquet(
        metrics_dir / f"retail-fomo-{args.timeframe}.parquet",
        compression="snappy",
        index=False,
    )
    if not whale_flow.empty:
        _write_symbol_parquet(whale_flow, metrics_dir, f"whale-flow-{args.timeframe}")
    if not retail_fomo.empty:
        _write_symbol_parquet(retail_fomo, metrics_dir, f"retail-fomo-{args.timeframe}")

    report_dir = Path(args.report_dir) / args.exchange
    report_dir.mkdir(parents=True, exist_ok=True)
    quality = cleaner.summarize_trade_quality(cleaned)
    (report_dir / "trades-quality.json").write_text(
        json.dumps(quality, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    _write_latest_report(whale_flow, report_dir / "whale-flow-latest.json")
    _write_latest_report(retail_fomo, report_dir / "retail-fomo-latest.json")

    logger.info("Trade ETL complete. Cleaned rows: %s", len(cleaned))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    main()
