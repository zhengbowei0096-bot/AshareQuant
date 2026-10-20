"""Compatibility import; implementation lives in reporting.report."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module(".reporting.report", __package__)
