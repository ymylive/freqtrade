#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from freqtrade.ai_iteration import AiIterationTuner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay AI iteration feedback JSONL into tuner state.",
    )
    parser.add_argument(
        "--state",
        required=True,
        help="State file path to update (json).",
    )
    parser.add_argument(
        "--feedback",
        required=True,
        help="Feedback JSONL file path(s), comma separated.",
    )
    parser.add_argument("--min-trades", type=int, default=10)
    parser.add_argument("--profit-threshold", type=float, default=0.0)
    parser.add_argument("--smoothing", type=float, default=0.2)
    parser.add_argument("--weight-scale", type=float, default=0.02)
    parser.add_argument("--weight-min", type=float, default=0.6)
    parser.add_argument("--weight-max", type=float, default=2.5)
    parser.add_argument("--win-rate-bias", type=float, default=0.15)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing state before replay.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    state_path = Path(args.state)
    if args.reset and state_path.exists():
        state_path.unlink()

    feedback_paths = [Path(item.strip()) for item in args.feedback.split(",") if item.strip()]
    if not feedback_paths:
        raise RuntimeError("No feedback paths provided.")

    tuner = AiIterationTuner(
        state_path,
        min_trades=args.min_trades,
        profit_threshold=args.profit_threshold,
        smoothing=args.smoothing,
        weight_scale=args.weight_scale,
        weight_min=args.weight_min,
        weight_max=args.weight_max,
        win_rate_bias=args.win_rate_bias,
    )

    total = 0
    for path in feedback_paths:
        total += tuner.replay_feedback(path)

    segments = tuner._state.get("segments", {}) if hasattr(tuner, "_state") else {}
    print(f"Replay complete. Updated entries: {total}")
    for segment in segments:
        summary = tuner.get_segment_summary(segment)
        print(
            f"Segment={segment} wins={summary['wins']} losses={summary['losses']} "
            f"updated_at={summary['updated_at']}"
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover
        print(f"Replay failed: {exc}", file=sys.stderr)
        sys.exit(1)
