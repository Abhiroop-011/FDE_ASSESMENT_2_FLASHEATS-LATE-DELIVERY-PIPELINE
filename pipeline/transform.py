"""TRANSFORM: reorganise source-shaped tables into one business-shaped table — the order journey.

Grain: one row per order in the KPI window. Every one-to-many source (events, interventions,
tickets, app actions) is aggregated to order_id BEFORE the left join, so joins cannot fan out.
"""
from __future__ import annotations

from typing import Dict

import pandas as pd


def _minutes(end: pd.Series, start: pd.Series) -> pd.Series:
    return (end - start).dt.total_seconds() / 60


def _join_types(s: pd.Series) -> str:
    return ",".join(sorted(set(s.dropna().astype(str))))


def build_order_journey(frames: Dict[str, pd.DataFrame], cfg, dispatch_available: bool, log) -> pd.DataFrame:
    orders = frames["orders"]
    j = orders.copy()

    # --- entity attributes (restaurant dimension)
    rest = frames["restaurants"][["restaurant_id", "cuisine", "manual_status_updates"]].rename(
        columns={"cuisine": "restaurant_cuisine", "manual_status_updates": "restaurant_manual_status_updates"})
    j = j.merge(rest, on="restaurant_id", how="left")

    # --- dispatch (1:1 per order)
    d = frames["dispatch"]
    disp_cols = ["order_id", "assigned_at", "reassigned_at", "original_driver_id", "current_delivery_eta", "eta_model_version"]
    if dispatch_available:
        j = j.merge(d[disp_cols], on="order_id", how="left")
        j["was_reassigned"] = j["reassigned_at"].notna()
    else:
        for col in disp_cols[1:]:
            j[col] = pd.NaT if col.endswith("_at") or col.endswith("eta") else None
        j["was_reassigned"] = pd.NA
    j["dispatch_available"] = dispatch_available

    # --- driver app events (1:N -> aggregate)
    de = frames["driver_events"]
    first = de.pivot_table(index="order_id", columns="type", values="timestamp", aggfunc="min")
    app = pd.DataFrame(index=first.index)
    app["app_picked_up_at"] = first.get("picked_up")
    app["app_delivered_at"] = first.get("delivered")
    pings = de[de["type"].eq("gps_ping")].merge(app.reset_index(), on="order_id", how="left")
    in_transit = (pings["timestamp"] > pings["app_picked_up_at"]) & (pings["timestamp"] < pings["app_delivered_at"])
    app["gps_pings_total"] = pings.groupby("order_id").size()
    app["gps_pings_in_transit"] = pings[in_transit].groupby("order_id").size()
    j = j.merge(app.reset_index(), on="order_id", how="left")

    # --- restaurant status (latest status per order)
    rs = frames["restaurant_status"].sort_values("last_updated_at").groupby("order_id").tail(1)
    j = j.merge(rs[["order_id", "status", "last_updated_at"]].rename(
        columns={"status": "restaurant_last_status", "last_updated_at": "restaurant_status_updated_at"}), on="order_id", how="left")

    # --- interventions (1:N -> aggregate; split preventive vs after-the-fact)
    iv = frames["interventions"].merge(orders[["order_id", "actual_delivery_at"]], on="order_id", how="inner")
    iv["preventive"] = iv["actual_delivery_at"].isna() | (iv["intervention_at"] < iv["actual_delivery_at"])
    iv_summary = iv.groupby("order_id").agg(
        intervention_count=("intervention_id", "count"),
        preventive_intervention_count=("preventive", "sum"),
        intervention_types=("intervention_type", _join_types),
        first_intervention_at=("intervention_at", "min"),
    )
    j = j.merge(iv_summary.reset_index(), on="order_id", how="left")

    # --- support tickets and app actions (1:N -> aggregate)
    tk = frames["support_tickets"].groupby("order_id").agg(
        ticket_count=("ticket_id", "count"), ticket_categories=("category", _join_types), first_ticket_at=("created_at", "min"))
    j = j.merge(tk.reset_index(), on="order_id", how="left")
    aa = frames["app_actions"]
    actions = pd.crosstab(aa["order_id"], aa["action_type"]).rename(columns={
        "ETA_VIEWED": "eta_views", "SUPPORT_OPENED": "support_opened", "CANCEL_ATTEMPTED": "cancel_attempts"})
    j = j.merge(actions.reset_index(), on="order_id", how="left")

    count_cols = ["gps_pings_total", "gps_pings_in_transit", "intervention_count", "preventive_intervention_count",
                  "ticket_count", "eta_views", "support_opened", "cancel_attempts"]
    for col in count_cols:
        if col not in j:
            j[col] = 0
        j[col] = j[col].fillna(0).astype(int)

    # --- states and outcome
    j["is_delivered"] = j["final_status"].eq("delivered")
    j["is_cancelled"] = j["final_status"].eq("cancelled")
    j["is_measurable"] = j["is_delivered"] & j["actual_delivery_at"].notna()

    canonical = cfg.late_definition["definitions"][cfg.late_definition["canonical"]]
    j["delay_min"] = _minutes(j["actual_delivery_at"], j["promised_eta"])
    j["is_late"] = (j["delay_min"] > canonical["threshold_min"]).where(j["is_measurable"]).astype("boolean")
    j["is_late_over_10min"] = (j["delay_min"] > 10).where(j["is_measurable"]).astype("boolean")

    # --- data-quality flags (kept on the row, never silently dropped)
    j["dq_unmeasurable_delivery"] = j["is_delivered"] & j["actual_delivery_at"].isna()
    j["dq_delivery_recoverable_from_driver_app"] = j["dq_unmeasurable_delivery"] & j["app_delivered_at"].notna()
    j["dq_delivery_before_pickup"] = j["actual_delivery_at"] < j["pickup_at"]
    j["dq_promised_before_created"] = j["promised_eta"] < j["created_at"]
    j["dq_pickup_before_assigned"] = j["pickup_at"] < j["assigned_at"]
    j["dq_no_transit_gps"] = j["is_delivered"] & j["app_delivered_at"].notna() & j["gps_pings_in_transit"].eq(0)

    # --- workflow stage durations (null when the stage's own chronology is invalid or the order was cancelled)
    live = j["is_delivered"]
    j["promised_duration_min"] = _minutes(j["promised_eta"], j["created_at"])
    j["created_to_assigned_min"] = _minutes(j["assigned_at"], j["created_at"]).where(live)
    j["assigned_to_pickup_min"] = _minutes(j["pickup_at"], j["assigned_at"]).where(live & ~j["dq_pickup_before_assigned"])
    j["pickup_to_delivered_min"] = _minutes(j["actual_delivery_at"], j["pickup_at"]).where(live & ~j["dq_delivery_before_pickup"])
    threshold = cfg.raw["metrics"]["pickup_wait_breach_min"]
    j["pickup_wait_breach"] = (j["assigned_to_pickup_min"] > threshold).where(j["assigned_to_pickup_min"].notna()).astype("boolean")

    log.info("Built order_journey | rows=%s unique_orders=%s columns=%s", len(j), j["order_id"].nunique(), len(j.columns))
    return j
