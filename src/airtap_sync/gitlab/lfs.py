"""Minimal Git LFS Batch API client for a GitLab project."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from airtap_sync.gitlab.client import GitLabAuthError, GitLabError, GitLabNotFound

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
            if e.code == 404:
                raise GitLabNotFound(
                    "该工程没有启用 LFS，或服务端未开启 LFS（HTTP 404）", 404
                ) from None
            raise GitLabError(f"LFS 请求失败（HTTP {e.code}）", e.code) from None
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            reason = getattr(e, "reason", e)
            raise GitLabError(f"LFS 请求失败：{reason}") from None
        return list(doc.get("objects", []))
