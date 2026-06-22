from __future__ import annotations

import re
import sys
from pathlib import Path


def fetch_version(pyproject_path: Path) -> str:
    text = pyproject_path.read_text()
    match = re.search(r'^version\s*=\s*"(\d+\.\d+\.\d+)"', text, re.MULTILINE)
    if not match:
        print("Could not find version in pyproject.toml", file=sys.stderr)
        sys.exit(1)
    return match.group(1)


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    print(fetch_version(root / "pyproject.toml"))


if __name__ == "__main__":
    main()
