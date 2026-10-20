"""Compatibility entry point: batch update replaces the incomplete old script."""
from asharequant.cli import main

if __name__ == '__main__':
    raise SystemExit(main(['run', *__import__('sys').argv[1:]]))
