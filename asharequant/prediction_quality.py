"""Compatibility import; implementation lives in evaluation.prediction_quality."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module(".evaluation.prediction_quality", __package__)
