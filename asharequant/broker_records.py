"""Compatibility import; implementation lives in data_sources.broker_records."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module(".data_sources.broker_records", __package__)
