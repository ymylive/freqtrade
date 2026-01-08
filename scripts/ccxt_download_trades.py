#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import ccxt
import pandas as pd


logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download trades via CCXT and store parquet.")
    parser.add_argument("--exchange", required=True, help="CCXT exchange id.")
    parser.add_argument("--pairs", required=True, help="Comma-separated pairs.")
    parser.add_argument("--days", type=int, default=3, help="Number of days to fetch.")
    parser.add_argument("--limit", type=int, default=1000, help="Trades per request.")
    parser.add_argument("--max-trades", type=int, default=200000, help="Max trades per pair.")
    parser.add_argument(
        "--out-dir",
        default="user_data/trades_raw",
        help="Output directory for parquet files.",
    )
    return parser.parse_args()


def _pair_key(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_")


def _write_batch(out_dir: Path, symbol: str, index: int, rows: list[dict]) -> None:
    if not rows:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    path = out_dir / f"{_pair_key(symbol)}-trades-{index:04d}.parquet"
    frame.to_parquet(path, compression="snappy", index=False)


def _fetch_pair(
    exchange: ccxt.Exchange,
    symbol: str,
    *,
    since_ms: int,
    limit: int,
    max_trades: int,
    out_dir: Path,
) -> int:
    total = 0
    batch_index = 0
    next_since = since_ms
    last_ts = None
    buffer: list[dict] = []

    while total < max_trades:
        try:
            trades = exchange.fetch_trades(symbol, since=next_since, limit=limit)
        except Exception as exc:
            logger.warning("Fetch trades failed for %s: %s", symbol, exc)
            break
        if not trades:
            break
        for trade in trades:
            buffer.append(
                {
                    "timestamp": int(trade.get("timestamp") or 0),
                    "symbol": trade.get("symbol") or symbol,
                    "price": float(trade.get("price") or 0.0),
                    "amount": float(trade.get("amount") or 0.0),
                    "side": trade.get("side") or "",
                    "id": trade.get("id"),
                }
            )
        total += len(trades)
        last_trade_ts = trades[-1].get("timestamp")
        if last_trade_ts is None:
            break
        if last_ts is not None and last_trade_ts <= last_ts:
            break
        last_ts = last_trade_ts
        next_since = int(last_trade_ts) + 1

        if len(buffer) >= 20000:
            _write_batch(out_dir, symbol, batch_index, buffer)
            buffer = []
            batch_index += 1

        if getattr(exchange, "rateLimit", 0):
            time.sleep(exchange.rateLimit / 1000)

    if buffer:
        _write_batch(out_dir, symbol, batch_index, buffer)
    return total


def main() -> None:
    args = parse_args()
    exchange = getattr(ccxt, args.exchange)(
        {"enableRateLimit": True, "options": {"defaultType": "swap"}}
    )
    exchange.load_markets()

    symbols = [item.strip() for item in args.pairs.split(",") if item.strip()]
    if not symbols:
        raise RuntimeError("No pairs provided.")

    since = datetime.now(UTC) - timedelta(days=int(args.days))
    since_ms = int(since.timestamp() * 1000)
    out_dir = Path(args.out_dir) / args.exchange

    for symbol in symbols:
        logger.info("Downloading trades: %s", symbol)
        count = _fetch_pair(
            exchange,
            symbol,
            since_ms=since_ms,
            limit=int(args.limit),
            max_trades=int(args.max_trades),
            out_dir=out_dir,
        )
        logger.info("Trades downloaded: %s => %s", symbol, count)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    main()
