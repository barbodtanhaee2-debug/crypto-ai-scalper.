"""Minimal standard-library logging setup."""

import logging

from config import LOG_LEVEL


def configure_logging() -> None:
    """Configure consistent console logging for the application."""
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
