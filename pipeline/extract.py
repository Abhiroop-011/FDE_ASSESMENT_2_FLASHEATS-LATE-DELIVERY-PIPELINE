"""EXTRACT: one extractor per retrieval mode (SQL, CSV files, nested JSON, paginated API).

Every extractor preserves the raw input in the bronze staging folder and proves completeness
(returned rows == rows the source says exist) before anything downstream sees the data.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests

from .config import PROJECT_ROOT


class ExtractError(RuntimeError):
    """Retrieval is incomplete or the anchor source is unreadable — never publish on top of this."""


class RetryableAPIError(RuntimeError):
    pass


class NonRetryableAPIError(RuntimeError):
    pass


@dataclass
class ExtractResult:
    name: str
    mode: str
    source: str
    df: pd.DataFrame = field(repr=False)
    rows: int
    expected_rows: Optional[int]
    status: str  # OK | UNAVAILABLE
    completeness_check: str
    sha256: Optional[str] = None

    @property
    def complete(self) -> bool:
        return self.status == "OK" and self.expected_rows == self.rows

    def manifest(self) -> dict:
        return {
            "mode": self.mode,
            "source": self.source,
            "rows": self.rows,
            "expected_rows": self.expected_rows,
            "complete": self.complete,
            "status": self.status,
            "completeness_check": self.completeness_check,
            "sha256": self.sha256,
        }


def _shown(path: Path) -> str:
    """Project-relative path for logs/errors, so logs never leak a local home directory."""
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return Path(path).name


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- SQL

def order_window_sql(window_start: str, window_end: str):
    """Parameterised query for the KPI window. ISO-8601 strings compare correctly as text in SQLite."""
    where = "WHERE created_at >= ? AND created_at < ?"
    return f"SELECT * FROM orders {where}", f"SELECT COUNT(*) FROM orders {where}", (window_start, window_end)


def extract_sql(db_path: Path, window_start: str, window_end: str, staging: Path, log) -> Dict[str, ExtractResult]:
    if not db_path.exists():
        raise ExtractError(f"Order DB (anchor source) not found: {_shown(db_path)}")

    select_sql, count_sql, params = order_window_sql(window_start, window_end)
    queries = {
        "orders": (select_sql, count_sql, params),
        # All order keys (not windowed) so FK checks on child sources and freshness see the full population.
        "order_keys": ("SELECT order_id, created_at FROM orders", "SELECT COUNT(*) FROM orders", ()),
        # Only the key is needed for FK checks; name/email stay in the client system (data minimisation).
        "customers": ("SELECT customer_id FROM customers", "SELECT COUNT(*) FROM customers", ()),
        "drivers": ("SELECT * FROM drivers", "SELECT COUNT(*) FROM drivers", ()),
        "restaurants": ("SELECT * FROM restaurants", "SELECT COUNT(*) FROM restaurants", ()),
    }

    results = {}
    with sqlite3.connect(db_path) as con:
        for name, (query, count_query, qparams) in queries.items():
            # No dtype=str here: it would turn SQL NULL into the string "None" and hide missing keys.
            df = pd.read_sql(query, con, params=qparams)
            expected = con.execute(count_query, qparams).fetchone()[0]
            df.to_csv(staging / f"sql_{name}.csv", index=False)
            result = ExtractResult(
                name=name, mode="SQL", source=f"{db_path.name}:{name}", df=df, rows=len(df),
                expected_rows=expected, status="OK", completeness_check="len(result) == SELECT COUNT(*) with same filter",
            )
            if not result.complete:
                raise ExtractError(f"SQL extract incomplete for {name}: got {len(df)}, COUNT(*)={expected}")
            log.info("SQL %-12s rows=%s COUNT(*)=%s complete=%s", name, len(df), expected, result.complete)
            results[name] = result
    return results


# --------------------------------------------------------------------------- files

def _csv_record_count(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as f:
        return sum(1 for _ in csv.reader(f)) - 1  # minus header


def extract_csv(path: Path, name: str, staging: Path, log) -> ExtractResult:
    if not path.exists():
        raise ExtractError(f"Source file missing: {_shown(path)}")
    shutil.copy2(path, staging / path.name)
    df = pd.read_csv(path, dtype=str)
    result = ExtractResult(
        name=name, mode="CSV", source=path.name, df=df, rows=len(df), expected_rows=_csv_record_count(path),
        status="OK", completeness_check="parsed rows == csv records in file", sha256=_sha256(path),
    )
    if not result.complete:
        raise ExtractError(f"CSV parse incomplete for {path.name}: parsed {result.rows}, file has {result.expected_rows}")
    log.info("CSV %-12s rows=%s complete=%s", name, result.rows, result.complete)
    return result


def extract_driver_events(path: Path, staging: Path, log) -> ExtractResult:
    """Flatten nested JSON: [{driver_id, events: [{order_id, type, timestamp, lat?, lon?}]}] -> one row per event."""
    if not path.exists():
        raise ExtractError(f"Source file missing: {_shown(path)}")
    shutil.copy2(path, staging / path.name)
    drivers = json.loads(path.read_text())
    expected = sum(len(d.get("events", [])) for d in drivers)
    rows = [{"driver_id": d["driver_id"], **event} for d in drivers for event in d.get("events", [])]
    df = pd.DataFrame(rows).astype({"order_id": str, "type": str, "timestamp": str})
    result = ExtractResult(
        name="driver_events", mode="JSON", source=path.name, df=df, rows=len(df), expected_rows=expected,
        status="OK", completeness_check=f"flattened rows == sum of nested events across {len(drivers)} drivers",
        sha256=_sha256(path),
    )
    if not result.complete:
        raise ExtractError(f"driver_events flatten lost rows: {result.rows} vs {expected}")
    log.info("JSON %-11s drivers=%s events=%s complete=%s", "driver_events", len(drivers), len(df), result.complete)
    return result


# --------------------------------------------------------------------------- API

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _request_page(session, url, page, api_cfg, log) -> dict:
    max_retries, base = api_cfg["max_retries"], api_cfg["retry_base_seconds"]
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(f"{url}/dispatch/orders", params={"page": page, "page_size": api_cfg["page_size"]},
                               timeout=api_cfg["timeout_seconds"])
        except requests.RequestException as exc:
            wait = base * 2 ** (attempt - 1)
            log.warning("Dispatch API network error | page=%s attempt=%s/%s wait=%.0fs error=%s",
                        page, attempt, max_retries, wait, type(exc).__name__)
            if attempt == max_retries:
                raise RetryableAPIError(f"page {page}: network error after {max_retries} attempts") from exc
            time.sleep(wait)
            continue

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code in RETRYABLE_STATUS:
            retry_after = resp.headers.get("Retry-After")
            if resp.status_code == 429 and retry_after is None:
                retry_after = (resp.json() or {}).get("retry_after_seconds")
            wait = float(retry_after) if retry_after is not None else base * 2 ** (attempt - 1)
            log.warning("Dispatch API retryable failure | page=%s status=%s attempt=%s/%s wait=%.0fs",
                        page, resp.status_code, attempt, max_retries, wait)
            if attempt == max_retries:
                raise RetryableAPIError(f"page {page}: status {resp.status_code} after {max_retries} attempts")
            time.sleep(wait)
            continue

        # 400/401/403/404 etc. will not fix themselves — retrying only hides the bug.
        raise NonRetryableAPIError(f"page {page}: non-retryable status {resp.status_code}: {resp.text[:200]}")
    raise RetryableAPIError(f"page {page}: exhausted retries")


def extract_dispatch_api(url: str, api_cfg: dict, required_columns: List[str], staging: Path, log) -> ExtractResult:
    raw_dir = staging / "dispatch_api"
    raw_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    records, page, expected = [], 1, None

    try:
        while True:
            payload = _request_page(session, url, page, api_cfg, log)
            (raw_dir / f"page_{page:03d}.json").write_text(json.dumps(payload, indent=1))  # raw before parsing
            expected = payload["total_records"] if expected is None else expected
            records.extend(payload["data"])
            log.info("Dispatch API page=%s rows=%s cumulative=%s/%s", page, len(payload["data"]), len(records), expected)
            if not payload.get("has_more"):
                break
            page += 1
    except RetryableAPIError as exc:
        if api_cfg.get("required"):
            raise ExtractError(f"Dispatch API unavailable and marked required: {exc}") from exc
        log.warning("Dispatch API UNAVAILABLE — degrading: dispatch columns will be null | %s", exc)
        return ExtractResult(
            name="dispatch", mode="API", source="dispatch_api:/dispatch/orders", df=pd.DataFrame(columns=required_columns),
            rows=0, expected_rows=None, status="UNAVAILABLE", completeness_check="source unreachable",
        )

    df = pd.DataFrame(records).astype(str).replace({"None": None})
    unique_ids = df["order_id"].nunique()
    result = ExtractResult(
        name="dispatch", mode="API", source="dispatch_api:/dispatch/orders", df=df, rows=len(df), expected_rows=expected,
        status="OK", completeness_check=f"sum(page rows) == total_records over {page} pages; unique order_id={unique_ids}",
    )
    if not result.complete or unique_ids != len(df):
        raise ExtractError(f"Dispatch retrieval incomplete: got {len(df)} rows ({unique_ids} unique), expected {expected}")
    log.info("API  dispatch     pages=%s rows=%s total_records=%s complete=%s", page, len(df), expected, result.complete)
    return result


# --------------------------------------------------------------------------- orchestration

FILE_SOURCES = {
    "interventions": "order_interventions.csv",
    "support_tickets": "support_tickets.csv",
    "restaurant_status": "restaurant_status.csv",
    "app_actions": "customer_app_actions.csv",
    "order_events": "order_events.csv",
    "order_outcomes": "order_outcomes.csv",
}


def extract_all(cfg, window_start: str, window_end: str, api_url: Optional[str], staging: Path, log) -> Dict[str, ExtractResult]:
    staging.mkdir(parents=True, exist_ok=True)
    results = extract_sql(cfg.path("database"), window_start, window_end, staging, log)

    files_dir = cfg.path("files_dir")
    for name, filename in FILE_SOURCES.items():
        results[name] = extract_csv(files_dir / filename, name, staging, log)
    results["driver_events"] = extract_driver_events(files_dir / "driver_events.json", staging, log)

    required = cfg.validation["required_columns"]["dispatch"]
    if api_url is None:
        log.warning("Dispatch API disabled for this run — degrading: dispatch columns will be null")
        results["dispatch"] = ExtractResult(
            name="dispatch", mode="API", source="disabled", df=pd.DataFrame(columns=required), rows=0,
            expected_rows=None, status="UNAVAILABLE", completeness_check="source unreachable",
        )
    else:
        results["dispatch"] = extract_dispatch_api(api_url, cfg.api, required, staging, log)

    manifest = {name: r.manifest() for name, r in results.items()}
    manifest["_window"] = {"created_at_from": window_start, "created_at_to_exclusive": window_end}
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return results
