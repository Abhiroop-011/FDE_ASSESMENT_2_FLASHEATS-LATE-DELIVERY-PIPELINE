#!/usr/bin/env bash
# Replays the dependability evidence end to end (~30s) and regenerates the committed sample outputs.
#   PYTHON=.venv/bin/python bash scripts/demo_dependability.sh
set -u
cd "$(dirname "$0")/.."
PY="${PYTHON:-python}"

hash_tree() { "$PY" - "$@" <<'EOF'
import hashlib, pathlib, sys
for root in sys.argv[1:]:
    for p in sorted(pathlib.Path(root).rglob("*")):
        if p.is_file():
            print(hashlib.sha256(p.read_bytes()).hexdigest()[:16], p)
EOF
}
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
run() { "$PY" run_pipeline.py "$@" > /dev/null 2>&1; echo $?; }

step "0. Clean slate"
rm -rf data logs

step "1. Normal daily runs for two dates (rolling 28-day window)"
echo "run_date=2026-08-22 exit=$(run --run-date 2026-08-22)"
echo "run_date=2026-08-29 exit=$(run --run-date 2026-08-29)"
grep -h '"value_pct"' data/gold/run_date=2026-08-2*/metrics.json

step "2. Idempotency: rerun the same date, outputs must be byte-identical"
before=$(hash_tree data/bronze/run_date=2026-08-29 data/silver/run_date=2026-08-29 data/gold/run_date=2026-08-29)
echo "rerun exit=$(run --run-date 2026-08-29)"
after=$(hash_tree data/bronze/run_date=2026-08-29 data/silver/run_date=2026-08-29 data/gold/run_date=2026-08-29)
[ "$before" == "$after" ] && echo "PASS: $(echo "$after" | wc -l | tr -d ' ') files identical" || echo "FAIL: outputs changed"

step "3. Schema break upstream (promised_eta dropped) -> validation gate, exit 2, last good output untouched"
gold_before=$(hash_tree data/gold/run_date=2026-08-29)
echo "exit=$(run --run-date 2026-08-29 --simulate missing_column)"
[ "$gold_before" == "$(hash_tree data/gold/run_date=2026-08-29)" ] && echo "PASS: gold partition unchanged"
grep -h '"status": "ERROR"' -A0 data/failed/run_date=2026-08-29/validation_report.json | head -1

step "4. Dispatch API outage -> retries with backoff, then degraded publish (exit 0, WARN)"
echo "exit=$(run --run-date 2026-08-29 --simulate api_down)"
grep -h '"dispatch_api_available"' data/gold/run_date=2026-08-29/metrics.json

step "5. Recovery: rerun once the API is back replaces the degraded partition"
echo "exit=$(run --run-date 2026-08-29)"
grep -h '"dispatch_api_available"' data/gold/run_date=2026-08-29/metrics.json
[ "$before" == "$(hash_tree data/bronze/run_date=2026-08-29 data/silver/run_date=2026-08-29 data/gold/run_date=2026-08-29)" ] \
  && echo "PASS: identical to the original good run"

step "6. Stale data: running 'today' (2026-09-27) against an August extract -> exit 2"
echo "exit=$(run --run-date 2026-09-27)"
ls data/failed

step "7. Order DB (anchor source) unreachable -> hard fail, exit 1, nothing published"
gold_before=$(hash_tree data/gold/run_date=2026-08-29)
echo "exit=$(run --run-date 2026-08-29 --simulate db_down)"
[ "$gold_before" == "$(hash_tree data/gold/run_date=2026-08-29)" ] && echo "PASS: gold partition unchanged"
ls -a data/bronze | grep -q staging && echo "FAIL: staging left behind" || echo "PASS: no staging leftovers"

step "Done — see logs/ for the on-call view of every run above"
ls logs
