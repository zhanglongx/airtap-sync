"""A tiny in-process GitLab REST API for tests."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


class FakeGitLab:
    """Serves ``/api/v4/user`` and ``/api/v4/projects`` from in-memory data."""

    def __init__(self, token: str = "secret") -> None:
        self.token = token
        self.projects: list[dict[str, Any]] = []
        self.private_groups = False  # reject public projects in groups
        self.version = "13.1.3-ee"
        self.username = "syncbot"
        self.is_admin = True
        self.requests: list[tuple[str, str]] = []
        self.lfs: dict[str, dict[str, bytes]] = {}  # project -> oid -> content
        # Hrefs in LFS answers use this host, like a GitLab whose external_url
        # differs from the address clients use.
        self.lfs_external = "http://external.invalid"
        self.groups: dict[str, dict[str, Any]] = {}  # by full_path
        self.users: dict[str, int] = {}  # username -> id
        self.bodies: list[tuple[str, dict[str, Any]]] = []  # (path, JSON body)
        self._next_id = 1000
        # HTTP statuses to answer with before behaving normally.
        self.fail_with: list[int] = []
        handler = self._handler()
        self._write_methods(handler)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
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

    @staticmethod
    def effective_level(project: dict[str, Any]) -> int:
        """The user's real role, as ``min_access_level`` sees it.

        ``inherited_level`` models a role inherited from a parent group, which
        the ``permissions`` field of the project list does not show.
        """
        perms = project.get("permissions") or {}
        levels = [
            (perms.get(k) or {}).get("access_level", 0)
            for k in ("project_access", "group_access")
        ]
        return max([*levels, project.get("inherited_level", 0)])

    def lfs_put(self, project: str, content: bytes) -> tuple[str, int]:
        """Store an LFS object in a project; return ``(oid, size)``."""
        oid = hashlib.sha256(content).hexdigest()
        self.lfs.setdefault(project, {})[oid] = content
        return oid, len(content)

    def new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def add_group(self, full_path: str) -> dict[str, Any]:
        parent, _, path = full_path.rpartition("/")
        group = {
            "id": self.new_id(),
            "name": path,
            "path": path,
            "full_path": full_path,
            "parent_id": self.groups[parent]["id"] if parent else None,
        }
        self.groups[full_path] = group
        return group

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
                if "/gitlab-lfs/objects/" in self.path:
                    project, _, oid = self.path.partition(".git/gitlab-lfs/objects/")
                    content = fake.lfs.get(project.strip("/"), {}).get(oid)
                    fake.requests.append(("LFS-GET", self.path))
                    if content is None:
                        return self._send(404, {"message": "not found"})
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                fake.requests.append(("GET", self.path))
                if fake.fail_with:
                    status = fake.fail_with.pop(0)
                    return self._send(status, {"message": f"injected {status}"})
                if self.headers.get("PRIVATE-TOKEN") != fake.token:
                    return self._send(401, {"message": "401 Unauthorized"})
                if parsed.path.startswith("/api/v4/groups/"):
                    path = unquote(parsed.path.removeprefix("/api/v4/groups/"))
                    if path in fake.groups:
                        return self._send(200, fake.groups[path])
                    return self._send(404, {"message": "404 Group Not Found"})
                if parsed.path == "/api/v4/users":
                    name = query.get("username", "")
                    found = (
                        [{"id": fake.users[name], "username": name}]
                        if name in fake.users
                        else []
                    )
                    return self._send(200, found)
                if parsed.path == "/api/v4/version":
                    return self._send(200, {"version": fake.version})
                if parsed.path == "/api/v4/user":
                    return self._send(
                        200, {"username": fake.username, "is_admin": fake.is_admin}
                    )
                if parsed.path == "/api/v4/projects":
                    per_page = int(query.get("per_page", 20))
                    page = int(query.get("page", 1))
                    listing = fake.projects
                    if "min_access_level" in query:
                        wanted = int(query["min_access_level"])
                        listing = [
                            p for p in listing if fake.effective_level(p) >= wanted
                        ]
                    chunk = listing[(page - 1) * per_page : page * per_page]
                    more = page * per_page < len(listing)
                    return self._send(
                        200,
                        chunk,
                        {
                            "X-Total": str(len(listing)),
                            "X-Next-Page": str(page + 1) if more else "",
                        },
                    )
                return self._send(404, {"message": "404 Not Found"})

        return Handler

    def _write_methods(self, handler: type[BaseHTTPRequestHandler]) -> None:
        fake = self

        def body_of(self: BaseHTTPRequestHandler) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def do_POST(self: Any) -> None:
            fake.requests.append(("POST", self.path))
            if self.path.endswith("/info/lfs/objects/batch"):
                return self._lfs_batch()
            if self.path.endswith("/gitlab-lfs/objects/verify"):
                fake.requests.append(("LFS-VERIFY", self.path))
                body_of(self)
                return self._send(200, {})
            if self.headers.get("PRIVATE-TOKEN") != fake.token:
                return self._send(401, {"message": "401 Unauthorized"})
            body = body_of(self)
            path = urlparse(self.path).path
            fake.bodies.append((path, body))
            if path == "/api/v4/groups":
                if not fake.is_admin and not body.get("parent_id"):
                    return self._send(403, {"message": "403 Forbidden"})
                parent = next(
                    (
                        g
                        for g in fake.groups.values()
                        if g["id"] == body.get("parent_id")
                    ),
                    None,
                )
                full = (
                    f"{parent['full_path']}/{body['path']}" if parent else body["path"]
                )
                if full in fake.groups:
                    return self._send(400, {"message": "Path has already been taken"})
                return self._send(201, fake.add_group(full))
            if path == "/api/v4/projects" or path.startswith("/api/v4/projects/user/"):
                return self._create_project(path, body)
            if path.endswith("/archive"):
                pid = int(path.split("/")[-2])
                for p in fake.projects:
                    if p["id"] == pid:
                        p["archived"] = True
                        return self._send(201, p)
            return self._send(404, {"message": "404 Not Found"})

        def _create_project(self: Any, path: str, body: dict[str, Any]) -> None:
            if path.endswith("/projects") and "namespace_id" not in body:
                kind, ns_path = "user", fake.username  # the caller's own namespace
            elif path.endswith("/projects"):
                ns = next(
                    (
                        g
                        for g in fake.groups.values()
                        if g["id"] == body.get("namespace_id")
                    ),
                    None,
                )
                if ns is None:
                    return self._send(400, {"message": "namespace is invalid"})
                kind, ns_path = "group", ns["full_path"]
                if fake.private_groups and body.get("visibility") == "public":
                    return self._send(
                        400,
                        {"message": {"visibility_level": ["public is not allowed"]}},
                    )
            else:
                if not fake.is_admin:
                    return self._send(403, {"message": "403 Forbidden"})
                user_id = int(path.rsplit("/", 1)[-1])
                names = [n for n, i in fake.users.items() if i == user_id]
                if not names:
                    return self._send(404, {"message": "404 User Not Found"})
                kind, ns_path = "user", names[0]
            full = f"{ns_path}/{body['path']}"
            if any(p["path_with_namespace"] == full for p in fake.projects):
                return self._send(400, {"message": "has already been taken"})
            raw = fake.add_project(
                fake.new_id(),
                full,
                name=body.get("name", body["path"]),
                description=body.get("description", ""),
                visibility=body.get("visibility", "private"),
                lfs_enabled=body.get("lfs_enabled", False),
                default_branch=None,
                namespace={"id": 1, "kind": kind, "full_path": ns_path},
            )
            return self._send(201, raw)

        def _lfs_batch(self: Any) -> None:
            body = body_of(self)
            fake.requests.append(("LFS", self.path))
            if not self.headers.get("Authorization", "").startswith("Basic "):
                return self._send(401, {"message": "no auth"})
            if not body.get("objects"):  # real GitLab answers 404 to an empty batch
                return self._send(404, {"message": "Not Found"})
            project = self.path.removesuffix(".git/info/lfs/objects/batch").strip("/")
            if not any(
                p["path_with_namespace"] == project and p.get("lfs_enabled")
                for p in fake.projects
            ):
                return self._send(404, {"message": "LFS not enabled"})
            store = fake.lfs.get(project, {})
            base = f"{fake.lfs_external}/{project}.git/gitlab-lfs/objects"
            answers = []
            for obj in body["objects"]:
                entry = {"oid": obj["oid"], "size": obj["size"], "authenticated": True}
                have = obj["oid"] in store
                if body["operation"] == "download":
                    if have:
                        entry["actions"] = {
                            "download": {"href": f"{base}/{obj['oid']}"}
                        }
                    else:
                        entry["error"] = {
                            "code": 404,
                            "message": "Object does not exist",
                        }
                elif not have:
                    entry["actions"] = {
                        "upload": {
                            "href": f"{base}/{obj['oid']}/{obj['size']}",
                            "header": {"X-Test": "1"},
                        },
                        "verify": {"href": f"{base}/verify"},
                    }
                answers.append(entry)
            return self._send(200, {"transfer": "basic", "objects": answers})

        def _lfs_put(self: Any) -> None:
            fake.requests.append(("LFS-PUT", self.path))
            if not self.headers.get("Authorization", "").startswith("Basic "):
                return self._send(401, {"message": "no auth"})
            project, _, rest = self.path.partition(".git/gitlab-lfs/objects/")
            oid, _, size = rest.partition("/")
            data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if hashlib.sha256(data).hexdigest() != oid or len(data) != int(size):
                return self._send(422, {"message": "wrong content"})
            fake.lfs.setdefault(project.strip("/"), {})[oid] = data
            return self._send(200, {})

        def do_PUT(self: Any) -> None:
            if "/gitlab-lfs/objects/" in self.path:
                return self._lfs_put()
            fake.requests.append(("PUT", self.path))
            if self.headers.get("PRIVATE-TOKEN") != fake.token:
                return self._send(401, {"message": "401 Unauthorized"})
            body = body_of(self)
            path = urlparse(self.path).path
            fake.bodies.append((path, body))
            pid = int(path.rsplit("/", 1)[-1])
            for p in fake.projects:
                if p["id"] == pid:
                    p.update(body)
                    return self._send(200, p)
            return self._send(404, {"message": "404 Not Found"})

        handler.do_POST = do_POST  # type: ignore[attr-defined]
        handler.do_PUT = do_PUT  # type: ignore[attr-defined]
        handler._create_project = _create_project  # type: ignore[attr-defined]
        handler._lfs_batch = _lfs_batch  # type: ignore[attr-defined]
        handler._lfs_put = _lfs_put  # type: ignore[attr-defined]
