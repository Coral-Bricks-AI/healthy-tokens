"""Module entry point for `python ./prober` and `python -m prober`."""

from pathlib import Path
import sys

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from prober.cli import main

if __name__ == "__main__":
    sys.exit(main())
