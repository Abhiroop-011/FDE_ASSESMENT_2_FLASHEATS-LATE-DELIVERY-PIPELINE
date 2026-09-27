"""Start/stop the mock Dispatch API as a fresh subprocess for each pipeline run."""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import requests


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def mock_dispatch_api(root: Path, log):
    """Yields the base URL of a freshly started mock API; always terminates it.

    A new process per run matters: the planted 500/429 failures only fire on the first
    request to a page, so reusing a long-lived server would hide the retry behaviour.
    A random free port avoids colliding with a stale server someone left on :8000.
    """
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, str(root / "api" / "mock_dispatch_api.py"), "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                health = requests.get(f"{url}/health", timeout=1).json()
                log.info("Mock Dispatch API healthy | url=%s records=%s", url, health.get("records"))
                break
            except requests.RequestException:
                time.sleep(0.3)
        else:
            raise RuntimeError(f"Mock Dispatch API did not become healthy on {url}")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.info("Mock Dispatch API stopped")
