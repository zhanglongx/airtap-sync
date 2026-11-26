"""The build helpers: icon conversion, clean-up and the Makefile."""

from __future__ import annotations

import importlib.util
import io
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from airtap_sync.resources import icon_png

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_application_icon_is_shipped() -> None:
    PIL_Image = pytest.importorskip("PIL.Image")  # noqa: N806
    png = icon_png()
    assert png is not None and png.name == "icon.png"
    with PIL_Image.open(png) as image:
        assert image.size == (512, 512)
        assert image.mode == "RGBA"


def test_ico_contains_the_usual_windows_sizes(tmp_path: Path) -> None:
    PIL_Image = pytest.importorskip("PIL.Image")  # noqa: N806
    make_icon = load_script("make_icon")
    ico = make_icon.make_ico(output=tmp_path / "out" / "icon.ico")
    assert ico.is_file()
    with PIL_Image.open(ico) as image:
        sizes = {size[0] for size in image.info["sizes"]}
    assert {16, 32, 48, 256} <= sizes


def test_clean_removes_artefacts_but_keeps_the_rest(tmp_path: Path) -> None:
    clean = load_script("clean")
    for name in ("build", "dist", ".pytest_cache", ".venv", "src/pkg.egg-info"):
        (tmp_path / name).mkdir(parents=True)
    (tmp_path / "src" / "mod" / "__pycache__").mkdir(parents=True)
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "file.txt").write_text("x")

    removed = clean.clean(root=tmp_path)
    assert {p.name for p in removed} >= {
        "build",
        "dist",
        ".pytest_cache",
        "__pycache__",
    }
    assert not (tmp_path / "build").exists()
    assert (tmp_path / ".venv").exists()  # only `distclean` removes it
    assert (tmp_path / "keep" / "file.txt").exists()

    clean.clean(everything=True, root=tmp_path)
    assert not (tmp_path / ".venv").exists()
    assert clean.clean(root=tmp_path) == []  # nothing left to do


def test_the_spec_wires_the_icon_and_data() -> None:
    text = (ROOT / "packaging" / "airtap-sync.spec").read_text(encoding="utf-8")
    assert text.count("icon=exe_icon") == 2  # both executables
    assert "AIRTAP_ICON" in text and 'sys.platform == "win32"' in text
    assert "airtap_sync/assets" in text  # the PNG used at run time


def test_build_succeeds_with_restricted_output_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build = load_script("build_exe")
    output = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr("sys.stdout", output)
    monkeypatch.setattr(build, "ROOT", tmp_path / "中文")
    monkeypatch.setenv("TCL_LIBRARY", "obsolete")
    monkeypatch.setattr(build, "_tk_works", lambda env: False)
    monkeypatch.setattr(build.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(build.shutil, "make_archive", lambda *args: "中文.zip")
    monkeypatch.setattr(build, "version", lambda name: "0.1.0")
    if build.sys.platform == "win32":
        make_icon = load_script("make_icon")
        monkeypatch.setattr(make_icon, "make_ico", lambda: tmp_path / "icon.ico")
        monkeypatch.setitem(build.sys.modules, "make_icon", make_icon)

    assert build.main() == 0
    output.flush()
    text = output.buffer.getvalue().decode("cp1252")
    assert r"\u7a0b\u5e8f\u76ee\u5f55" in text
    assert r"\u4e2d\u6587.zip" in text


needs_make = pytest.mark.skipif(
    shutil.which("make") is None, reason="make not installed"
)


def make(*args: str) -> str:
    done = subprocess.run(
        ["make", *args], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return done.stdout


@needs_make
def test_make_help_lists_the_targets() -> None:
    out = make("help")
    for target in ("build", "install", "test", "lint", "clean", "distclean", "icon"):
        assert f"make {target}" in out or f"make / make {target}" in out


@needs_make
def test_plain_make_builds_the_executables() -> None:
    out = make("-n")
    assert "scripts/build_exe.py" in out
    assert "-m venv" in out or "-m pip install" in out or ".venv" in out


@needs_make
def test_make_with_an_explicit_interpreter_skips_the_venv() -> None:
    out = make("-n", "PY=python", "test")
    assert "python -m pytest" in out
    assert "venv" not in out and "pip install" not in out


@needs_make
def test_make_clean_targets() -> None:
    assert "scripts/clean.py" in make("-n", "clean")
    assert "scripts/clean.py --all" in make("-n", "distclean")
