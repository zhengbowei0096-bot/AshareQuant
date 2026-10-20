"""Compatibility import; implementation lives in risk.risk_control."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module(".risk.risk_control", __package__)
