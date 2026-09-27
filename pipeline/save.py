"""SAVE: publish date partitions atomically so reruns replace, never append or half-write.

Layout (medallion tiers):
  data/bronze/run_date=D/  raw copies of every source + raw API pages + manifest.json
  data/silver/run_date=D/  order_journey.csv (validated, order grain) + cleaning_log.json
  data/gold/run_date=D/    metrics.json, evidence_table.md, late_definition_sensitivity.csv, validation_report.json
  data/failed/run_date=D/  validation_report.json when the gate stops a run (gold is left untouched)
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path


def staging_dir(tier_dir: Path, run_date: str) -> Path:
    path = tier_dir / f".staging_run_date={run_date}"
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def publish(staging: Path, tier_dir: Path, run_date: str, log) -> Path:
    """Swap a fully written staging folder into place. A crash before this line leaves the old partition intact."""
    final = tier_dir / f"run_date={run_date}"
    backup = tier_dir / f".previous_run_date={run_date}"
    if backup.exists():
        shutil.rmtree(backup)
    if final.exists():
        final.rename(backup)
    staging.rename(final)
    if backup.exists():
        shutil.rmtree(backup)
    log.info("Published %s (%s files)", final.relative_to(tier_dir.parent.parent), sum(1 for _ in final.rglob("*") if _.is_file()))
    return final


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str))


def write_csv(path: Path, df) -> None:
    df.to_csv(path, index=False, date_format="%Y-%m-%dT%H:%M:%S")
