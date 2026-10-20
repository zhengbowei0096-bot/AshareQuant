"""Compatibility import; implementation lives in evaluation.factor_research."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module(".evaluation.factor_research", __package__)
