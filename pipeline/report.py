"""Render gold metrics as a human-readable evidence table (Markdown) for Ops leadership."""
from __future__ import annotations

from typing import List

import pandas as pd


def _v(x, unit=""):
    return "n/a" if x is None else f"{x}{unit}"


def evidence_table(metrics: dict, sensitivity: pd.DataFrame, report_summary: dict, checks: List[dict]) -> str:
    m, t, w = metrics["metrics"], metrics["data_trust"], metrics["window"]
    m1, m2, m3, m4, m5 = (m[k] for k in sorted(m))
    stages = m3["stages"]

    lines = [
        f"# FlashEats late-delivery evidence — run_date {metrics['run_date']}",
        "",
        f"Window: orders created {w['created_at_from']} to {w['created_at_to_exclusive']} (exclusive) · "
        f"KPI: **{metrics['kpi']}** · Validation: {report_summary}",
        "",
        "| # | Metric | Value | Definition / grain | Why it matters |",
        "|---|---|---|---|---|",
        f"| M1 | **Late delivery rate** (KPI) | **{_v(m1['value_pct'], '%')}** ({m1['numerator_late_orders']}/{m1['denominator_measurable_deliveries']}) "
        f"| delay > 0 min ÷ delivered orders with a recorded delivery time · order | {m1['role']} |",
        f"| M2 | Late severity | median {_v(m2['median_delay_of_late_orders_min'], ' min')} late · p90 {_v(m2['p90_delay_of_late_orders_min'], ' min')} · "
        f"{_v(m2['late_over_10min_pct_of_deliveries'], '%')} of deliveries > 10 min late | among late orders · order | {m2['role']} |",
    ]
    stage_txt = " · ".join(
        f"{s['stage'].split(' (')[0]}: {_v(s['median_on_time_min'])} vs {_v(s['median_late_min'])} (gap {_v(s['gap_min'], ' min')})"
        for s in stages.values())
    lines.append(f"| M3 | Stage delay gap (on-time vs late, median min) | {stage_txt} · **largest gap: {m3['largest_gap_stage'] or 'not determinable (stage data unavailable)'}** "
                 f"| {m3['formula']} · {m3['grain']} | {m3['role']} |")
    lines.append(f"| M4 | Pickup-wait breach (> {m4['threshold_min']} min assigned→pickup) | {_v(m4['breach_rate_pct'], '%')} of deliveries breach; "
                 f"late rate {_v(m4['late_rate_when_breached_pct'], '%')} when breached vs {_v(m4['late_rate_when_not_breached_pct'], '%')} when not "
                 f"| order | {m4['role']} |")
    lines.append(f"| M5 | Intervention coverage of late orders | {_v(m5['late_orders_with_any_intervention_pct'], '%')} got any intervention; "
                 f"{_v(m5['late_orders_with_preventive_intervention_pct'], '%')} before delivery; "
                 f"{_v(m5['interventions_after_delivery_pct'], '%')} of all interventions happen after delivery "
                 f"| order (interventions aggregated) | {m5['role']} ⚠️ {m5['caution']} |")

    lines += [
        "",
        "## Data trust",
        "",
        "| Orders in window | Cancelled | Delivered | Measurable | Unmeasurable (recoverable from driver app) | Coverage | Dispatch API |",
        "|---|---|---|---|---|---|---|",
        f"| {t['orders_in_window']} | {t['cancelled_orders']} | {t['delivered_orders']} | {t['measurable_deliveries']} | "
        f"{t['unmeasurable_deliveries']} ({t['unmeasurable_recoverable_from_driver_app']}) | {t['measurement_coverage_pct']}% | "
        f"{'available' if t['dispatch_api_available'] else 'UNAVAILABLE (degraded run)'} |",
        "",
        "## Late-rate sensitivity to the stakeholder definition",
        "",
        "| Definition | Rule | Late / denominator | Late rate |",
        "|---|---|---|---|",
    ]
    for r in sensitivity.itertuples():
        name = f"**{r.definition}** (canonical)" if r.canonical else r.definition
        lines.append(f"| {name} | {r.label} | {r.late_orders}/{r.denominator} | {r.late_rate_pct}% |")

    issues = [c for c in checks if c["status"] in ("WARN", "ERROR")]
    lines += ["", "## Open data issues (WARN/ERROR)", "", "| Rule | Check | Affected | Handling |", "|---|---|---|---|"]
    lines += [f"| {c['rule_id']} | {c['rule']} | {c['affected']} | {c['handling']} |" for c in issues]
    return "\n".join(lines) + "\n"
