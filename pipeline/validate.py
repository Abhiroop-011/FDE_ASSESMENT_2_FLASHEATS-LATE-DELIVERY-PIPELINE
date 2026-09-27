"""VALIDATE: business-oriented rules with explicit severity.

ERROR -> unsafe to publish; the run stops before transform/save and the report goes to data/failed/.
WARN  -> published, but the issue and how we handled it are in validation_report.json.
INFO  -> noted for the client; no impact on the KPI.

Rules never modify data. Each result says what the pipeline does about the issue (`handling`),
so nothing is fixed silently.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Dict, List

import pandas as pd


class ValidationGateError(RuntimeError):
    def __init__(self, results):
        self.results = results
        failed = [r.rule_id for r in results if r.status == "ERROR"]
        super().__init__(f"Validation gate failed: {failed}")


@dataclass
class CheckResult:
    rule_id: str
    category: str
    rule: str
    status: str  # PASS | INFO | WARN | ERROR
    affected: int
    detail: str
    handling: str = ""
    sample_ids: List[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, format="mixed", errors="coerce")


def norm(series: pd.Series) -> pd.Series:
    return series.astype("string").str.strip().str.lower()


class Validator:
    def __init__(self):
        self.results: List[CheckResult] = []

    def check(self, rule_id, category, rule, violated_mask_or_count, fail_status, detail="", handling="", ids=None):
        if isinstance(violated_mask_or_count, pd.Series):
            affected = int(violated_mask_or_count.sum())
        else:
            affected = int(violated_mask_or_count)
        status = fail_status if affected else "PASS"
        sample = [] if ids is None or not affected else [str(i) for i in list(ids)[:10]]
        self.results.append(CheckResult(rule_id, category, rule, status, affected, detail, handling if affected else "", sample))
        return affected


def _missing_columns(df, required):
    return sorted(set(required) - set(df.columns))


def validate_sources(sources: Dict[str, "ExtractResult"], cfg, run_date: date) -> List[CheckResult]:  # noqa: F821
    v = Validator()
    vcfg = cfg.validation
    orders = sources["orders"].df
    keys = sources["order_keys"].df
    dispatch_ok = sources["dispatch"].status == "OK"

    # ------------------------------------------------------------------ structural
    for name, required in vcfg["required_columns"].items():
        if name == "dispatch" and not dispatch_ok:
            v.check("S01", "structural", "Dispatch API reachable", 1, "WARN",
                    "Dispatch API unavailable for this run",
                    "Degraded run: dispatch columns null; created->assigned stage metric reported as unavailable")
            continue
        missing = _missing_columns(sources[name].df, required)
        v.check("S01", "structural", f"{name} has all required columns", len(missing), "ERROR",
                f"missing={missing}" if missing else f"{len(required)} required columns present",
                "Stop: downstream joins/metrics would silently break")

    all_created = ts(keys["created_at"])
    latest_db = all_created.max()
    v.check("S02", "structural", "KPI window contains orders", int(len(orders) == 0), "ERROR",
            f"window orders={len(orders)}; latest order in DB={latest_db}",
            "Stop: an empty window would publish a meaningless KPI")

    if len(orders) and "created_at" in orders:
        latest = ts(orders["created_at"]).max()
        age = (pd.Timestamp(run_date) - latest.normalize()).days
    else:
        latest, age = latest_db, (pd.Timestamp(run_date) - latest_db.normalize()).days
    v.check("S03", "structural", f"Freshness: newest order <= {cfg.run['max_data_age_days']} days before run_date",
            int(age > cfg.run["max_data_age_days"]), "ERROR", f"newest order={latest}; age_days={age}",
            "Stop: upstream extract is stale; do not overwrite the last good KPI")

    if missing_any := _missing_columns(orders, vcfg["required_columns"]["orders"]):
        return v.results  # remaining order rules need these columns; the ERROR above already stops the run

    dup = orders["order_id"].duplicated(keep=False)
    conflicting = []
    if dup.any():
        grouped = orders[dup].groupby("order_id").nunique(dropna=False)
        conflicting = sorted(grouped.columns[(grouped > 1).any()].tolist())
    v.check("S04", "structural", "order_id is unique (grain = one row per order)", orders["order_id"].duplicated(), "WARN",
            f"duplicate extra rows={int(orders['order_id'].duplicated().sum())}; columns that disagree between copies={conflicting}",
            "Keep first occurrence (agreed rule). Timestamps identical, so KPI unaffected; traffic_bucket for these orders is ambiguous",
            orders.loc[dup, "order_id"].unique())

    for col, severity in (("final_status", "ERROR"), ("traffic_bucket", "WARN"), ("weather_bucket", "WARN")):
        allowed = set(vcfg["allowed_values"][col])
        raw_variants = orders[col].dropna()[~orders[col].dropna().isin(allowed)]
        outside = ~norm(orders[col]).isin(allowed)
        v.check("S05", "structural", f"{col} in {sorted(allowed)} after case/space normalisation", outside, severity,
                f"unknown values={sorted(orders.loc[outside, col].dropna().unique().tolist())}",
                "Stop: KPI denominator depends on status" if severity == "ERROR" else "Bucket excluded from driver analysis")
        v.check("S06", "structural", f"{col} uses canonical spelling", len(raw_variants), "INFO",
                f"variants={raw_variants.value_counts().to_dict()}", "Normalised to lower-case in clean step")

    lo, hi = vcfg["distance_km_range"]
    dist = pd.to_numeric(orders["distance_km_estimate"], errors="coerce")
    bad_dist = dist.isna() | ~dist.between(lo, hi)
    v.check("S07", "structural", f"distance_km_estimate within [{lo}, {hi}] km", bad_dist, "WARN",
            f"min={dist.min()} max={dist.max()}", "Kept; flagged for stakeholder review (outliers are not auto-deleted)",
            orders.loc[bad_dist, "order_id"])

    for col in ("driver_id", "restaurant_id"):
        v.check("S08", "structural", f"orders.{col} is populated", orders[col].isna(), "WARN",
                f"null {col}={int(orders[col].isna().sum())}", "Kept in KPI; excluded from per-driver/restaurant breakdowns",
                orders.loc[orders[col].isna(), "order_id"])

    # ------------------------------------------------------------------ lifecycle
    status = norm(orders["final_status"])
    created, promised = ts(orders["created_at"]), ts(orders["promised_eta"])
    pickup, delivered_at = ts(orders["pickup_at"]), ts(orders["actual_delivery_at"])
    delivered = status.eq("delivered")

    for col, parsed in (("created_at", created), ("promised_eta", promised), ("pickup_at", pickup)):
        v.check("L01", "lifecycle", f"{col} parses as a timestamp", parsed.isna() & orders[col].notna(), "ERROR",
                "mixed precision (seconds vs microseconds) handled with format='mixed'", "Stop: unparseable lifecycle time")

    unmeasurable = delivered & delivered_at.isna()
    share = unmeasurable.sum() / max(int(delivered.sum()), 1)
    over = share > vcfg["max_unmeasurable_share"]
    v.check("L02", "lifecycle", "Delivered orders have actual_delivery_at", unmeasurable, "ERROR" if over else "WARN",
            f"{int(unmeasurable.sum())} of {int(delivered.sum())} delivered ({share:.1%}); limit {vcfg['max_unmeasurable_share']:.0%}",
            "Excluded from KPI denominator and reported as 'unmeasurable'; NOT back-filled (driver-app recovery shown as sensitivity)",
            orders.loc[unmeasurable, "order_id"])

    bad = delivered_at < pickup
    v.check("L03", "lifecycle", "Delivery happens after pickup", bad, "WARN", "",
            "Kept in KPI (delay uses promised vs delivered only); excluded from pickup->delivered stage metric",
            orders.loc[bad, "order_id"])

    bad = promised < created
    v.check("L04", "lifecycle", "Promised ETA is after order creation", bad, "WARN", "",
            "Kept in KPI (max impact 0.3 pp) and listed as assumption; needs order-platform owner to confirm",
            orders.loc[bad, "order_id"])

    cancelled = status.eq("cancelled")
    v.check("L05", "lifecycle", "Cancelled orders have no pickup timestamp", cancelled & pickup.notna(), "INFO",
            "pickup_at is populated for cancelled orders, so it is not evidence of a pickup",
            "Cancelled orders excluded from all stage metrics")

    if dispatch_ok:
        d = sources["dispatch"].df.set_index("order_id")
        assigned = ts(orders["order_id"].map(d["assigned_at"]))
        bad = pickup < assigned
        v.check("L06", "lifecycle", "Pickup happens after dispatch assignment", bad & delivered, "WARN",
                "order platform pickup_at vs dispatch assigned_at",
                "Excluded from assigned->pickup stage metric", orders.loc[bad & delivered, "order_id"])

    # ------------------------------------------------------------------ cross-source
    all_ids = set(keys["order_id"])
    dims = {"restaurant_id": sources["restaurants"].df["restaurant_id"], "driver_id": sources["drivers"].df["driver_id"],
            "customer_id": sources["customers"].df["customer_id"]}
    for col, parent in dims.items():
        vals = orders[col].dropna()
        orphans = ~vals.isin(set(parent))
        v.check("X01", "cross-source", f"orders.{col} exists in {col.split('_')[0]}s table", orphans, "WARN",
                f"coverage={1 - orphans.mean():.1%}", "Kept; dimension attributes null")

    children = ["interventions", "support_tickets", "restaurant_status", "app_actions", "driver_events", "order_outcomes"]
    if dispatch_ok:
        children.append("dispatch")
    for name in children:
        oid = sources[name].df["order_id"]
        null_ids = oid.isna()
        orphans = ~oid.dropna().isin(all_ids)
        coverage = 1 - orphans.mean() if len(orphans) else 1.0
        v.check("X02", "cross-source", f"{name}.order_id links to an order", orphans,
                "ERROR" if coverage < vcfg["min_fk_coverage"] else "WARN", f"coverage={coverage:.1%}",
                "Orphans dropped from joins and counted here", oid.dropna()[orphans])
        if null_ids.any():
            v.check("X03", "cross-source", f"{name}.order_id is populated", null_ids, "WARN",
                    f"rows without order_id={int(null_ids.sum())}", "Cannot be linked to an order; excluded from journey")

    for name, key in (("support_tickets", "ticket_id"), ("restaurant_status", None), ("interventions", "intervention_id")):
        df = sources[name].df
        dups = df.duplicated() if key is None else df.duplicated(key)
        v.check("X04", "cross-source", f"{name} has no duplicate {'rows' if key is None else key}", dups, "WARN",
                "", "Exact duplicates removed in clean step")

    if dispatch_ok:
        d = sources["dispatch"].df.set_index("order_id")
        disp_status = norm(orders["order_id"].map(d["dispatch_status"]))
        disagree = (status.eq("cancelled") != disp_status.eq("cancelled"))
        v.check("X05", "cross-source", "Dispatch and order DB agree on cancellation", disagree, "WARN",
                "", "Order DB is system of record for final_status", orders.loc[disagree, "order_id"])

    de = sources["driver_events"].df
    de_t = ts(de["timestamp"])
    app_deliv = de_t[de["type"].eq("delivered")].groupby(de.loc[de["type"].eq("delivered"), "order_id"]).min()
    app_pick = de_t[de["type"].eq("picked_up")].groupby(de.loc[de["type"].eq("picked_up"), "order_id"]).min()
    both = delivered_at.notna() & orders["order_id"].isin(app_deliv.index)
    diff = (delivered_at - orders["order_id"].map(app_deliv)).dt.total_seconds().abs()
    v.check("X06", "cross-source", "Driver app and order DB agree on delivery time (where both exist)",
            both & (diff > 1), "WARN", f"compared={int(both.sum())}", "Order DB kept as system of record")
    recoverable = unmeasurable & orders["order_id"].isin(app_deliv.index)
    v.check("X07", "cross-source", "Unmeasurable deliveries have no alternative evidence", recoverable, "INFO",
            "driver app has a delivered event for these orders",
            "Not back-filled into KPI; reported as 'recovered' sensitivity so the client can decide")
    pdiff = (pickup - orders["order_id"].map(app_pick)).dt.total_seconds().abs()
    tol = vcfg["pickup_tolerance_seconds"]
    v.check("X08", "cross-source", f"Driver app and order DB pickup times agree within {tol}s", delivered & (pdiff > tol), "WARN",
            f"max diff={pdiff.max():.0f}s", "Order DB pickup_at used; disagreement recorded as instrumentation issue",
            orders.loc[delivered & (pdiff > tol), "order_id"])

    ev = sources["order_events"].df
    ev_orders = set(ev.loc[ev["source_system"].eq("interventions"), "order_id"])
    iv_orders = set(sources["interventions"].df["order_id"])
    v.check("X09", "cross-source", "Intervention log and order-event stream describe the same interventions",
            len(ev_orders ^ iv_orders), "INFO",
            f"order_interventions.csv orders={len(iv_orders)}; order_events intervention orders={len(ev_orders)}; overlap={len(ev_orders & iv_orders)}",
            "order_interventions.csv used as system of record (has initiator + reason); discrepancy raised with Ops")

    return v.results


def validate_journey(journey: pd.DataFrame, window_order_ids: pd.Series, outcomes: pd.DataFrame) -> List[CheckResult]:
    """Output checks: the modelled table must keep the grain and reproduce the client's own outcome labels."""
    v = Validator()
    expected = window_order_ids.nunique()
    v.check("O01", "output", "order_journey has exactly one row per window order", int(len(journey) != expected
            or journey["order_id"].duplicated().any()), "ERROR", f"rows={len(journey)} expected={expected}",
            "Stop: a join fanned out or dropped orders")

    m = journey[journey["is_measurable"]].merge(outcomes[["order_id", "late_flag"]], on="order_id", how="left")
    ref = pd.to_numeric(m["late_flag"], errors="coerce")
    compared = ref.notna()
    mismatch = compared & (m["is_late"].astype(int) != ref)
    v.check("O02", "output", "Pipeline late flag reproduces order_outcomes.csv late_flag", mismatch, "WARN",
            f"compared={int(compared.sum())} mismatches={int(mismatch.sum())}",
            "Investigate definition drift before publishing externally", m.loc[mismatch, "order_id"])

    v.check("O03", "output", "KPI denominator is non-zero", int(journey["is_measurable"].sum() == 0), "ERROR",
            f"measurable deliveries={int(journey['is_measurable'].sum())}", "Stop: no measurable deliveries")
    return v.results


def gate(results: List[CheckResult]) -> None:
    if any(r.status == "ERROR" for r in results):
        raise ValidationGateError(results)


def summarise(results: List[CheckResult]) -> Dict[str, int]:
    counts = {"PASS": 0, "INFO": 0, "WARN": 0, "ERROR": 0}
    for r in results:
        counts[r.status] += 1
    return counts
