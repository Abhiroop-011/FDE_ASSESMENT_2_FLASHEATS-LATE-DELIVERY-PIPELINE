"""CLEAN: apply only the agreed, reversible rules. Every rule is logged with the rows it touched.

What clean deliberately does NOT do: back-fill missing delivery times, drop chronology violations,
or delete outliers. Those are flagged on the journey table (dq_* columns) and handled per metric.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import pandas as pd

from .validate import norm, ts

TICKET_CATEGORY_MAP = {"late delivery": "late_delivery", "eta issue": "eta_changed"}
RESTAURANT_STATUS_MAP = {"handoff": "handed_off"}

TIMESTAMPS = {
    "orders": ["created_at", "promised_eta", "pickup_at", "actual_delivery_at"],
    "dispatch": ["assigned_at", "reassigned_at", "estimated_pickup_at", "current_delivery_eta"],
    "interventions": ["intervention_at"],
    "support_tickets": ["created_at"],
    "restaurant_status": ["last_updated_at"],
    "app_actions": ["action_at"],
    "driver_events": ["timestamp"],
}


def clean_sources(sources: Dict[str, "ExtractResult"], log) -> Tuple[Dict[str, pd.DataFrame], List[dict]]:  # noqa: F821
    frames = {name: r.df.copy() for name, r in sources.items()}
    actions: List[dict] = []

    def record(rule, dataset, rows_affected, note=""):
        actions.append({"rule": rule, "dataset": dataset, "rows_affected": int(rows_affected), "note": note})
        log.info("%-38s %-18s rows_affected=%s %s", rule, dataset, rows_affected, note)

    orders = frames["orders"]
    before = len(orders)
    orders = orders.drop_duplicates("order_id", keep="first").copy()
    record("C01 dedupe to one row per order", "orders", before - len(orders), "keep first occurrence")

    for col in ("final_status", "traffic_bucket", "weather_bucket"):
        normalised = norm(orders[col])
        record("C02 normalise category case/space", f"orders.{col}", (normalised != orders[col]).sum())
        orders[col] = normalised
    orders["distance_km_estimate"] = pd.to_numeric(orders["distance_km_estimate"], errors="coerce")
    frames["orders"] = orders

    tickets = frames["support_tickets"]
    before = len(tickets)
    tickets = tickets.drop_duplicates().copy()
    record("C03 drop exact duplicate rows", "support_tickets", before - len(tickets))
    category = norm(tickets["category"]).replace(TICKET_CATEGORY_MAP)
    record("C02 normalise category case/space", "support_tickets.category", (category != tickets["category"]).sum(),
           "'Late Delivery'->late_delivery, 'ETA issue'->eta_changed")
    tickets["category"] = category
    frames["support_tickets"] = tickets

    rs = frames["restaurant_status"]
    before = len(rs)
    rs = rs.drop_duplicates().copy()
    record("C03 drop exact duplicate rows", "restaurant_status", before - len(rs))
    status = norm(rs["status"]).replace(RESTAURANT_STATUS_MAP)
    record("C02 normalise category case/space", "restaurant_status.status", (status != rs["status"]).sum(),
           "'handoff'->handed_off; 'unknown' kept as unknown")
    rs["status"] = status
    frames["restaurant_status"] = rs

    if len(frames["dispatch"]):
        frames["dispatch"]["dispatch_status"] = norm(frames["dispatch"]["dispatch_status"])

    for name, cols in TIMESTAMPS.items():
        df = frames[name]
        for col in cols:
            if col in df:
                parsed = ts(df[col])
                lost = int((parsed.isna() & df[col].notna()).sum())
                if lost:
                    record("C04 unparseable timestamp -> NaT", f"{name}.{col}", lost)
                df[col] = parsed
    record("C04 parse timestamps (format='mixed')", "all", 0, "seconds + microsecond precision mixed in sources")

    ids = set(frames["order_keys"]["order_id"])
    for name in ("support_tickets", "interventions", "app_actions", "restaurant_status", "driver_events", "dispatch"):
        df = frames[name]
        unlinkable = df["order_id"].isna() | ~df["order_id"].isin(ids)
        if unlinkable.any():
            record("C05 exclude rows that cannot link to an order", name, unlinkable.sum(), "counted in validation X02/X03")
        frames[name] = df[~unlinkable]

    return frames, actions
