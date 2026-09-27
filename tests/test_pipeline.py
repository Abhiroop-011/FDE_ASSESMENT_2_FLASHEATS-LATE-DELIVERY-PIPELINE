"""Dependability tests: completeness, validation gate, retry semantics, grain, idempotent publish.

Run: python -m pytest -q
"""
import logging
from datetime import date

import pytest

from pipeline import save
from pipeline.clean import clean_sources
from pipeline.config import load_config
from pipeline.extract import ExtractError, NonRetryableAPIError, _request_page, extract_all, extract_sql
from pipeline.metrics import build_metrics
from pipeline.transform import build_order_journey
from pipeline.validate import ValidationGateError, gate, validate_journey, validate_sources

LOG = logging.getLogger("test")
RUN_DATE = date(2026, 8, 29)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture()
def sources(cfg, tmp_path):
    # api_url=None -> dispatch degraded; keeps tests fast and independent of the mock server.
    return extract_all(cfg, "2026-08-01", "2026-08-29", None, tmp_path, LOG)


def _by_id(results, rule_id):
    return [r for r in results if r.rule_id == rule_id]


def test_every_local_source_is_complete(sources):
    for name, r in sources.items():
        if name != "dispatch":
            assert r.complete, f"{name} incomplete: {r.rows} vs {r.expected_rows}"
    assert sources["orders"].rows == 1603


def test_sql_nulls_stay_null(sources):
    # Regression: read_sql(dtype=str) once turned NULL into the string "None", hiding 3 missing driver_ids.
    assert sources["orders"].df["driver_id"].isna().sum() == 3


def test_anchor_source_down_fails_hard(tmp_path):
    # Dispatch (enrichment) degrades; the order DB (anchor) must not.
    with pytest.raises(ExtractError):
        extract_sql(tmp_path / "missing.db", "2026-08-01", "2026-08-29", tmp_path, LOG)


def test_missing_required_column_stops_the_run(sources, cfg):
    sources["orders"].df = sources["orders"].df.drop(columns=["promised_eta"])
    results = validate_sources(sources, cfg, RUN_DATE)
    assert _by_id(results, "S01")[0].status == "ERROR"
    with pytest.raises(ValidationGateError):
        gate(results)


def test_stale_data_stops_the_run(sources, cfg):
    results = validate_sources(sources, cfg, date(2026, 9, 27))
    assert _by_id(results, "S03")[0].status == "ERROR"


def test_known_issues_warn_but_do_not_block(sources, cfg):
    results = validate_sources(sources, cfg, RUN_DATE)
    assert _by_id(results, "S04")[0].status == "WARN"   # duplicate orders
    assert _by_id(results, "L02")[0].affected == 37     # delivered without delivery time
    gate(results)  # no ERROR -> does not raise


def test_journey_keeps_order_grain_and_reconciles(sources, cfg):
    frames, _ = clean_sources(sources, LOG)
    journey = build_order_journey(frames, cfg, dispatch_available=False, log=LOG)
    checks = validate_journey(journey, frames["orders"]["order_id"], frames["order_outcomes"])
    assert all(c.status == "PASS" for c in checks), checks
    m1 = build_metrics(journey, cfg, "2026-08-29", {})["metrics"]["M1_late_delivery_rate"]
    assert (m1["numerator_late_orders"], m1["denominator_measurable_deliveries"]) == (843, 1495)


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers, self.text = status, body or {}, headers or {}, str(body)

    def json(self):
        return self._body


class _Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), 0

    def get(self, *a, **k):
        self.calls += 1
        return self.responses.pop(0)


API = {"max_retries": 3, "retry_base_seconds": 0, "page_size": 200, "timeout_seconds": 1}


def test_transient_errors_are_retried():
    ok = {"data": [], "has_more": False, "total_records": 0}
    session = _Session([_Resp(500), _Resp(429, {"retry_after_seconds": 0}), _Resp(200, ok)])
    assert _request_page(session, "http://x", 1, API, LOG) == ok
    assert session.calls == 3


def test_permanent_errors_are_not_retried():
    session = _Session([_Resp(404, {"error": "not found"})])
    with pytest.raises(NonRetryableAPIError):
        _request_page(session, "http://x", 1, API, LOG)
    assert session.calls == 1


def test_publish_replaces_partition_instead_of_appending(tmp_path):
    tier = tmp_path / "gold"
    tier.mkdir()
    for content in ("first", "second"):
        staging = save.staging_dir(tier, "2026-08-29")
        (staging / f"{content}.txt").write_text(content)
        save.publish(staging, tier, "2026-08-29", LOG)
    assert sorted(p.name for p in (tier / "run_date=2026-08-29").iterdir()) == ["second.txt"]
    assert [p.name for p in tier.iterdir()] == ["run_date=2026-08-29"]  # no staging/backup leftovers
