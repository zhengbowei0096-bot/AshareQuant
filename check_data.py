from asharequant.cli import main

if __name__ == '__main__':
    raise SystemExit(main(['check', *__import__('sys').argv[1:]]))
