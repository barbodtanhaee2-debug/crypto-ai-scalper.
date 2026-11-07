"""Stage 3.2.1 H1 CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parent
PROJECT = LAB.parent
for path in (LAB, PROJECT):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from research_lab.experiments.h1_runner import run_h1, write_markdown_report


def main() -> None:
    payload = run_h1()
    report = write_markdown_report(payload)
    print(json.dumps(payload["classification"], indent=2))
    print(f"report={report}")


if __name__ == "__main__":
    main()
