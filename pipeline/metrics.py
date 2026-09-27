"""METRICS: 5 metrics tied to the project KPI "reduce late delivery rate", plus a data-trust panel.

Each metric carries its formula, grain and denominator so a stakeholder can recompute it.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

STAGES = {
    "created_to_assigned_min": "Order created -> driver assigned (dispatch)",
    "assigned_to_pickup_min": "Driver assigned -> food picked up (restaurant prep + driver arrival)",
    "pickup_to_delivered_min": "Picked up -> delivered (transit)",
}


def _pct(num, den) -> Optional[float]:
    return round(100 * float(num) / float(den), 1) if den else None


def _med(s: pd.Series) -> Optional[float]:
    s = s.dropna()
    return round(float(s.median()), 1) if len(s) else None


def late_definition_sensitivity(j: pd.DataFrame, cfg) -> pd.DataFrame:
    denominators = {
        "measurable_delivered": j["is_measurable"],
        "all_delivered": j["is_delivered"],
        "all_orders": pd.Series(True, index=j.index),
    }
    rows = []
    for key, d in cfg.late_definition["definitions"].items():
        den = denominators[d["denominator"]]
        late = (j["delay_min"] > d["threshold_min"]) & j["is_measurable"]
        rows.append({"definition": key, "label": d["label"], "threshold_min": d["threshold_min"],
                     "denominator_rule": d["denominator"], "late_orders": int((late & den).sum()),
                     "denominator": int(den.sum()), "late_rate_pct": _pct((late & den).sum(), den.sum()),
                     "canonical": key == cfg.late_definition["canonical"]})

    # What if the 37 unmeasurable deliveries were back-filled from the driver app? (reported, not applied)
    canon = cfg.late_definition["definitions"][cfg.late_definition["canonical"]]
    recovered_time = j["actual_delivery_at"].fillna(j["app_delivered_at"].where(j["dq_delivery_recoverable_from_driver_app"]))
    rec_delay = (recovered_time - j["promised_eta"]).dt.total_seconds() / 60
    rec_den = j["is_delivered"] & recovered_time.notna()
    rec_late = rec_den & (rec_delay > canon["threshold_min"])
    rows.append({"definition": "sensitivity_driver_app_backfill", "label": "Canonical, with missing delivery times back-filled from driver app",
                 "threshold_min": canon["threshold_min"], "denominator_rule": "delivered_with_any_delivery_evidence",
                 "late_orders": int(rec_late.sum()), "denominator": int(rec_den.sum()),
                 "late_rate_pct": _pct(rec_late.sum(), rec_den.sum()), "canonical": False})
    return pd.DataFrame(rows)


def build_metrics(j: pd.DataFrame, cfg, run_date: str, window: dict) -> dict:
    m = j[j["is_measurable"]]
    late, on_time = m[m["is_late"] == True], m[m["is_late"] == False]  # noqa: E712 (nullable boolean)
    canon_key = cfg.late_definition["canonical"]
    threshold = cfg.raw["metrics"]["pickup_wait_breach_min"]

    # M3: where does delay accumulate?
    stages = {}
    for col, label in STAGES.items():
        on, lt = _med(on_time[col]), _med(late[col])
        stages[col] = {"stage": label, "median_on_time_min": on, "median_late_min": lt,
                       "gap_min": round(lt - on, 1) if on is not None and lt is not None else None,
                       "orders_with_valid_stage": int(m[col].notna().sum())}
    gaps = {k: v["gap_min"] for k, v in stages.items()}
    # Naming a "largest" stage when some stages are unmeasured would point Ops at the wrong stage.
    worst = max(gaps, key=gaps.get) if all(g is not None for g in gaps.values()) else None

    # M4: pickup-wait breach as a leading indicator
    b = m[m["pickup_wait_breach"].notna()]
    breached, not_breached = b[b["pickup_wait_breach"] == True], b[b["pickup_wait_breach"] == False]  # noqa: E712

    # M5: do late orders get help in time?
    late_any = late["intervention_count"] > 0
    late_prev = late["preventive_intervention_count"] > 0
    iv_total = int(j["intervention_count"].sum())
    iv_prev = int(j["preventive_intervention_count"].sum())

    metrics = {
        "M1_late_delivery_rate": {
            "value_pct": _pct(len(late), len(m)),
            "numerator_late_orders": len(late), "denominator_measurable_deliveries": len(m),
            "definition": canon_key, "formula": "count(delay_min > 0) / count(delivered AND actual_delivery_at not null)",
            "grain": "order", "role": "Project KPI",
        },
        "M2_late_severity": {
            "median_delay_of_late_orders_min": _med(late["delay_min"]),
            "p90_delay_of_late_orders_min": round(float(late["delay_min"].quantile(0.9)), 1) if len(late) else None,
            "late_over_10min_pct_of_deliveries": _pct((m["delay_min"] > 10).sum(), len(m)),
            "formula": "median(delay_min | late); share of measurable deliveries with delay_min > 10",
            "grain": "order", "role": "How bad is late? (Support Lead's >10 min view)",
        },
        "M3_stage_delay_gap": {
            "stages": stages, "largest_gap_stage": worst,
            "formula": "median(stage minutes | late) - median(stage minutes | on time)",
            "grain": "order x workflow stage", "role": "Where in the workflow delay accumulates",
        },
        "M4_pickup_wait_breach": {
            "threshold_min": threshold,
            "breach_rate_pct": _pct(len(breached), len(b)),
            "late_rate_when_breached_pct": _pct((breached["is_late"] == True).sum(), len(breached)),  # noqa: E712
            "late_rate_when_not_breached_pct": _pct((not_breached["is_late"] == True).sum(), len(not_breached)),  # noqa: E712
            "orders_evaluated": len(b),
            "formula": f"share of deliveries with assigned_to_pickup_min > {threshold}; late rate split by breach",
            "grain": "order", "role": "Leading indicator Ops can act on before the ETA is missed",
        },
        "M5_intervention_coverage": {
            "late_orders_with_any_intervention_pct": _pct(late_any.sum(), len(late)),
            "late_orders_with_preventive_intervention_pct": _pct(late_prev.sum(), len(late)),
            "interventions_after_delivery_pct": _pct(iv_total - iv_prev, iv_total),
            "late_rate_with_preventive_intervention_pct": _pct(
                (m.loc[m["preventive_intervention_count"] > 0, "is_late"] == True).sum(),  # noqa: E712
                (m["preventive_intervention_count"] > 0).sum()),
            "late_rate_without_intervention_pct": _pct(
                (m.loc[m["intervention_count"] == 0, "is_late"] == True).sum(), (m["intervention_count"] == 0).sum()),  # noqa: E712
            "formula": "share of late orders with >=1 intervention (preventive = before actual delivery)",
            "grain": "order (interventions aggregated to order)", "role": "Are interventions reaching late orders in time?",
            "caution": "Association only: interventions are triggered by late risk, so with-vs-without is confounded",
        },
    }

    supporting = {
        "late_rate_by_traffic_bucket_pct": {k: _pct((g["is_late"] == True).sum(), len(g))  # noqa: E712
                                            for k, g in m.groupby("traffic_bucket", dropna=True)},
        "late_rate_reassigned_vs_not_pct": None if not j["dispatch_available"].all() else {
            "reassigned": _pct((m.loc[m["was_reassigned"] == True, "is_late"] == True).sum(), (m["was_reassigned"] == True).sum()),  # noqa: E712
            "not_reassigned": _pct((m.loc[m["was_reassigned"] == False, "is_late"] == True).sum(), (m["was_reassigned"] == False).sum()),  # noqa: E712
        },
    }

    trust = {
        "orders_in_window": int(len(j)),
        "cancelled_orders": int(j["is_cancelled"].sum()),
        "delivered_orders": int(j["is_delivered"].sum()),
        "measurable_deliveries": int(len(m)),
        "unmeasurable_deliveries": int(j["dq_unmeasurable_delivery"].sum()),
        "unmeasurable_recoverable_from_driver_app": int(j["dq_delivery_recoverable_from_driver_app"].sum()),
        "measurement_coverage_pct": _pct(len(m), j["is_delivered"].sum()),
        "orders_without_transit_gps": int(j["dq_no_transit_gps"].sum()),
        "dispatch_api_available": bool(j["dispatch_available"].all()),
    }

    return {"run_date": run_date, "window": window, "kpi": "Reduce late delivery rate",
            "metrics": metrics, "supporting": supporting, "data_trust": trust}
