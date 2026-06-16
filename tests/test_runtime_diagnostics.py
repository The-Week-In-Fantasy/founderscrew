from pathlib import Path

from founderscrew.runtime_diagnostics import source_newer_than


def test_source_newer_than_detects_newer_python_file(tmp_path):
    source = tmp_path / "module.py"
    source.write_text("x = 1\n", encoding="utf-8")

    newest = source.stat().st_mtime

    assert source_newer_than(newest - 5, Path(tmp_path)) is True
    assert source_newer_than(newest + 5, Path(tmp_path)) is False
