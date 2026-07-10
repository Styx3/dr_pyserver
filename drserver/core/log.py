"""Logging shim replacing UnityEngine.Debug.

The C# server used Debug.LogError as its primary diagnostic channel (~2970 calls),
not only for errors. To preserve that behaviour without flooding ERROR level, the
ported code should call ``log.debug/info/warn/error``. A thin ``Debug`` facade is
provided so mechanically-translated call sites (Debug.Log / LogError / LogWarning)
keep working; LogError is mapped to INFO by default since it was used as a trace
channel, and can be promoted via the ``DR_LOGERROR_LEVEL`` env var.
"""
from __future__ import annotations

import logging
import os
import sys

_LEVEL = os.environ.get("DR_LOG_LEVEL", "INFO").upper()
# C# Debug.LogError was a trace channel, not a real error — map it to INFO by default.
_LOGERROR_LEVEL = getattr(logging, os.environ.get("DR_LOGERROR_LEVEL", "INFO").upper(), logging.INFO)

logging.basicConfig(
    level=getattr(logging, _LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)

_logger = logging.getLogger("dr")


def debug(msg: object) -> None:
    _logger.debug("%s", msg)


def info(msg: object) -> None:
    _logger.info("%s", msg)


def warn(msg: object) -> None:
    _logger.warning("%s", msg)


def error(msg: object) -> None:
    _logger.error("%s", msg)


class Debug:
    """Facade matching the C# UnityEngine.Debug surface used by ported code."""

    @staticmethod
    def Log(msg: object) -> None:
        _logger.info("%s", msg)

    @staticmethod
    def LogWarning(msg: object) -> None:
        _logger.warning("%s", msg)

    @staticmethod
    def LogError(msg: object) -> None:
        _logger.log(_LOGERROR_LEVEL, "%s", msg)
