"""Compatibility import; implementation lives in backtesting.research."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module(".backtesting.research", __package__)
