"""Minimal GitLab REST client built on ``urllib``.

Only a handful of endpoints are needed, so a small client keeps the frozen
executable lean. Error messages are user-facing and therefore in Chinese.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

API_PREFIX = "/api/v4"
DEFAULT_PER_PAGE = 100
RETRY_STATUSES = frozenset({500, 502, 503, 504})


class GitLabError(Exception):
    """A GitLab request failed."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitLabAuthError(GitLabError):
    """The token is invalid, expired or lacks the required permission."""


class GitLabNotFound(GitLabError):
    """The requested resource does not exist (or is invisible to the token)."""


@dataclass(frozen=True)
class Response:
    status: int
    data: Any
    headers: dict[str, str]


class GitLabClient:
    """REST client for ``<base_url>/api/v4`` using a private access token."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 60.0,
        retries: int = 2,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep
        # GitLab sits on the internal network; never send it through a proxy
        # picked up from the environment or the Windows system settings.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Response:
        url = f"{self.base_url}{API_PREFIX}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"PRIVATE-TOKEN": self._token, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        # Only idempotent reads are retried; a repeated POST could create twice.
        attempts = 1 + (self.retries if method == "GET" else 0)

        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    return self._response(resp.status, resp.read(), resp.headers)
            except urllib.error.HTTPError as e:
                if e.code in RETRY_STATUSES and attempt < attempts:
                    self._wait(attempt, f"HTTP {e.code}")
                    continue
                raise self._error(e.code, e.read()) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < attempts:
                    self._wait(attempt, str(e))
                    continue
                reason = getattr(e, "reason", e)
                raise GitLabError(
                    f"无法连接 GitLab（{self.base_url}）：{reason}"
                ) from None
        raise AssertionError("unreachable")

    def _wait(self, attempt: int, reason: str) -> None:
        delay = self.backoff * 2 ** (attempt - 1)
        log.warning("GitLab request failed (%s), retrying in %.1fs", reason, delay)
        self._sleep(delay)

    @staticmethod
    def _response(status: int, raw: bytes, headers: Any) -> Response:
        data: Any = None
        if raw:
            try:
                data = json.loads(raw.decode("utf-8"))
            except ValueError:
                raise GitLabError("GitLab 返回了无法解析的内容", status) from None
        return Response(status, data, {k.lower(): v for k, v in headers.items()})

    @staticmethod
    def _error(status: int, raw: bytes) -> GitLabError:
        detail = ""
        try:
            doc = json.loads(raw.decode("utf-8"))
            detail = str(doc.get("message") or doc.get("error") or doc)
        except (ValueError, AttributeError):
            detail = raw.decode("utf-8", "replace")[:200]
        if status in (401, 403):
            return GitLabAuthError(
                f"GitLab 拒绝访问（HTTP {status}）：令牌失效或权限不足。{detail}",
                status,
            )
        if status == 404:
            return GitLabNotFound(f"GitLab 上找不到资源（HTTP 404）：{detail}", status)
        return GitLabError(f"GitLab 请求失败（HTTP {status}）：{detail}", status)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params).data

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, body=body or {}).data

    def put(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.request("PUT", path, body=body or {}).data

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> Iterator[Any]:
        """Yield every item of a list endpoint, following offset pagination."""
        query = dict(params or {})
        query["per_page"] = per_page
        page = 1
        while True:
            query["page"] = page
            resp = self.request("GET", path, params=query)
            items = resp.data or []
            yield from items
            next_page = resp.headers.get("x-next-page", "")
            if next_page:
                page = int(next_page)
            elif "x-next-page" in resp.headers or len(items) < per_page:
                return
            else:
                page += 1
