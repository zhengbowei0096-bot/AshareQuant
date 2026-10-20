"""Compatibility import; implementation lives in data_sources.database."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module(".data_sources.database", __package__)
