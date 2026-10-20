"""Compatibility import; implementation lives in reporting.daily_bundle."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module(".reporting.daily_bundle", __package__)
