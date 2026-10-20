"""Compatibility import; implementation lives in reporting.report_navigation."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module(".reporting.report_navigation", __package__)
