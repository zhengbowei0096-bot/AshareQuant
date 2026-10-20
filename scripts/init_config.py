"""Create local configuration without overwriting existing settings."""
from pathlib import Path
import argparse
import shutil

def initialize(root):
    root = Path(root)
    created = []
    for source in sorted((root / "config").glob("*.example.*")):
        target = root / source.name.replace(".example", "")
        if not target.exists():
            shutil.copy2(source, target)
            created.append(target.name)
    return created

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    print(initialize(parser.parse_args().root))
