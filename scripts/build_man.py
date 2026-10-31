"""Render the sylve-mcp(1) manual page from the roff template.

The templated file uses @VERSION@ placeholders; this script substitutes the
installed package version and verifies the committed man page is fresh.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "src"))
from sylve_mcp import __version__  # noqa: E402

SRC = ROOT / "docs" / "man" / "src" / "sylve-mcp.1.in"
DEST = ROOT / "docs" / "man" / "man1" / "sylve-mcp.1"


def render() -> str:
    text = SRC.read_text(encoding="utf-8")
    return text.replace("@VERSION@", __version__)


def main() -> int:
    output = render()
    if "--check" in sys.argv:
        if DEST.exists() and DEST.read_text(encoding="utf-8") == output:
            print("man page is up to date")
            return 0
        print(f"ERROR: {DEST} is stale; run 'python scripts/build_man.py'", file=sys.stderr)
        return 1
    DEST.write_text(output, encoding="utf-8")
    print(f"wrote {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())