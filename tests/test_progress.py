import io

from airtap_sync.progress import ConsoleProgress, ProgressEvent, Stage, null_progress


def test_fraction() -> None:
    assert ProgressEvent(Stage.SCAN).fraction is None
    assert ProgressEvent(Stage.SCAN, done=1, total=4).fraction == 0.25
    assert ProgressEvent(Stage.SCAN, done=9, total=4).fraction == 1.0


def test_console_progress_renders_line() -> None:
    out = io.StringIO()
    ConsoleProgress(out)(
        ProgressEvent(Stage.IMPORT, "docs/a.txt", done=2, total=10, unit="files")
    )
    assert out.getvalue() == "[导入] 2/10 files docs/a.txt\n"


def test_null_progress_accepts_events() -> None:
    null_progress(ProgressEvent(Stage.DONE))
