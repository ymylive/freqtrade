from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


FEATURE_KEYS = (
    "rsi",
    "macd_hist",
    "ema_diff",
    "atr_pct",
    "volume_zscore",
    "price_change_1h",
    "price_change_4h",
)
DEFAULT_SEGMENT = "default"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _empty_stats() -> dict[str, Any]:
    return {
        "wins": 0,
        "losses": 0,
        "mean_win": {},
        "mean_loss": {},
    }


def _empty_segment() -> dict[str, Any]:
    return {"long": _empty_stats(), "short": _empty_stats()}


class AiFeedbackStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append_trade_feedback(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")


class AiIterationTuner:
    def __init__(
        self,
        state_path: Path,
        *,
        min_trades: int = 8,
        profit_threshold: float = 0.0,
    ) -> None:
        self.state_path = state_path
        self.min_trades = max(1, int(min_trades))
        self.profit_threshold = float(profit_threshold)
        self._state = self._load_state()

    def update_from_trade(
        self,
        side: str,
        profit_ratio: float,
        features: Mapping[str, float],
        *,
        segment: str = DEFAULT_SEGMENT,
    ) -> None:
        if side not in ("long", "short"):
            return
        stats = self._get_segment(segment)[side]
        is_win = profit_ratio > self.profit_threshold
        self._update_stats(stats, is_win, features)
        self._state["updated_at"] = datetime.now(UTC).isoformat()
        self._save_state()

    def compute_thresholds(
        self,
        *,
        defaults: Mapping[str, float],
        bounds: Mapping[str, tuple[float, float]],
        segment: str = DEFAULT_SEGMENT,
    ) -> dict[str, float]:
        thresholds = dict(defaults)
        segment_state = self._get_segment(segment)
        long_stats = segment_state["long"]
        short_stats = segment_state["short"]

        rsi_long = self._derive_threshold(long_stats, "rsi", prefer_higher=True)
        if rsi_long is not None and "rsi_long" in bounds:
            thresholds["rsi_long"] = _clamp(rsi_long, *bounds["rsi_long"])

        rsi_short = self._derive_threshold(short_stats, "rsi", prefer_higher=False)
        if rsi_short is not None and "rsi_short" in bounds:
            thresholds["rsi_short"] = _clamp(rsi_short, *bounds["rsi_short"])

        macd_hist = self._derive_threshold(long_stats, "macd_hist", prefer_higher=True)
        if macd_hist is not None and "macd_hist" in bounds:
            thresholds["macd_hist"] = _clamp(macd_hist, *bounds["macd_hist"])

        ema_diff = self._derive_threshold(long_stats, "ema_diff", prefer_higher=True)
        if ema_diff is not None and "ema_diff" in bounds:
            thresholds["ema_diff"] = _clamp(ema_diff, *bounds["ema_diff"])

        atr_pct = self._derive_threshold(long_stats, "atr_pct", prefer_higher=True)
        if atr_pct is not None and "atr_pct" in bounds:
            thresholds["atr_pct"] = _clamp(atr_pct, *bounds["atr_pct"])

        volume_zscore = self._derive_threshold(long_stats, "volume_zscore", prefer_higher=True)
        if volume_zscore is not None and "volume_zscore" in bounds:
            thresholds["volume_zscore"] = _clamp(volume_zscore, *bounds["volume_zscore"])

        return thresholds

    def _derive_threshold(
        self,
        stats: Mapping[str, Any],
        feature_key: str,
        *,
        prefer_higher: bool,
    ) -> float | None:
        wins = int(stats.get("wins", 0))
        losses = int(stats.get("losses", 0))
        if wins + losses < self.min_trades or wins == 0 or losses == 0:
            return None
        mean_win = stats.get("mean_win", {}).get(feature_key)
        mean_loss = stats.get("mean_loss", {}).get(feature_key)
        if mean_win is None or mean_loss is None:
            return None
        mean_win = float(mean_win)
        mean_loss = float(mean_loss)
        if prefer_higher and mean_win <= mean_loss:
            return None
        if not prefer_higher and mean_win >= mean_loss:
            return None
        return (mean_win + mean_loss) / 2.0

    @staticmethod
    def _update_stats(stats: dict[str, Any], is_win: bool, features: Mapping[str, float]) -> None:
        count_key = "wins" if is_win else "losses"
        mean_key = "mean_win" if is_win else "mean_loss"
        count = int(stats.get(count_key, 0))
        mean_map = dict(stats.get(mean_key, {}))

        for key in FEATURE_KEYS:
            if key not in features:
                continue
            value = features.get(key)
            if value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            prev = float(mean_map.get(key, 0.0))
            mean_map[key] = (prev * count + value) / (count + 1)

        stats[mean_key] = mean_map
        stats[count_key] = count + 1

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._default_state()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return self._default_state()
        return self._normalize_state(data)

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(self._state, ensure_ascii=True, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.state_path)

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "version": 1,
            "updated_at": None,
            "segments": {DEFAULT_SEGMENT: _empty_segment()},
        }

    @staticmethod
    def _normalize_state(data: Mapping[str, Any]) -> dict[str, Any]:
        raw_segments = data.get("segments")
        segments: dict[str, Any] = raw_segments if isinstance(raw_segments, dict) else {}
        normalized_segments: dict[str, Any] = {}
        for key, segment in segments.items():
            if not isinstance(segment, dict):
                normalized_segments[key] = _empty_segment()
                continue
            long_raw = segment.get("long")
            short_raw = segment.get("short")
            long_stats = long_raw if isinstance(long_raw, dict) else _empty_stats()
            short_stats = short_raw if isinstance(short_raw, dict) else _empty_stats()
            normalized_segments[str(key)] = {"long": long_stats, "short": short_stats}

        segments_state: dict[str, Any] = normalized_segments or {
            DEFAULT_SEGMENT: _empty_segment()
        }
        state = {
            "version": 1,
            "updated_at": data.get("updated_at"),
            "segments": segments_state,
        }

        for segment_state in segments_state.values():
            for side in ("long", "short"):
                stats = segment_state.get(side)
                if not isinstance(stats, dict):
                    segment_state[side] = _empty_stats()
                    stats = segment_state[side]
                stats.setdefault("wins", 0)
                stats.setdefault("losses", 0)
                stats.setdefault("mean_win", {})
                stats.setdefault("mean_loss", {})
        return state

    def get_segment_summary(self, segment: str = DEFAULT_SEGMENT) -> dict[str, Any]:
        seg = self._get_segment(segment)
        long_stats = seg["long"]
        short_stats = seg["short"]
        return {
            "segment": segment,
            "wins": int(long_stats.get("wins", 0)) + int(short_stats.get("wins", 0)),
            "losses": int(long_stats.get("losses", 0)) + int(short_stats.get("losses", 0)),
            "updated_at": self._state.get("updated_at"),
        }

    def _get_segment(self, segment: str) -> dict[str, Any]:
        segment_key = segment or DEFAULT_SEGMENT
        segments = self._state.setdefault("segments", {})
        if segment_key not in segments or not isinstance(segments.get(segment_key), dict):
            segments[segment_key] = _empty_segment()
        segment_state = segments[segment_key]
        if "long" not in segment_state or "short" not in segment_state:
            segments[segment_key] = _empty_segment()
            segment_state = segments[segment_key]
        return segment_state
