# Known / Unknown / Assumption / Limitation

All numbers are from `data/gold/run_date=2026-08-29/` (orders created 2026-08-01 → 2026-08-28).

## Known (backed by validated data)
- **56.4% of measurable deliveries are late** (843 / 1,495). This reproduces leadership's claim and matches `order_outcomes.csv` row for row (0 mismatches).
- **Late is mostly mild:** median 8.0 min late; 23.3% of deliveries are more than 10 min late; p90 is 22.3 min.
- **Delay accumulates between assignment and pickup.** Late orders spend a median 28.8 min there vs 16.8 min for on-time orders (+12.0). Dispatch assignment shows no gap (2.3 vs 2.3), and transit only +3.7.
- **Traffic lengthens transit, not the pickup stage.** Transit median rises from 43.5 min (low) to 61.9 min (severe), while assigned → pickup stays at ~22–24 min in every bucket.
- **Pickup wait > 25 min is a strong warning sign:** 94.4% of those orders end up late vs 30.7% otherwise, and 41.2% of deliveries breach it.
- **73% of late orders get no intervention.** Only 24.2% get one before delivery, and 45 of the 64 customer credits happen after delivery.

## Unknown (cannot be answered with current data)
- **Is the pickup wait restaurant prep or driver travel?** No source has a *driver-arrived-at-restaurant* event, and restaurant status holds only the latest value, with no history.
- **Do interventions work?** Interventions are triggered by late risk, so late rate with vs without (56.8% vs 56.4%) is confounded. Only a controlled trial can show effect.
- **Which intervention record is right?** The intervention log (430) and order-event stream (260) overlap on only 75 orders.
- **Who owns the "late" KPI?** The client hasn't documented an owner.
- **Why 37 delivered orders lack a delivery time,** and why 5 orders have `pickup_at` = delivery + 5 min.

## Assumptions (stated, reversible in `config/config.yaml`)
| Assumption | Where | Impact if wrong |
|---|---|---|
| Late = delivered **> 0 min** after promised ETA; denominator = delivered orders with a delivery time | `late_definition.canonical` | Shown in sensitivity table (23.3% – 56.6%) |
| Order DB is the system of record for status and timestamps; driver app is corroboration only | extract/validate | Back-fill would give 56.6% (+0.2 pp) |
| Duplicate orders: keep the first row | clean C01 | KPI unaffected; 3 orders' traffic bucket may be wrong |
| Promised ETA before creation (4 orders) is kept as-is | L04 | ≤ 0.3 pp on KPI |
| An intervention is *preventive* if it happens before `actual_delivery_at` | transform | M5 split changes |
| Pickup-wait threshold = 25 min (between on-time median ≈ 17 and late median ≈ 29) | `metrics.pickup_wait_breach_min` | Breach rate changes; the direction of the finding doesn't |
| Owners in the source map are inferred from which system creates the data | docs/source_map.md | Needs confirmation with the client |
| KPI window = trailing 28 days, run daily; freshness SLA = 2 days | `run.*` | Different cadence changes the numbers, not the logic |

## Limitations
- **Synthetic, single-city, single-month data** (Bengaluru, August 2026, 1,600 orders). Daily partitions reuse the same static extract, filtered by window.
- **Association, not causation.** No metric here proves an intervention or traffic *causes* lateness.
- **Stage metrics exclude orders with invalid chronology** (14 in assigned → pickup, 5 in transit), so they cover 1,481 and 1,490 of the 1,495 deliveries.
- **The mock Dispatch API's failures are deterministic** (500 on page 3, 429 on page 5). A real API fails less predictably; retries are bounded (4 attempts, exponential backoff).
- **No scheduler is included.** The pipeline is designed for a daily 06:00 cron/Airflow trigger (`python run_pipeline.py --run-date $(date +%F)`) but isn't wired to one.
