"""A tiny in-process GitLab REST API for tests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


class FakeGitLab:
    """Serves ``/api/v4/user`` and ``/api/v4/projects`` from in-memory data."""

    def __init__(self, token: str = "secret") -> None:
        self.token = token
        self.projects: list[dict[str, Any]] = []
        self.username = "syncbot"
        self.is_admin = True
        self.requests: list[tuple[str, str]] = []
        # HTTP statuses to answer with before behaving normally.
        self.fail_with: list[int] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeGitLab:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def add_project(self, project_id: int, full_path: str, **extra: Any) -> dict:
        namespace_path, _, path = full_path.rpartition("/")
        raw = {
            "id": project_id,
            "name": path,
            "path": path,
            "path_with_namespace": full_path,
            "description": "",
            "visibility": "private",
            "default_branch": "master",
            "archived": False,
            "lfs_enabled": True,
            "namespace": {
                "id": 1,
                "kind": "group",
                "full_path": namespace_path,
            },
            "http_url_to_repo": f"{self.url}/{full_path}.git",
            "statistics": {"repository_size": 1000},
            "last_activity_at": "2026-11-20T08:00:00.000Z",
        }
        raw.update(extra)
        self.projects.append(raw)
        return raw

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: Any, headers: dict | None = None):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                fake.requests.append(("GET", self.path))
                if fake.fail_with:
                    status = fake.fail_with.pop(0)
                    return self._send(status, {"message": f"injected {status}"})
                if self.headers.get("PRIVATE-TOKEN") != fake.token:
                    return self._send(401, {"message": "401 Unauthorized"})
                if parsed.path == "/api/v4/user":
                    return self._send(
                        200, {"username": fake.username, "is_admin": fake.is_admin}
                    )
                if parsed.path == "/api/v4/projects":
                    per_page = int(query.get("per_page", 20))
                    page = int(query.get("page", 1))
                    chunk = fake.projects[(page - 1) * per_page : page * per_page]
                    more = page * per_page < len(fake.projects)
                    return self._send(
                        200,
                        chunk,
                        {
                            "X-Total": str(len(fake.projects)),
                            "X-Next-Page": str(page + 1) if more else "",
                        },
                    )
                return self._send(404, {"message": "404 Not Found"})

        return Handler
