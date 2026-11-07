import sys
from pathlib import Path

_LAB = Path(__file__).resolve().parents[1]
_PROJECT = _LAB.parent
for path in (_LAB, _PROJECT):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)
