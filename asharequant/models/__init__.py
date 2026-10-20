"""Stable public API; frozen implementations retain their audited paths."""
from ..forecast import features_and_labels, fit_predict
from ..validation_engine import infer
__all__ = ["features_and_labels", "fit_predict", "infer"]
