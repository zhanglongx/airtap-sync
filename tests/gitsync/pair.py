"""Two simulated sites exchanging one project through bundles."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from airtap_sync.gitsync.bundle import import_bundle
from airtap_sync.gitsync.cache import RepoCache
from airtap_sync.gitsync.gitcmd import GitRunner, RefMap
from airtap_sync.gitsync.refs import RefFilter
from airtap_sync.gitsync.refsync import RefSyncResult, export_bundle, sync_refs
from tests.gitsync.gitrepo import Remote, git

PROJECT = "team/app"


@dataclass
class Site:
    name: str
    remote: Remote
    cache: RepoCache
    base: RefMap = field(default_factory=dict)
    peer_view: RefMap = field(default_factory=dict)  # peer refs as last reported
    last_result: RefSyncResult | None = None


class Pair:
    def __init__(self, root: Path) -> None:
        self.runner = GitRunner()
        self.root = root
        self.sites: dict[str, Site] = {}
        for name in ("A", "B"):
            remote = Remote(root, f"gitlab-{name}")
            git("config", "receive.advertisePushOptions", "true", cwd=remote.bare)
            git("config", "receive.denyDeleteCurrent", "ignore", cwd=remote.bare)
            cache = RepoCache(root / f"cache-{name}", 10**9, self.runner)
            self.sites[name] = Site(name, remote, cache)
        self.bundles: list[Path] = []

    def __getitem__(self, name: str) -> Site:
        return self.sites[name]

    def ship(
        self,
        src: str,
        dst: str,
        *,
        full: bool = False,
        ref_filter: RefFilter | None = None,
        ci_skip: bool = True,
    ) -> RefSyncResult:
        """One half of a daily sync: ``src`` exports, ``dst`` imports and merges."""
        s, d = self.sites[src], self.sites[dst]
        flt = ref_filter or RefFilter()
        with s.cache.open(PROJECT, s.remote.url) as src_repo:
            from airtap_sync.gitsync.refsync import read_local_refs

            exported = flt.apply(read_local_refs(self.runner, src_repo))
            bundle = self.root / f"{src}-to-{dst}-{len(self.bundles)}.bundle"
            made = export_bundle(
                self.runner,
                src_repo,
                bundle,
                exported,
                peer_refs=s.peer_view,
                base=s.base,
                full=full,
            )
        with d.cache.open(PROJECT, d.remote.url) as dst_repo:
            if made:
                self.bundles.append(bundle)
                import_bundle(self.runner, dst_repo, bundle)
            result = sync_refs(
                self.runner,
                dst_repo,
                d.remote.url,
                exported,
                d.base,
                peer_side=src,
                ref_filter=flt,
                ci_skip=ci_skip,
                trash_stamp="20260101T000000Z",
            )
        d.base = result.new_base
        d.peer_view = exported
        d.last_result = result
        return result

    def sync_both(self) -> None:
        self.ship("A", "B")
        self.ship("B", "A")
        self.ship("A", "B")

    def heads(self, name: str) -> RefMap:
        out = git(
            "for-each-ref",
            "--format=%(objectname) %(refname)",
            cwd=self[name].remote.bare,
        )
        refs = {}
        for line in out.splitlines():
            oid, _, ref = line.partition(" ")
            refs[ref] = oid
        return refs
