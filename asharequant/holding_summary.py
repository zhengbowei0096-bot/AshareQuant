"""Compatibility import; implementation lives in reporting.holding_summary."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module(".reporting.holding_summary", __package__)
