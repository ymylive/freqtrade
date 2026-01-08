#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from freqtrade.ai_iteration import AiIterationTuner


logger = logging.getLogger(__name__)


DEFAULT_THRESHOLDS = {
    "rsi_long": 56.0,
    "rsi_short": 44.0,
    "macd_hist": 0.0,
    "ema_diff": 0.002,
    "atr_pct": 0.004,
    "volume_zscore": 0.0,
}

OFFSET_BOUNDS = {
    "rsi_long": (-10.0, 10.0),
    "rsi_short": (-10.0, 10.0),
    "macd_hist": (-0.01, 0.01),
    "ema_diff": (-0.005, 0.005),
    "atr_pct": (-0.01, 0.01),
    "volume_zscore": (-1.0, 1.0),
}

OFFSET_STEP_LIMITS = {
    "rsi_long": 1.0,
    "rsi_short": 1.0,
    "macd_hist": 0.001,
    "ema_diff": 0.0005,
    "atr_pct": 0.0005,
    "volume_zscore": 0.1,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LLM-driven strategy iteration runner.")
    parser.add_argument("--config", required=True, help="Freqtrade config json path.")
    parser.add_argument("--segment", help="Segment name override (main/alt).")
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--output", help="JSONL output path for iteration history.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep-seconds", type=int, default=2)
    parser.add_argument("--etl-report", help="Cleaned OHLCV quality report (json).")
    parser.add_argument("--trades-report", help="Cleaned trades quality report (json).")
    parser.add_argument("--whale-report", help="Whale flow latest report (json).")
    parser.add_argument("--retail-report", help="Retail fomo latest report (json).")
    parser.add_argument("--api-url", help="Override AI API url.")
    parser.add_argument("--api-key", help="Override AI API key.")
    parser.add_argument("--model", help="Override AI model.")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=2000)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--max-feedback", type=int, default=2000)
    parser.add_argument("--max-retries", type=int, default=2)
    return parser.parse_args()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Failed to read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Config must be a json object: {path}")
    return payload


def _load_optional_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _resolve_path(base_dir: Path, raw_path: str | None, fallback: str) -> Path:
    path = Path(raw_path or fallback)
    if not path.is_absolute():
        path = base_dir / path
    return path


def _tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists() or limit <= 0:
        return []
    buffer: deque[dict[str, Any]] = deque(maxlen=limit)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            buffer.append(payload)
    return list(buffer)


def _summarize_feedback(entries: list[dict[str, Any]]) -> dict[str, Any]:
    total = 0
    wins = 0
    losses = 0
    sum_profit = 0.0
    sum_win = 0.0
    sum_loss = 0.0
    last_ts = None
    for payload in entries:
        profit = payload.get("profit_ratio")
        if profit is None:
            continue
        try:
            profit = float(profit)
        except (TypeError, ValueError):
            continue
        total += 1
        sum_profit += profit
        if profit > 0:
            wins += 1
            sum_win += profit
        else:
            losses += 1
            sum_loss += profit
        last_ts = payload.get("exit_time") or payload.get("timestamp") or last_ts
    avg_profit = sum_profit / total if total else 0.0
    avg_win = sum_win / wins if wins else 0.0
    avg_loss = sum_loss / losses if losses else 0.0
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / total if total else 0.0,
        "avg_profit": avg_profit,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "last_event": last_ts,
    }


def _current_thresholds(offsets: dict[str, float]) -> dict[str, float]:
    thresholds = dict(DEFAULT_THRESHOLDS)
    for key, value in offsets.items():
        if key in thresholds:
            thresholds[key] = thresholds[key] + float(value)
    return thresholds


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _apply_offset_delta(
    offsets: dict[str, float],
    delta: dict[str, Any],
) -> dict[str, float]:
    updated = dict(offsets)
    for key, step in delta.items():
        if key not in OFFSET_BOUNDS:
            continue
        try:
            step_value = float(step)
        except (TypeError, ValueError):
            continue
        max_step = OFFSET_STEP_LIMITS.get(key, abs(step_value))
        step_value = _clamp(step_value, -max_step, max_step)
        low, high = OFFSET_BOUNDS[key]
        updated[key] = _clamp(updated.get(key, 0.0) + step_value, low, high)
    return updated


def _extract_json_payload(text: str) -> dict[str, Any] | list[Any] | None:
    obj_start = text.find("{")
    obj_end = text.rfind("}")
    if obj_start != -1 and obj_end != -1 and obj_end > obj_start:
        snippet = text[obj_start : obj_end + 1]
        try:
            payload = json.loads(snippet)
        except (json.JSONDecodeError, ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            return payload

    arr_start = text.find("[")
    arr_end = text.rfind("]")
    if arr_start != -1 and arr_end != -1 and arr_end > arr_start:
        snippet = text[arr_start : arr_end + 1]
        try:
            payload = json.loads(snippet)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
        if isinstance(payload, list):
            return payload
    return None


def _call_ai(
    api_url: str,
    api_key: str,
    model: str,
    prompt: str,
    *,
    temperature: float,
    max_tokens: int,
    timeout: int,
) -> dict[str, Any] | list[Any] | None:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a quantitative strategy researcher."},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        response = requests.post(api_url, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.warning("AI request failed: %s", exc)
        return None
    choices = data.get("choices") or []
    if not choices:
        return None
    content = choices[0].get("message", {}).get("content", "")
    if not content:
        return None
    return _extract_json_payload(content)


def _normalize_iterations(payload: dict[str, Any] | list[Any] | None) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    iterations = payload.get("iterations")
    if isinstance(iterations, list):
        return [item for item in iterations if isinstance(item, dict)]
    return []


def _build_prompt(
    *,
    segment: str,
    batch_size: int,
    offsets: dict[str, float],
    thresholds: dict[str, float],
    feedback_summary: dict[str, Any],
    segment_summary: dict[str, Any],
    data_context: dict[str, Any],
) -> str:
    return "\n".join(
        [
            "Return JSON only, no markdown or explanations.",
            "Output must be valid JSON and contain only the schema below.",
            (
                '{"iterations":[{"offset_delta":{"rsi_long":0.0,"rsi_short":0.0,'
                '"macd_hist":0.0,"ema_diff":0.0,"atr_pct":0.0,"volume_zscore":0.0},'
                '"confidence":0.5,"notes":"..."}]}'
            ),
            f"Segment: {segment}",
            f"Batch size: {batch_size}",
            f"Current offsets: {offsets}",
            f"Current thresholds: {thresholds}",
            f"Feedback summary: {feedback_summary}",
            f"Segment summary: {segment_summary}",
            f"Cleaned OHLCV report: {data_context.get('etl_report')}",
            f"Cleaned trades report: {data_context.get('trades_report')}",
            f"Whale flow report: {data_context.get('whale_report')}",
            f"Retail fomo report: {data_context.get('retail_report')}",
            "Rules:",
            "- offset_delta values must be small and within the given step limits.",
            "- prefer conservative adjustments if win_rate < 0.5.",
            "- keep total offsets within bounds; do not suggest large jumps.",
        ]
    )


def _resolve_api_args(args: argparse.Namespace) -> tuple[str, str, str]:
    api_url = args.api_url or os.getenv("AI_STRATEGY_API_URL", "")
    api_key = args.api_key or os.getenv("AI_STRATEGY_API_KEY", "")
    model = args.model or os.getenv("AI_STRATEGY_MODEL", "")
    if not api_url or not api_key or not model:
        raise RuntimeError("Missing AI_STRATEGY_API_URL / AI_STRATEGY_API_KEY / AI_STRATEGY_MODEL")
    return api_url, api_key, model


def _prepare_run(
    args: argparse.Namespace,
) -> tuple[
    dict[str, Any],
    Path,
    str,
    dict[str, float],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    Path,
]:
    config_path = Path(args.config)
    config = _load_json(config_path)
    base_dir = config_path.parent

    ai_cfg = config.get("ai_iteration", {})
    if not isinstance(ai_cfg, dict):
        raise RuntimeError("ai_iteration config missing")

    segment = args.segment or str(ai_cfg.get("segment", "default") or "default")
    offsets = (
        ai_cfg.get("threshold_offsets", {})
        if isinstance(ai_cfg.get("threshold_offsets"), dict)
        else {}
    )
    offsets = {key: float(value) for key, value in offsets.items() if key in DEFAULT_THRESHOLDS}

    state_path = _resolve_path(base_dir, ai_cfg.get("state_file"), "ai_iteration_state.json")
    feedback_path = _resolve_path(
        base_dir,
        ai_cfg.get("feedback_file"),
        "ai_iteration_feedback.jsonl",
    )

    tuner = AiIterationTuner(state_path, min_trades=1, profit_threshold=0.0)
    segment_summary = tuner.get_segment_summary(segment)
    feedback_entries = _tail_jsonl(feedback_path, args.max_feedback)
    feedback_summary = _summarize_feedback(feedback_entries)

    exchange_name = ""
    timeframe = str(config.get("timeframe", "1h"))
    exchange_cfg = config.get("exchange", {})
    if isinstance(exchange_cfg, dict):
        exchange_name = str(exchange_cfg.get("name", "") or "")
    etl_report_path = args.etl_report or (
        f"user_data/etl/reports/{exchange_name}/quality-{timeframe}.json"
        if exchange_name
        else ""
    )
    trades_report_path = args.trades_report or (
        f"user_data/etl/reports/{exchange_name}/trades-quality.json"
        if exchange_name
        else ""
    )
    whale_report_path = args.whale_report or (
        f"user_data/etl/reports/{exchange_name}/whale-flow-latest.json"
        if exchange_name
        else ""
    )
    retail_report_path = args.retail_report or (
        f"user_data/etl/reports/{exchange_name}/retail-fomo-latest.json"
        if exchange_name
        else ""
    )

    data_context = {
        "etl_report": _load_optional_json(
            _resolve_path(base_dir, etl_report_path, etl_report_path)
            if etl_report_path
            else None
        ),
        "trades_report": _load_optional_json(
            _resolve_path(base_dir, trades_report_path, trades_report_path)
            if trades_report_path
            else None
        ),
        "whale_report": _load_optional_json(
            _resolve_path(base_dir, whale_report_path, whale_report_path)
            if whale_report_path
            else None
        ),
        "retail_report": _load_optional_json(
            _resolve_path(base_dir, retail_report_path, retail_report_path)
            if retail_report_path
            else None
        ),
    }

    output_path = (
        Path(args.output)
        if args.output
        else base_dir / f"ai_iteration_runs_{segment}.jsonl"
    )
    if not output_path.is_absolute():
        output_path = base_dir / output_path

    return (
        config,
        base_dir,
        segment,
        offsets,
        feedback_summary,
        segment_summary,
        data_context,
        output_path,
    )


def _run_batches(
    *,
    args: argparse.Namespace,
    config: dict[str, Any],
    segment: str,
    offsets: dict[str, float],
    feedback_summary: dict[str, Any],
    segment_summary: dict[str, Any],
    data_context: dict[str, Any],
    output_path: Path,
    api_url: str,
    api_key: str,
    model: str,
) -> None:
    iterations = max(1, int(args.iterations))
    batch_size = max(1, int(args.batch_size))

    completed = 0
    while completed < iterations:
        remaining = iterations - completed
        batch = min(batch_size, remaining)
        prompt = _build_prompt(
            segment=segment,
            batch_size=batch,
            offsets=offsets,
            thresholds=_current_thresholds(offsets),
            feedback_summary=feedback_summary,
            segment_summary=segment_summary,
            data_context=data_context,
        )
        items: list[dict[str, Any]] = []
        for attempt in range(args.max_retries + 1):
            payload = _call_ai(
                api_url,
                api_key,
                model,
                prompt,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
            )
            items = _normalize_iterations(payload)
            if items:
                break
            if attempt < args.max_retries:
                prompt = f"{prompt}\nReturn the exact JSON schema, no extra text."
        if not items:
            raise RuntimeError("AI response iterations invalid")
        for item in items[:batch]:
            if not isinstance(item, dict):
                continue
            delta = item.get("offset_delta", {})
            if not isinstance(delta, dict):
                delta = {}
            updated = _apply_offset_delta(offsets, delta)
            record = {
                "timestamp": datetime.now(UTC).isoformat(),
                "segment": segment,
                "offsets_before": offsets,
                "offset_delta": delta,
                "offsets_after": updated,
                "confidence": float(item.get("confidence", 0.0) or 0.0),
                "notes": str(item.get("notes", "") or ""),
                "model": model,
            }
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True))
                handle.write("\n")
            offsets = updated
            config["ai_iteration"]["threshold_offsets"] = offsets
            if not args.dry_run:
                _save_json(Path(args.config), config)
            completed += 1
            if completed >= iterations:
                break
        time.sleep(max(0, args.sleep_seconds))

    logger.info("Completed iterations=%s for segment=%s", completed, segment)


def main() -> None:
    args = parse_args()
    api_url, api_key, model = _resolve_api_args(args)
    (
        config,
        _base_dir,
        segment,
        offsets,
        feedback_summary,
        segment_summary,
        data_context,
        output_path,
    ) = _prepare_run(args)
    _run_batches(
        args=args,
        config=config,
        segment=segment,
        offsets=offsets,
        feedback_summary=feedback_summary,
        segment_summary=segment_summary,
        data_context=data_context,
        output_path=output_path,
        api_url=api_url,
        api_key=api_key,
        model=model,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    main()
