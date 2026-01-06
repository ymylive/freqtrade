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
STATE_VERSION = 2


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
        smoothing: float = 0.2,
        weight_scale: float = 0.02,
        weight_min: float = 0.6,
        weight_max: float = 2.5,
        win_rate_bias: float = 0.15,
    ) -> None:
        self.state_path = state_path
        self.min_trades = max(1, int(min_trades))
        self.profit_threshold = float(profit_threshold)
        self.smoothing = _clamp(float(smoothing), 0.01, 1.0)
        self.weight_scale = max(1e-6, float(weight_scale))
        self.weight_min = max(0.1, float(weight_min))
        self.weight_max = max(self.weight_min, float(weight_max))
        self.win_rate_bias = _clamp(float(win_rate_bias), 0.0, 0.5)
        self._state = self._load_state()

    def update_from_trade(
        self,
        side: str,
        profit_ratio: float,
        features: Mapping[str, float],
        *,
        segment: str = DEFAULT_SEGMENT,
        save: bool = True,
    ) -> None:
        if side not in ("long", "short"):
            return
        stats = self._get_segment(segment)[side]
        is_win = profit_ratio > self.profit_threshold
        weight = self._trade_weight(profit_ratio)
        self._update_stats(stats, is_win, features, weight=weight)
        self._state["updated_at"] = datetime.now(UTC).isoformat()
        if save:
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
        long_win_rate = self._win_rate(long_stats)
        short_win_rate = self._win_rate(short_stats)

        rsi_long = self._derive_threshold(
            long_stats,
            "rsi",
            prefer_higher=True,
            win_rate=long_win_rate,
        )
        if rsi_long is not None and "rsi_long" in bounds:
            thresholds["rsi_long"] = _clamp(rsi_long, *bounds["rsi_long"])

        rsi_short = self._derive_threshold(
            short_stats,
            "rsi",
            prefer_higher=False,
            win_rate=short_win_rate,
        )
        if rsi_short is not None and "rsi_short" in bounds:
            thresholds["rsi_short"] = _clamp(rsi_short, *bounds["rsi_short"])

        macd_hist = self._derive_threshold(
            long_stats,
            "macd_hist",
            prefer_higher=True,
            win_rate=long_win_rate,
        )
        if macd_hist is not None and "macd_hist" in bounds:
            thresholds["macd_hist"] = _clamp(macd_hist, *bounds["macd_hist"])

        ema_diff = self._derive_threshold(
            long_stats,
            "ema_diff",
            prefer_higher=True,
            win_rate=long_win_rate,
        )
        if ema_diff is not None and "ema_diff" in bounds:
            thresholds["ema_diff"] = _clamp(ema_diff, *bounds["ema_diff"])

        atr_pct = self._derive_threshold(
            long_stats,
            "atr_pct",
            prefer_higher=True,
            win_rate=long_win_rate,
        )
        if atr_pct is not None and "atr_pct" in bounds:
            thresholds["atr_pct"] = _clamp(atr_pct, *bounds["atr_pct"])

        volume_zscore = self._derive_threshold(
            long_stats,
            "volume_zscore",
            prefer_higher=True,
            win_rate=long_win_rate,
        )
        if volume_zscore is not None and "volume_zscore" in bounds:
            thresholds["volume_zscore"] = _clamp(volume_zscore, *bounds["volume_zscore"])

        return thresholds

    def _derive_threshold(
        self,
        stats: Mapping[str, Any],
        feature_key: str,
        *,
        prefer_higher: bool,
        win_rate: float | None,
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
        base = (mean_win + mean_loss) / 2.0
        if win_rate is None:
            return base
        bias = _clamp((win_rate - 0.5) * 2.0, -1.0, 1.0) * self.win_rate_bias
        return base + bias * (mean_win - mean_loss)

    @staticmethod
    def _update_stats(
        stats: dict[str, Any],
        is_win: bool,
        features: Mapping[str, float],
        *,
        weight: float,
    ) -> None:
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
            prev = float(mean_map.get(key, value))
            if count == 0:
                mean_map[key] = value
            else:
                mean_map[key] = prev + (value - prev) * weight

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
            "version": STATE_VERSION,
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
            "version": STATE_VERSION,
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

    def _trade_weight(self, profit_ratio: float) -> float:
        magnitude = abs(float(profit_ratio))
        scaled = magnitude / self.weight_scale if self.weight_scale else 1.0
        scaled = _clamp(scaled, self.weight_min, self.weight_max)
        return _clamp(self.smoothing * scaled, 0.01, 1.0)

    @staticmethod
    def _win_rate(stats: Mapping[str, Any]) -> float | None:
        wins = int(stats.get("wins", 0))
        losses = int(stats.get("losses", 0))
        total = wins + losses
        if total <= 0:
            return None
        return wins / total

    def apply_feedback_payload(self, payload: Mapping[str, Any]) -> bool:
        if not isinstance(payload, Mapping):
            return False
        side = payload.get("side")
        if side not in ("long", "short"):
            return False
        profit_ratio = payload.get("profit_ratio")
        if profit_ratio is None:
            return False
        features = payload.get("entry_features") or payload.get("features")
        if not isinstance(features, Mapping):
            return False
        segment = payload.get("segment") or DEFAULT_SEGMENT
        self.update_from_trade(
            str(side),
            float(profit_ratio),
            features,
            segment=str(segment),
            save=False,
        )
        return True

    def replay_feedback(self, feedback_path: Path) -> int:
        if not feedback_path.exists():
            return 0
        updated = 0
        for raw_line in feedback_path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                continue
            if self.apply_feedback_payload(payload):
                updated += 1
        if updated:
            self._state["updated_at"] = datetime.now(UTC).isoformat()
            self._save_state()
        return updated

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
