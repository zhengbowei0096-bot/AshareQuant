"""Update all enabled stocks, validate data and generate the research dashboard."""
from asharequant.cli import main

if __name__ == '__main__':
    raise SystemExit(main(['run', *__import__('sys').argv[1:]]))
