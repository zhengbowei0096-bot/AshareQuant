"""Compatibility import; implementation lives in evaluation.connection_health."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module(".evaluation.connection_health", __package__)
