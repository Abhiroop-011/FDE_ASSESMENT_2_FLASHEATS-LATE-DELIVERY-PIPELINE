#!/usr/bin/env python3
"""FlashEats late-delivery KPI pipeline: EXTRACT -> VALIDATE -> CLEAN -> TRANSFORM -> VALIDATE OUTPUT -> SAVE.

    python run_pipeline.py --run-date 2026-08-29
    python run_pipeline.py --run-date 2026-08-29 --simulate api_down        # degraded run (exit 0, WARN)
    python run_pipeline.py --run-date 2026-08-29 --simulate missing_column  # validation gate (exit 2)
    python run_pipeline.py --run-date 2026-08-29 --simulate db_down         # anchor source down (exit 1)
    python run_pipeline.py --run-date 2026-09-27                            # stale data (exit 2)

Exit codes: 0 = published (possibly with warnings) · 1 = extract/unexpected failure · 2 = validation gate stopped the run.
"""
from __future__ import annotations

import argparse
import contextlib
import shutil
import sys
import time
import warnings
from datetime import date, datetime, timedelta

# macOS system Python ships LibreSSL; urllib3 warns about it on import. Irrelevant for a localhost API.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

from pipeline import save  # noqa: E402
from pipeline.clean import clean_sources
from pipeline.config import load_config
from pipeline.extract import ExtractError, extract_all
from pipeline.logging_utils import setup_logger, stage_logger
from pipeline.metrics import build_metrics, late_definition_sensitivity
from pipeline.mock_api import mock_dispatch_api
from pipeline.report import evidence_table
from pipeline.transform import build_order_journey
from pipeline.validate import ValidationGateError, gate, summarise, validate_journey, validate_sources

EXIT_OK, EXIT_FAILURE, EXIT_VALIDATION = 0, 1, 2
UNREACHABLE_API = "http://127.0.0.1:9"  # nothing listens here; used to simulate a dispatch outage


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-date", required=True, type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                   help="Logical run date (YYYY-MM-DD). KPI window = [run_date - window_days, run_date).")
    p.add_argument("--window-days", type=int, help="Override config run.window_days")
    p.add_argument("--simulate", choices=["api_down", "missing_column", "db_down"], help="Inject a controlled failure")
    return p.parse_args(argv)


def report_payload(run_date, status, checks):
    return {"run_date": run_date, "status": status, "summary": summarise(checks), "checks": [c.to_dict() for c in checks]}


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = load_config()
    run_date: date = args.run_date
    rd = run_date.isoformat()
    window_days = args.window_days or cfg.run["window_days"]
    window = {"created_at_from": (run_date - timedelta(days=window_days)).isoformat(), "created_at_to_exclusive": rd,
              "window_days": window_days}

    logger = setup_logger(cfg.path("log_dir"), rd)
    log = lambda stage: stage_logger(logger, stage)  # noqa: E731
    started = time.time()
    log("RUN").info("===== Pipeline start | run_date=%s window=%s..%s simulate=%s =====",
                    rd, window["created_at_from"], rd, args.simulate or "none")

    if args.simulate == "db_down":
        # The order DB is the anchor: without it there is no KPI, so the run must fail rather than degrade.
        cfg.raw["paths"]["database"] = "source_data/database/_unreachable.db"

    checks = []
    try:
        # ------------------------------------------------------------ EXTRACT (bronze)
        bronze = save.staging_dir(cfg.path("bronze_dir"), rd)
        use_mock = cfg.api["start_mock_api"] and args.simulate != "api_down"
        api_ctx = mock_dispatch_api(cfg.root, log("EXTRACT")) if use_mock else contextlib.nullcontext(
            UNREACHABLE_API if args.simulate == "api_down" else cfg.api["url"])
        with api_ctx as api_url:
            sources = extract_all(cfg, window["created_at_from"], rd, api_url, bronze, log("EXTRACT"))
        # Raw inputs are preserved even if validation fails later — that is what the on-call engineer debugs from.
        save.publish(bronze, cfg.path("bronze_dir"), rd, log("EXTRACT"))

        if args.simulate == "missing_column":
            log("EXTRACT").warning("SIMULATION: upstream order DB dropped column promised_eta")
            sources["orders"].df = sources["orders"].df.drop(columns=["promised_eta"])

        # ------------------------------------------------------------ VALIDATE (gate 1: sources)
        checks = validate_sources(sources, cfg, run_date)
        for c in checks:
            level = {"PASS": "info", "INFO": "info", "WARN": "warning", "ERROR": "error"}[c.status]
            getattr(log("VALIDATE"), level)("%-5s %s %s | affected=%s %s", c.status, c.rule_id, c.rule, c.affected, c.detail)
        gate(checks)

        # ------------------------------------------------------------ CLEAN + TRANSFORM (silver)
        frames, cleaning_log = clean_sources(sources, log("CLEAN"))
        dispatch_available = sources["dispatch"].status == "OK"
        journey = build_order_journey(frames, cfg, dispatch_available, log("TRANSFORM"))

        # ------------------------------------------------------------ VALIDATE (gate 2: modelled output)
        output_checks = validate_journey(journey, frames["orders"]["order_id"], frames["order_outcomes"])
        for c in output_checks:
            getattr(log("VALIDATE"), "error" if c.status == "ERROR" else "info")(
                "%-5s %s %s | %s", c.status, c.rule_id, c.rule, c.detail)
        checks += output_checks
        gate(checks)

        # ------------------------------------------------------------ METRICS + SAVE (gold)
        metrics = build_metrics(journey, cfg, rd, window)
        sensitivity = late_definition_sensitivity(journey, cfg)
        status = "PASSED_WITH_WARNINGS" if any(c.status == "WARN" for c in checks) else "PASSED"
        report = report_payload(rd, status, checks)
        metrics["validation_status"] = status

        silver = save.staging_dir(cfg.path("silver_dir"), rd)
        save.write_csv(silver / "order_journey.csv", journey)
        save.write_json(silver / "cleaning_log.json", cleaning_log)

        gold = save.staging_dir(cfg.path("gold_dir"), rd)
        save.write_json(gold / "metrics.json", metrics)
        save.write_json(gold / "validation_report.json", report)
        save.write_csv(gold / "late_definition_sensitivity.csv", sensitivity)
        (gold / "evidence_table.md").write_text(
            evidence_table(metrics, sensitivity, report["summary"], report["checks"]),
            encoding="utf-8"
        )

        save.publish(silver, cfg.path("silver_dir"), rd, log("SAVE"))
        save.publish(gold, cfg.path("gold_dir"), rd, log("SAVE"))
        stale_failure = cfg.path("failed_dir") / f"run_date={rd}"
        if stale_failure.exists():
            shutil.rmtree(stale_failure)
            log("SAVE").info("Removed superseded failure report for run_date=%s", rd)

        m1 = metrics["metrics"]["M1_late_delivery_rate"]
        log("RUN").info("KPI late_delivery_rate=%s%% (%s/%s) | status=%s | %.1fs",
                        m1["value_pct"], m1["numerator_late_orders"], m1["denominator_measurable_deliveries"],
                        status, time.time() - started)
        if not dispatch_available:
            log("RUN").warning("DEGRADED run: dispatch-dependent metrics are n/a. Rerun this run_date when the "
                               "Dispatch API recovers; the rerun replaces this partition.")
        log("RUN").info("===== Pipeline finished: PUBLISHED =====")
        return EXIT_OK

    except ValidationGateError as exc:
        failed = save.staging_dir(cfg.path("failed_dir"), rd)
        save.write_json(failed / "validation_report.json", report_payload(rd, "FAILED", exc.results))
        save.publish(failed, cfg.path("failed_dir"), rd, log("VALIDATE"))
        for c in exc.results:
            if c.status == "ERROR":
                log("VALIDATE").error("BLOCKING %s %s | %s | action: %s", c.rule_id, c.rule, c.detail, c.handling)
        log("RUN").error("===== Pipeline STOPPED by validation gate: nothing published to silver/gold "
                         "(last good output left untouched) =====")
        return EXIT_VALIDATION
    except ExtractError as exc:
        log("EXTRACT").error("Extraction failed: %s", exc)
        log("RUN").error("===== Pipeline FAILED during extract: nothing published =====")
        return EXIT_FAILURE
    except Exception:
        logger.exception("Unexpected failure", extra={"stage": "RUN"})
        log("RUN").error("===== Pipeline FAILED (unexpected): nothing published =====")
        return EXIT_FAILURE
    finally:
        for tier in ("bronze_dir", "silver_dir", "gold_dir", "failed_dir"):
            leftover = cfg.path(tier) / f".staging_run_date={rd}"
            if leftover.exists():
                shutil.rmtree(leftover)


if __name__ == "__main__":
    sys.exit(main())
