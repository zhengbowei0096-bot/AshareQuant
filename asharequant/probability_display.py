"""Compatibility import; implementation lives in reporting.probability_display."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module(".reporting.probability_display", __package__)
