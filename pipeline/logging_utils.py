from __future__ import annotations

import logging
import sys
from pathlib import Path

FORMAT = "%(asctime)s | %(levelname)-7s | %(stage)-9s | %(message)s"


class _StageFilter(logging.Filter):
    """Guarantees every record has a `stage` so the on-call engineer can see which step failed."""

    def filter(self, record):
        if not hasattr(record, "stage"):
            record.stage = "-"
        return True


def setup_logger(log_dir: Path, run_date: str, level: str = "INFO") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("flasheats_pipeline")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    # Log file is appended per run_date so reruns of the same date keep their history.
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(log_dir / f"pipeline_run_date={run_date}.log")):
        handler.setFormatter(formatter)
        handler.addFilter(_StageFilter())
        logger.addHandler(handler)
    return logger


def stage_logger(logger: logging.Logger, stage: str) -> logging.LoggerAdapter:
    return logging.LoggerAdapter(logger, {"stage": stage})
