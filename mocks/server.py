"""Run the mock services in a background thread on a free port (tests and evals)."""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn

from mocks.main import app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def running_mocks(timeout_s: float = 10) -> Iterator[str]:
    """Yields the base URL. A real socket, so client timeouts are real network timeouts."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout_s
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("mock server did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
