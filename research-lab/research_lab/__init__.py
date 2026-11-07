"""Isolated scalping research laboratory. Does not change production trading."""

from __future__ import annotations

__all__ = ["PACKAGE_ROOT", "LAB_ROOT", "PROJECT_ROOT"]

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
LAB_ROOT = PACKAGE_ROOT.parent
PROJECT_ROOT = LAB_ROOT.parent
