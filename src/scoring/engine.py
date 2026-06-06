"""Model-driven event-alpha scoring engine."""

import logging
from typing import Any, Dict, List

from src.config import Config
from src.database import Database
from src.scoring.model import EventAlphaEnsemble

logger = logging.getLogger("capitol_alpha.scoring")


class ScoringEngine:
    """Scores disclosures with the active event-alpha ensemble model."""

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.model = EventAlphaEnsemble(config)
        self._ensure_default_model_run()

    def _ensure_default_model_run(self):
        if self.db.get_active_model_run():
            return
        self.db.insert_model_run({
            "model_version": self.model.model_version,
            "train_start": None,
            "train_end": None,
            "validation_metrics": {
                "status": "default_priors",
                "note": "Built-in priors active until research training is run.",
            },
            "calibration_metrics": {
                "status": "uncalibrated_default",
                "calibration_ok": False,
            },
            "metadata": self.model.artifact.get("metadata", {}),
            "active": 1,
        })

    def score_unscored_trades(self) -> List[Dict[str, Any]]:
        """
        Score all raw trades that do not have a signal.

        The model first scores the full batch, then ranks BUY candidates
        cross-sectionally by expected return and calibrated confidence.
        """
        unscored = self.db.get_unscored_trades()
        logger.info("Scoring %s unscored trades", len(unscored))

        scored = [self.model.score_trade(trade) for trade in unscored]
        ranked = sorted(
            scored,
            key=lambda s: (
                s["direction"] != "BUY",
                -(s.get("expected_excess_return") or 0) * (s.get("confidence") or 0),
                -(s.get("alpha_score") or 0),
            ),
        )

        new_signals = []
        for rank, signal in enumerate(ranked, start=1):
            signal["rank_order"] = rank
            features = signal.pop("_features")
            signal_id = self.db.insert_signal(signal)
            if not signal_id:
                continue
            signal["id"] = signal_id
            self.db.insert_feature_snapshot({
                "signal_id": signal_id,
                "raw_trade_id": signal["raw_trade_id"],
                "feature_hash": signal["feature_hash"],
                "model_version": signal["model_version"],
                "features": features,
                "market_context": {
                    "source": features["source"],
                    "sector": features["sector"],
                    "long_only_mode": True,
                },
            })
            new_signals.append(signal)

        logger.info("Created %s new signals", len(new_signals))
        return new_signals
