"""Loguru configuration."""

from __future__ import annotations

import sys

from loguru import logger


def configure_logging(level: str = "INFO") -> None:
    """Reset the default loguru sink and install a single stderr sink."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
            "<level>{level: <8}</level> "
            "<cyan>{name}:{function}:{line}</cyan> - <level>{message}</level>"
        ),
        backtrace=True,
        diagnose=False,   # don't leak local vars into logs
    )
