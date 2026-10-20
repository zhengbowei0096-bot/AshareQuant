"""Compatibility import; implementation lives in evaluation.validation_readiness."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module(".evaluation.validation_readiness", __package__)
