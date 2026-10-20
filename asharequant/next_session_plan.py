"""Compatibility import; implementation lives in strategies.next_session_plan."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module(".strategies.next_session_plan", __package__)
