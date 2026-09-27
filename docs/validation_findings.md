# Profiling & validation findings 

> Profiling tells us where to investigate; it does not tell us what to delete.
> Every rule below **reports**. Only the agreed `clean` rules change data, and each logs the rows it touched (`data/silver/run_date=*/cleaning_log.json`).

**Severity contract:** **ERROR** stops the run before anything is published and writes the report to `data/failed/`. **WARN** is published with the issue disclosed in `validation_report.json` and the evidence table. **INFO** is noted for the client with no KPI impact.

## The findings that matter for the decision

| # | Finding | Evidence | Why it matters | What we did |
|---|---|---|---|---|
| 1 | **"56% late" is only true under one definition** | 843 / 1,495 = 56.4%; 23.3% under Support's >10 min rule; 52.7% if cancelled orders are left in | A headline KPI without its definition gets argued, not acted on | Canonical definition in `config.yaml`; every alternative published in `late_definition_sensitivity.csv` |
| 2 | **37 "delivered" orders have no delivery time** | L02: 2.4% of delivered (limit 5%) | They can't be classified late/on-time | Excluded from denominator and reported as *unmeasurable*. **Not back-filled.** Back-fill shown as sensitivity (56.6%) |
| 3 | **The driver app has the missing evidence** | X07: all 37 have a driver-app `delivered` event; X06: app and DB agree to the second on the other 1,498 | The gap is an order-platform write bug, not missing reality | Raised as an instrumentation fix; client decides whether to adopt the app as fallback |
| 4 | **Duplicate orders disagree on `traffic_bucket`** | S04: O00120 (severe vs medium), O00723, O01302 | Keep-first doesn't change the KPI (timestamps identical) but makes traffic analysis ambiguous for those 3 | Keep first (agreed rule); conflict disclosed |
| 5 | **Pickup timestamp bug in the order DB** | L03 + X08 flag the **same 5 consecutive orders** (O00051–O00055). DB `pickup_at` = delivery time **+ exactly 5 min**; driver-app pickup is 43–61 min earlier and plausible | A systematic write bug, not noise. It would corrupt stage metrics | Kept in KPI (delay doesn't use pickup); excluded from transit stage; reported to order-platform owner |
| 6 | **Pickup before dispatch assignment** | L06: 14 delivered orders | Clock skew or back-dated assignment | Excluded from assigned → pickup stage only |
| 7 | **Promised ETA before order creation** | L04: 4 orders (O00101–O00104) | Impossible promise; inflates delay | Kept (≤ 0.3 pp impact); listed as assumption |
| 8 | **Cancelled orders carry a `pickup_at`** | L05: all 68 | `pickup_at` isn't evidence of a pickup (semantic mismatch) | Cancelled orders excluded from all stage metrics |
| 9 | **Two intervention systems disagree** | X09: 430 log records vs 260 event-stream records; only 75 orders in both | Intervention coverage depends on which system you believe | `order_interventions.csv` used (has initiator + reason); caveat on M5 |
| 10 | **Category spelling drift** | S06/C02: `Delivered`×5, `HIGH`×3, `ready `/`READY`/`Ready`, `handoff`, `Late Delivery`, `ETA issue` | Silent under-counting in any group-by | Normalised with an explicit mapping (logged) |
| 11 | **Orphan / unlinkable rows** | S08: 3 orders without driver_id and 3 without restaurant_id; X03: 3 tickets without order_id; X04: 1 duplicate ticket, 2 duplicate status rows | Small, but joins would silently drop or double them | Counted and excluded from the affected breakdowns only |

## Full rule catalogue: run_date 2026-08-29 (generated from `validation_report.json`)

Summary across all 47 checks: **31 PASS · 5 INFO · 11 WARN · 0 ERROR**. That includes the 3 output checks (O01 grain, O02 reconciliation with `order_outcomes.csv`, O03 non-zero denominator), all PASS.

| Rule | Category | Plain-English rule | Status | Affected | Handling |
|---|---|---|---|---|---|
| S01 | structural | orders has all required columns | PASS | 0 | — |
| S01 | structural | dispatch has all required columns | PASS | 0 | — |
| S01 | structural | interventions has all required columns | PASS | 0 | — |
| S01 | structural | support_tickets has all required columns | PASS | 0 | — |
| S01 | structural | driver_events has all required columns | PASS | 0 | — |
| S02 | structural | KPI window contains orders | PASS | 0 | — |
| S03 | structural | Freshness: newest order <= 2 days before run_date | PASS | 0 | — |
| S04 | structural | order_id is unique (grain = one row per order) | WARN | 3 | Keep first occurrence (agreed rule). Timestamps identical, so KPI unaffected; traffic_bucket for these orders is ambiguous |
| S05 | structural | final_status in ['cancelled', 'delivered'] after case/space normalisation | PASS | 0 | — |
| S06 | structural | final_status uses canonical spelling | INFO | 5 | Normalised to lower-case in clean step |
| S05 | structural | traffic_bucket in ['high', 'low', 'medium', 'severe'] after case/space normalisation | PASS | 0 | — |
| S06 | structural | traffic_bucket uses canonical spelling | INFO | 3 | Normalised to lower-case in clean step |
| S05 | structural | weather_bucket in ['clear', 'heavy_rain', 'rain'] after case/space normalisation | PASS | 0 | — |
| S06 | structural | weather_bucket uses canonical spelling | PASS | 0 | — |
| S07 | structural | distance_km_estimate within [0.1, 50] km | PASS | 0 | — |
| S08 | structural | orders.driver_id is populated | WARN | 3 | Kept in KPI; excluded from per-driver/restaurant breakdowns |
| S08 | structural | orders.restaurant_id is populated | WARN | 3 | Kept in KPI; excluded from per-driver/restaurant breakdowns |
| L01 | lifecycle | created_at parses as a timestamp | PASS | 0 | — |
| L01 | lifecycle | promised_eta parses as a timestamp | PASS | 0 | — |
| L01 | lifecycle | pickup_at parses as a timestamp | PASS | 0 | — |
| L02 | lifecycle | Delivered orders have actual_delivery_at | WARN | 37 | Excluded from KPI denominator and reported as 'unmeasurable'; NOT back-filled (driver-app recovery shown as sensitivity) |
| L03 | lifecycle | Delivery happens after pickup | WARN | 5 | Kept in KPI (delay uses promised vs delivered only); excluded from pickup->delivered stage metric |
| L04 | lifecycle | Promised ETA is after order creation | WARN | 4 | Kept in KPI (max impact 0.3 pp) and listed as assumption; needs order-platform owner to confirm |
| L05 | lifecycle | Cancelled orders have no pickup timestamp | INFO | 68 | Cancelled orders excluded from all stage metrics |
| L06 | lifecycle | Pickup happens after dispatch assignment | WARN | 14 | Excluded from assigned->pickup stage metric |
| X01 | cross-source | orders.restaurant_id exists in restaurants table | PASS | 0 | — |
| X01 | cross-source | orders.driver_id exists in drivers table | PASS | 0 | — |
| X01 | cross-source | orders.customer_id exists in customers table | PASS | 0 | — |
| X02 | cross-source | interventions.order_id links to an order | PASS | 0 | — |
| X02 | cross-source | support_tickets.order_id links to an order | PASS | 0 | — |
| X03 | cross-source | support_tickets.order_id is populated | WARN | 3 | Cannot be linked to an order; excluded from journey |
| X02 | cross-source | restaurant_status.order_id links to an order | PASS | 0 | — |
| X02 | cross-source | app_actions.order_id links to an order | PASS | 0 | — |
| X02 | cross-source | driver_events.order_id links to an order | PASS | 0 | — |
| X02 | cross-source | order_outcomes.order_id links to an order | PASS | 0 | — |
| X02 | cross-source | dispatch.order_id links to an order | PASS | 0 | — |
| X04 | cross-source | support_tickets has no duplicate ticket_id | WARN | 1 | Exact duplicates removed in clean step |
| X04 | cross-source | restaurant_status has no duplicate rows | WARN | 2 | Exact duplicates removed in clean step |
| X04 | cross-source | interventions has no duplicate intervention_id | PASS | 0 | — |
| X05 | cross-source | Dispatch and order DB agree on cancellation | PASS | 0 | — |
| X06 | cross-source | Driver app and order DB agree on delivery time (where both exist) | PASS | 0 | — |
| X07 | cross-source | Unmeasurable deliveries have no alternative evidence | INFO | 37 | Not back-filled into KPI; reported as 'recovered' sensitivity so the client can decide |
| X08 | cross-source | Driver app and order DB pickup times agree within 60s | WARN | 5 | Order DB pickup_at used; disagreement recorded as instrumentation issue |
| X09 | cross-source | Intervention log and order-event stream describe the same interventions | INFO | 540 | order_interventions.csv used as system of record (has initiator + reason); discrepancy raised with Ops |
| O01 | output | order_journey has exactly one row per window order | PASS | 0 | — |
| O02 | output | Pipeline late flag reproduces order_outcomes.csv late_flag | PASS | 0 | — |
| O03 | output | KPI denominator is non-zero | PASS | 0 | — |

## Rules that stop the pipeline (demonstrated)

| Scenario | Rule | Result |
|---|---|---|
| Upstream drops `promised_eta` (`--simulate missing_column`) | S01 → ERROR | exit 2 · report in `data/failed/` · last good gold partition byte-identical |
| Run "today" (2026-09-27) against the August extract | S02 + S03 → ERROR | exit 2 · `data/failed/run_date=2026-09-27/validation_report.json` (committed) |
| > 5% of delivered orders unmeasurable | L02 escalates WARN → ERROR | threshold in `config.yaml` (`max_unmeasurable_share`) |
| Child source links to < 95% of orders | X02 → ERROR | threshold `min_fk_coverage` |
| Journey fans out or drops orders | O01 → ERROR | grain guard before publish |
