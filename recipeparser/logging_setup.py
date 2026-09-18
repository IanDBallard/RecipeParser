"""One console handler for the package's own loggers, unless someone got there first.

uvicorn's ``--log-level`` configures only uvicorn's loggers and leaves the root
logger bare, so under the API every ``logger.info`` in the package went nowhere
and the pipeline's "Job … completed" line never reached the container log. The
API calls :func:`configure_logging` once when its module loads.

The setup is deliberately deferential: a root logger that already has a handler
(pytest's capture, a host application's own configuration, a second call) is
left exactly as found, level included.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

_FORMAT = "%(levelname)s %(name)s: %(message)s"


def configure_logging(logger: Optional[logging.Logger] = None) -> None:
    """Install one stderr handler on a bare logger (the root by default), at ``LOG_LEVEL`` (default INFO)."""
    root = logger if logger is not None else logging.getLogger()
    if root.handlers:
        return
    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        level = logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)
