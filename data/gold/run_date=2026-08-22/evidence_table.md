# FlashEats late-delivery evidence — run_date 2026-08-22

Window: orders created 2026-07-25 to 2026-08-22 (exclusive) · KPI: **Reduce late delivery rate** · Validation: {'PASS': 31, 'INFO': 5, 'WARN': 11, 'ERROR': 0}

| # | Metric | Value | Definition / grain | Why it matters |
|---|---|---|---|---|
| M1 | **Late delivery rate** (KPI) | **56.7%** (633/1116) | delay > 0 min ÷ delivered orders with a recorded delivery time · order | Project KPI |
| M2 | Late severity | median 8.2 min late · p90 22.0 min · 23.7% of deliveries > 10 min late | among late orders · order | How bad is late? (Support Lead's >10 min view) |
| M3 | Stage delay gap (on-time vs late, median min) | Order created -> driver assigned: 2.3 vs 2.3 (gap 0.0 min) · Driver assigned -> food picked up: 16.7 vs 28.8 (gap 12.1 min) · Picked up -> delivered: 45.1 vs 49.0 (gap 3.9 min) · **largest gap: assigned_to_pickup_min** | median(stage minutes | late) - median(stage minutes | on time) · order x workflow stage | Where in the workflow delay accumulates |
| M4 | Pickup-wait breach (> 25 min assigned→pickup) | 40.7% of deliveries breach; late rate 95.1% when breached vs 31.2% when not | order | Leading indicator Ops can act on before the ETA is missed |
| M5 | Intervention coverage of late orders | 27.8% got any intervention; 24.6% before delivery; 11.0% of all interventions happen after delivery | order (interventions aggregated) | Are interventions reaching late orders in time? ⚠️ Association only: interventions are triggered by late risk, so with-vs-without is confounded |

## Data trust

| Orders in window | Cancelled | Delivered | Measurable | Unmeasurable (recoverable from driver app) | Coverage | Dispatch API |
|---|---|---|---|---|---|---|
| 1193 | 54 | 1139 | 1116 | 23 (23) | 98.0% | available |

## Late-rate sensitivity to the stakeholder definition

| Definition | Rule | Late / denominator | Late rate |
|---|---|---|---|
| **fde_agreed** (canonical) | Delivered after promised ETA (> 0 min); denominator = delivered orders with a recorded delivery time | 633/1116 | 56.7% |
| vp_operations | Any delivered order after promised ETA; all delivered orders in denominator | 633/1139 | 55.6% |
| support_lead | Only > 10 min beyond ETA counts | 264/1116 | 23.7% |
| finance_naive_all_orders | Counterfactual: cancelled orders NOT excluded (what Finance warned against) | 633/1193 | 53.1% |
| data_team_dashboard | Historical dashboard: delivered orders with non-null actual delivery time | 633/1116 | 56.7% |
| sensitivity_driver_app_backfill | Canonical, with missing delivery times back-filled from driver app | 650/1139 | 57.1% |

## Open data issues (WARN/ERROR)

| Rule | Check | Affected | Handling |
|---|---|---|---|
| S04 | order_id is unique (grain = one row per order) | 2 | Keep first occurrence (agreed rule). Timestamps identical, so KPI unaffected; traffic_bucket for these orders is ambiguous |
| S08 | orders.driver_id is populated | 3 | Kept in KPI; excluded from per-driver/restaurant breakdowns |
| S08 | orders.restaurant_id is populated | 2 | Kept in KPI; excluded from per-driver/restaurant breakdowns |
| L02 | Delivered orders have actual_delivery_at | 23 | Excluded from KPI denominator and reported as 'unmeasurable'; NOT back-filled (driver-app recovery shown as sensitivity) |
| L03 | Delivery happens after pickup | 3 | Kept in KPI (delay uses promised vs delivered only); excluded from pickup->delivered stage metric |
| L04 | Promised ETA is after order creation | 2 | Kept in KPI (max impact 0.3 pp) and listed as assumption; needs order-platform owner to confirm |
| L06 | Pickup happens after dispatch assignment | 10 | Excluded from assigned->pickup stage metric |
| X03 | support_tickets.order_id is populated | 3 | Cannot be linked to an order; excluded from journey |
| X04 | support_tickets has no duplicate ticket_id | 1 | Exact duplicates removed in clean step |
| X04 | restaurant_status has no duplicate rows | 2 | Exact duplicates removed in clean step |
| X08 | Driver app and order DB pickup times agree within 60s | 3 | Order DB pickup_at used; disagreement recorded as instrumentation issue |
