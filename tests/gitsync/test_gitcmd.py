from __future__ import annotations

import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from airtap_sync.gitsync.gitcmd import (
    GitAuth,
    GitError,
    GitRunner,
    parse_ls_remote,
)
from airtap_sync.gitsync.scan import ls_remote_all
from tests.gitsync.gitrepo import Remote


def test_parse_ls_remote_drops_peeled_entries() -> None:
    out = "aaa\trefs/heads/main\nbbb\trefs/tags/v1\nccc\trefs/tags/v1^{}\n\n"
    assert parse_ls_remote(out) == {"refs/heads/main": "aaa", "refs/tags/v1": "bbb"}


def test_ls_remote_branches_and_tags(tmp_path: Path) -> None:
    remote = Remote(tmp_path, "r")
    sha = remote.commit("a.txt", "1")
    remote.commit("b.txt", "2", branch="dev")
    remote.tag("light")
    remote.tag("annotated", annotated=True)
    refs = GitRunner().ls_remote(remote.url)
    assert refs["refs/heads/main"] == sha
    assert set(refs) == {
        "refs/heads/main",
        "refs/heads/dev",
        "refs/tags/light",
        "refs/tags/annotated",
    }
    # The work clone is on "dev", so both tags were created there. A lightweight
    # tag points at the commit, an annotated one at its own tag object.
    assert refs["refs/tags/light"] == refs["refs/heads/dev"]
    assert refs["refs/tags/annotated"] != refs["refs/heads/dev"]


def test_ls_remote_empty_repository(tmp_path: Path) -> None:
    remote = Remote(tmp_path, "empty")
    assert GitRunner().ls_remote(remote.url) == {}


def test_ls_remote_failure_raises(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="ls-remote"):
        GitRunner().ls_remote((tmp_path / "nope.git").as_uri())


def test_missing_git_executable() -> None:
    with pytest.raises(GitError, match="找不到 git"):
        GitRunner(git="definitely-not-git").version()


def test_version_check(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = GitRunner()
    monkeypatch.setattr(runner, "version", lambda: (2, 30, 0))
    with pytest.raises(GitError, match="版本过低"):
        runner.check_version()
    monkeypatch.setattr(runner, "version", lambda: (2, 43, 0))
    runner.check_version()
    assert GitRunner().version() >= (2, 31)


def test_auth_env_is_scoped_and_not_plaintext() -> None:
    env = GitAuth("http://gitlab.example/", "s3cret").env()
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "http.http://gitlab.example/.extraHeader"
    header = env["GIT_CONFIG_VALUE_0"]
    assert header.startswith("Authorization: Basic ")
    decoded = base64.b64decode(header.rsplit(" ", 1)[1]).decode()
    assert decoded == "airtap-sync:s3cret"
    assert env["GIT_CONFIG_KEY_1"] == "http.http://gitlab.example/.proxy"


class _Recorder:
    def __init__(self) -> None:
        self.headers: list[str | None] = []
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                recorder.headers.append(self.headers.get("Authorization"))
                self.send_response(404)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        self.thread.daemon = True
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_auth_header_sent_only_to_configured_server() -> None:
    gitlab, other = _Recorder(), _Recorder()
    try:
        runner = GitRunner(GitAuth(gitlab.url, "s3cret"))
        for server in (gitlab, other):
            with pytest.raises(GitError):
                runner.ls_remote(f"{server.url}/g/p.git")
        expected = "Basic " + base64.b64encode(b"airtap-sync:s3cret").decode()
        assert gitlab.headers[0] == expected
        assert other.headers and all(h is None for h in other.headers)
    finally:
        gitlab.stop()
        other.stop()


def test_ls_remote_all_records_errors(tmp_path: Path) -> None:
    good = Remote(tmp_path, "good")
    good.commit("a", "1")
    done: list[str] = []
    results = ls_remote_all(
        GitRunner(),
        [("g/good", good.url), ("g/bad", (tmp_path / "x.git").as_uri())],
        concurrency=2,
        on_done=lambda r: done.append(r.key),
    )
    assert results["g/good"].ok
    assert "refs/heads/main" in (results["g/good"].refs or {})
    assert not results["g/bad"].ok
    assert "ls-remote" in (results["g/bad"].error or "")
    assert sorted(done) == ["g/bad", "g/good"]
