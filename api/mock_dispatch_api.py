"""Mock FlashEats Dispatch API (stands in for the client's dispatch service).

Behaviour copied from the class pack so failures are deterministic per server process:
  * page 3 returns HTTP 500 on its first request
  * page 5 returns HTTP 429 (retry_after_seconds=1) on its first request
Usage: python api/mock_dispatch_api.py --port 8000
"""
import argparse
import json
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)
DATA = json.loads((Path(__file__).parent / "dispatch_data.json").read_text())
page_hits = {}


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "flasheats-dispatch-api", "records": len(DATA)})


@app.get("/dispatch/orders")
def list_orders():
    page = max(1, int(request.args.get("page", 1)))
    page_size = min(200, max(1, int(request.args.get("page_size", 50))))
    page_hits[page] = page_hits.get(page, 0) + 1

    if page == 3 and page_hits[page] == 1:
        return jsonify({"error": "temporary upstream failure", "retryable": True}), 500
    if page == 5 and page_hits[page] == 1:
        return jsonify({"error": "rate limit exceeded", "retry_after_seconds": 1}), 429

    start, end = (page - 1) * page_size, page * page_size
    return jsonify({
        "data": DATA[start:end],
        "page": page,
        "page_size": page_size,
        "has_more": end < len(DATA),
        "total_records": len(DATA),
    })


@app.get("/dispatch/orders/<order_id>")
def get_order(order_id):
    record = next((x for x in DATA if x["order_id"] == order_id), None)
    if record is None:
        return jsonify({"error": "order not found", "order_id": order_id}), 404
    return jsonify(record)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    app.run(host="127.0.0.1", port=args.port, debug=False)
