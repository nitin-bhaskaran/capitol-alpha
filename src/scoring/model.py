"""Event-alpha ensemble model for disclosure-driven trading signals."""

import json
import math
from pathlib import Path
from typing import Any, Dict, Optional

from src.config import Config, PROJECT_ROOT
from src.scoring.features import build_event_features, clamp


DEFAULT_GROUP_STATS = {
    "global": {"n": 30, "mean_excess_return": 0.004, "win_rate": 0.52},
    "source": {
        "house_watcher": {"n": 12, "mean_excess_return": 0.005, "win_rate": 0.53},
        "senate_watcher": {"n": 12, "mean_excess_return": 0.005, "win_rate": 0.53},
        "capitol_trades": {"n": 8, "mean_excess_return": 0.003, "win_rate": 0.51},
        "trump_tracker": {"n": 2, "mean_excess_return": 0.0, "win_rate": 0.50},
    },
    "sector": {
        "technology": {"n": 12, "mean_excess_return": 0.006, "win_rate": 0.54},
        "semiconductors": {"n": 8, "mean_excess_return": 0.008, "win_rate": 0.55},
        "defense": {"n": 10, "mean_excess_return": 0.006, "win_rate": 0.54},
        "financials": {"n": 8, "mean_excess_return": 0.004, "win_rate": 0.52},
        "energy": {"n": 8, "mean_excess_return": 0.003, "win_rate": 0.51},
        "healthcare": {"n": 8, "mean_excess_return": 0.004, "win_rate": 0.52},
        "unknown": {"n": 2, "mean_excess_return": 0.001, "win_rate": 0.50},
    },
    "politician": {
        "nancy pelosi": {"n": 10, "mean_excess_return": 0.010, "win_rate": 0.57},
        "dan crenshaw": {"n": 6, "mean_excess_return": 0.006, "win_rate": 0.54},
        "tommy tuberville": {"n": 6, "mean_excess_return": 0.004, "win_rate": 0.52},
    },
}


class EventAlphaEnsemble:
    """Institutional-style event model with Bayesian shrinkage and calibration hooks."""

    def __init__(self, config: Config):
        self.config = config
        self.artifact = self._load_artifact()
        self.model_version = self.artifact.get(
            "model_version", config.model.default_model_version
        )
        self.group_stats = self.artifact.get("group_stats", DEFAULT_GROUP_STATS)
        self.calibration = self.artifact.get(
            "calibration", {"intercept": 0.0, "slope": 1.0}
        )

    def _load_artifact(self) -> Dict[str, Any]:
        path = Path(self.config.model.active_model_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.exists():
            return {
                "model_version": self.config.model.default_model_version,
                "group_stats": DEFAULT_GROUP_STATS,
                "calibration": {"intercept": 0.0, "slope": 1.0},
                "metadata": {"source": "built_in_default"},
            }
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def score_trade(self, trade: Dict[str, Any], rank_order: Optional[int] = None) -> Dict[str, Any]:
        features = build_event_features(trade, self.config)
        direction = features["direction"]
        expected = self._expected_excess_return(features)
        raw_confidence = self._raw_confidence(features, expected)
        confidence = self._calibrate(raw_confidence)
        alpha_score = self._alpha_score(expected, confidence, features)

        if direction != "BUY":
            tradeable = False
            suggested_action = "REDUCE_IF_HELD" if confidence >= self.config.model.min_alert_confidence else "NO_TRADE"
            decision_reason = "Long-only ISA mode blocks new short exposure."
        else:
            tradeable = (
                confidence >= self.config.model.min_trade_confidence
                and expected >= self.config.model.min_expected_excess_return
            )
            if tradeable and alpha_score >= 0.78:
                suggested_action = "HIGH_CONVICTION"
            elif tradeable:
                suggested_action = "SUGGEST_TRADE"
            elif confidence >= self.config.model.min_alert_confidence:
                suggested_action = "ALERT_ONLY"
            else:
                suggested_action = "LOG_ONLY"
            decision_reason = self._decision_reason(features, expected, confidence, tradeable)

        breakdown = self._breakdown(features, expected, confidence)
        return {
            "raw_trade_id": trade["id"],
            "ticker": features["ticker"],
            "politician_name": features["politician_name"],
            "direction": direction,
            "alpha_score": round(alpha_score, 3),
            "score_breakdown": json.dumps(breakdown, sort_keys=True),
            "vip_tier": features["vip_tier"],
            "filing_gap_days": features["filing_gap_days"],
            "suggested_action": suggested_action,
            "model_version": self.model_version,
            "expected_excess_return": round(expected, 5),
            "confidence": round(confidence, 4),
            "tradeable": 1 if tradeable else 0,
            "rank_order": rank_order,
            "execution_mode": self.config.execution.mode,
            "decision_reason": decision_reason,
            "feature_hash": features["feature_hash"],
            "_features": features,
        }

    def _expected_excess_return(self, features: Dict[str, Any]) -> float:
        global_stat = self.group_stats["global"]
        global_mean = global_stat["mean_excess_return"]

        source_mean = self._shrunk_mean("source", features["source"], global_mean)
        sector_mean = self._shrunk_mean("sector", features["sector"], global_mean)
        politician_key = features["politician_name"].lower()
        politician_mean = self._shrunk_mean("politician", politician_key, global_mean)

        feature_alpha = (
            0.0060 * features["vip_score"]
            + 0.0050 * features["committee_score"]
            + 0.0045 * features["filing_speed_score"]
            + 0.0035 * features["amount_score"]
            + 0.0025 * features["source_quality"]
            + 0.0020 * features["recency_score"]
        )
        ensemble_mean = (
            0.35 * politician_mean
            + 0.25 * sector_mean
            + 0.20 * source_mean
            + 0.20 * global_mean
        )
        expected = ensemble_mean + feature_alpha - 0.010
        if features["direction"] == "SELL":
            expected = -abs(expected) * 0.5
        return max(-0.05, min(0.12, expected))

    def _shrunk_mean(self, group: str, key: str, global_mean: float) -> float:
        stats = self.group_stats.get(group, {})
        stat = stats.get(key) or stats.get(key.lower())
        if not stat:
            return global_mean
        n = max(0, float(stat.get("n", 0)))
        mean = float(stat.get("mean_excess_return", global_mean))
        prior_n = 20.0
        return (prior_n * global_mean + n * mean) / (prior_n + n)

    def _raw_confidence(self, features: Dict[str, Any], expected: float) -> float:
        evidence_quality = (
            0.20 * features["source_quality"]
            + 0.18 * features["filing_speed_score"]
            + 0.18 * features["amount_score"]
            + 0.18 * features["vip_score"]
            + 0.16 * features["committee_score"]
            + 0.10 * features["recency_score"]
        )
        expected_component = clamp(0.50 + expected / 0.08)
        return clamp(0.55 * evidence_quality + 0.45 * expected_component)

    def _calibrate(self, confidence: float) -> float:
        confidence = clamp(confidence, 0.001, 0.999)
        intercept = float(self.calibration.get("intercept", 0.0))
        slope = float(self.calibration.get("slope", 1.0))
        logit = math.log(confidence / (1.0 - confidence))
        calibrated = 1.0 / (1.0 + math.exp(-(intercept + slope * logit)))
        return clamp(calibrated)

    def _alpha_score(self, expected: float, confidence: float, features: Dict[str, Any]) -> float:
        return clamp(
            0.42
            + expected / 0.06
            + 0.25 * (confidence - 0.5)
            + 0.08 * features["source_quality"]
        )

    def _breakdown(self, features: Dict[str, Any], expected: float, confidence: float) -> Dict[str, float]:
        return {
            "bayesian_expected_excess_return": round(expected, 5),
            "calibrated_confidence": round(confidence, 4),
            "vip": round(features["vip_score"], 3),
            "committee": round(features["committee_score"], 3),
            "filing_speed": round(features["filing_speed_score"], 3),
            "trade_size": round(features["amount_score"], 3),
            "source_quality": round(features["source_quality"], 3),
            "recency": round(features["recency_score"], 3),
        }

    def _decision_reason(
        self,
        features: Dict[str, Any],
        expected: float,
        confidence: float,
        tradeable: bool,
    ) -> str:
        if tradeable:
            return (
                f"Tradeable long event: expected excess {expected:.2%}, "
                f"confidence {confidence:.1%}, sector {features['sector']}."
            )
        blockers = []
        if confidence < self.config.model.min_trade_confidence:
            blockers.append(f"confidence {confidence:.1%} below trade threshold")
        if expected < self.config.model.min_expected_excess_return:
            blockers.append(f"expected excess {expected:.2%} below hurdle")
        return "; ".join(blockers) or "Model blocked trade."
