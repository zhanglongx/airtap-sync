"""Minimal Git LFS Batch API client for a GitLab project."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from airtap_sync.gitlab.client import GitLabAuthError, GitLabError, GitLabNotFound
from airtap_sync.medium.atomicio import tmp_path_for

LFS_MEDIA_TYPE = "application/vnd.git-lfs+json"


@dataclass(frozen=True)
class LfsObject:
    oid: str  # sha256 of the content
    size: int


class LfsClient:
    """Talks to ``<base_url>/<project>.git/info/lfs`` with HTTP Basic auth."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        username: str = "airtap-sync",
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        basic = base64.b64encode(f"{username}:{token}".encode()).decode()
        self._auth = f"Basic {basic}"
        # Internal network: never go through an environment proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def batch(
        self, project_path: str, operation: str, objects: list[LfsObject]
    ) -> list[dict[str, Any]]:
        """Ask the server what to do with ``objects`` (``download``/``upload``).

        Returns the server's per-object answers, which carry the ``actions``
        (hrefs and headers) needed to transfer each object.
        """
        url = f"{self.base_url}/{project_path}.git/info/lfs/objects/batch"
        body = json.dumps(
            {
                "operation": operation,
                "transfers": ["basic"],
                "objects": [{"oid": o.oid, "size": o.size} for o in objects],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": self._auth,
                "Content-Type": LFS_MEDIA_TYPE,
                "Accept": LFS_MEDIA_TYPE,
            },
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise GitLabAuthError(
                    f"LFS 拒绝访问（HTTP {e.code}）：令牌或权限不足", e.code
                ) from None
            detail = e.read().decode("utf-8", "replace").strip()[:200]
            if e.code == 404:
                raise GitLabNotFound(
                    "LFS 返回 404（工程未启用 LFS、无权访问，或请求未带对象）："
                    f"{detail}",
                    404,
                ) from None
            raise GitLabError(
                f"LFS 请求失败（HTTP {e.code}）：{detail}", e.code
            ) from None
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            reason = getattr(e, "reason", e)
            raise GitLabError(f"LFS 请求失败：{reason}") from None
        return list(doc.get("objects", []))

    # ----- transfers ---------------------------------------------------------

    def _rewrite(self, href: str) -> str:
        """Point a server-supplied URL at the configured base URL.

        The server builds hrefs from its own ``external_url``, which may name a
        host this machine cannot resolve (or, after a restore, the *other*
        network's host). Path and query are kept, scheme and host replaced.
        """
        parts = urllib.parse.urlsplit(href)
        base = urllib.parse.urlsplit(self.base_url)
        return urllib.parse.urlunsplit(
            (base.scheme, base.netloc, parts.path, parts.query, parts.fragment)
        )

    def _headers(self, action: dict[str, Any]) -> dict[str, str]:
        headers = {str(k): str(v) for k, v in (action.get("header") or {}).items()}
        headers.setdefault("Authorization", self._auth)
        return headers

    def _request(
        self, method: str, href: str, headers: dict[str, str], data: Any = None
    ) -> Any:
        req = urllib.request.Request(
            self._rewrite(href), data=data, method=method, headers=headers
        )
        try:
            return self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace").strip()[:200]
            raise GitLabError(
                f"LFS {method} 失败（HTTP {e.code}）：{detail}", e.code
            ) from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise GitLabError(f"LFS {method} 失败：{getattr(e, 'reason', e)}") from None

    def download(self, project_path: str, obj: LfsObject, dest: Path) -> None:
        """Download one object to ``dest`` (verified by size and sha256)."""
        answer = self.batch(project_path, "download", [obj])
        entry = answer[0] if answer else {}
        if entry.get("error"):
            message = entry["error"].get("message", "")
            raise GitLabNotFound(
                f"LFS 对象 {obj.oid[:12]} 在服务器上不存在：{message}", 404
            )
        action = (entry.get("actions") or {}).get("download")
        if not action:
            raise GitLabError(f"LFS 服务器没有给出对象 {obj.oid[:12]} 的下载地址")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = tmp_path_for(dest)
        digest = hashlib.sha256()
        size = 0
        try:
            with self._request("GET", action["href"], self._headers(action)) as resp:
                with tmp.open("wb") as out:
                    while chunk := resp.read(1024 * 1024):
                        out.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            if size != obj.size or digest.hexdigest() != obj.oid:
                raise GitLabError(f"LFS 对象 {obj.oid[:12]} 下载后校验不一致")
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)

    def upload(self, project_path: str, obj: LfsObject, source: Path) -> None:
        """Upload ``source`` as ``obj`` (verified by the server)."""
        answer = self.batch(project_path, "upload", [obj])
        entry = answer[0] if answer else {}
        if entry.get("error"):
            message = entry["error"].get("message", "")
            raise GitLabError(f"LFS 服务器拒绝对象 {obj.oid[:12]}：{message}")
        actions = entry.get("actions") or {}
        upload = actions.get("upload")
        if upload is None:
            return  # the project already has it
        headers = self._headers(upload)
        headers.setdefault("Content-Type", "application/octet-stream")
        headers["Content-Length"] = str(obj.size)
        with source.open("rb") as f:
            self._request("PUT", upload["href"], headers, f).close()
        verify = actions.get("verify")
        if verify is not None:
            vheaders = self._headers(verify)
            vheaders["Content-Type"] = LFS_MEDIA_TYPE
            body = json.dumps({"oid": obj.oid, "size": obj.size}).encode("utf-8")
            self._request("POST", verify["href"], vheaders, body).close()

    def missing(self, project_path: str, objects: list[LfsObject]) -> list[LfsObject]:
        """The objects the project still needs (the server would accept an upload)."""
        if not objects:
            return []
        answer = self.batch(project_path, "upload", objects)
        needed = {e["oid"] for e in answer if (e.get("actions") or {}).get("upload")}
        return [o for o in objects if o.oid in needed]
