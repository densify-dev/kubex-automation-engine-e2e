"""Tiny in-cluster mock Kubex server for e2e tests."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from time import time
from urllib.parse import urlparse

STATE_LOCK = Lock()
STATE = {
    "heartbeats": [],
    "mutations": [],
    "policies": [],
    "states": [],
    "recommendations": [],
    "requests": [],
}


def _fixture_path() -> Path:
    raw_path = os.getenv("KUBEX_RECOMMENDATIONS_FILE", "/data/recommendations.json")
    return Path(raw_path)


def _read_json_body(handler: BaseHTTPRequestHandler):
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length) if length > 0 else b""
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def _record(kind: str, cluster_name: str, payload) -> None:
    entry = {
        "clusterName": cluster_name,
        "payload": payload,
        "timestamp": int(time()),
    }
    with STATE_LOCK:
        STATE[kind].append(entry)


def _record_request(method: str, path: str, cluster_name: str = "") -> None:
    with STATE_LOCK:
        STATE["requests"].append(
            {
                "method": method,
                "path": path,
                "clusterName": cluster_name,
                "timestamp": int(time()),
            }
        )


def _cluster_name_from_path(parts: list[str], marker: str) -> str:
    try:
        idx = parts.index(marker)
    except ValueError:
        return "unknown"
    if idx == 0:
        return "unknown"
    return parts[idx - 1]


class Handler(BaseHTTPRequestHandler):
    server_version = "kubex-mock/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _send_json(self, status: HTTPStatus, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        _record_request("GET", path)
        if path == "/healthz":
            self._send_empty(HTTPStatus.OK)
            return
        if path == "/debug/state":
            with STATE_LOCK:
                snapshot = json.loads(json.dumps(STATE))
            self._send_json(HTTPStatus.OK, snapshot)
            return

        parts = path.strip("/").split("/")
        if parts and parts[-1] == "containers":
            _record("recommendations", _cluster_name_from_path(parts, "containers"), None)
            fixture_path = _fixture_path()
            if not fixture_path.is_file():
                self._send_json(HTTPStatus.OK, [])
                return
            self._send_json(HTTPStatus.OK, json.loads(fixture_path.read_text(encoding="utf-8")))
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        _record_request("POST", path)
        if path == "/api/v2/authorize":
            self._send_json(
                HTTPStatus.OK,
                {
                    "apiToken": "0123456789abcdef",
                    "expires": int((time() + 300) * 1000),
                    "status": HTTPStatus.OK,
                },
            )
            return
        if path == "/debug/reset":
            with STATE_LOCK:
                for key in STATE:
                    STATE[key].clear()
            self._send_empty(HTTPStatus.NO_CONTENT)
            return

        parts = path.strip("/").split("/")
        if parts and parts[-1] == "mutations":
            _record("mutations", _cluster_name_from_path(parts, "mutations"), _read_json_body(self))
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if parts and parts[-1] == "policy":
            _record("policies", _cluster_name_from_path(parts, "policy"), _read_json_body(self))
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if parts and parts[-1] == "state":
            payload = _read_json_body(self) or []
            _record("states", _cluster_name_from_path(parts, "state"), payload)
            self._send_json(
                HTTPStatus.OK,
                {
                    "results": [
                        {
                            "containerId": item.get("containerId", ""),
                            "outcome": "applied",
                        }
                        for item in payload
                        if isinstance(item, dict)
                    ]
                },
            )
            return
        if parts and parts[-1] == "heartbeat":
            cluster_name = _cluster_name_from_path(parts, "heartbeat")
            _record("heartbeats", cluster_name, _read_json_body(self))
            self._send_empty(HTTPStatus.OK)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})


if __name__ == "__main__":
    listen_host = os.getenv("KUBEX_MOCK_HOST", "0.0.0.0")
    listen_port = int(os.getenv("KUBEX_MOCK_PORT", "8080"))
    server = ThreadingHTTPServer((listen_host, listen_port), Handler)
    server.serve_forever()
