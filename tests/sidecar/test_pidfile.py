"""Tests for sidecar.main.write_pidfile_atomic — atomic pidfile creation."""

import json
import os

import pytest

from sidecar.main import write_pidfile_atomic


def test_write_pidfile_atomic_creates_file(tmp_path):
    """Writes JSON {pid, port} to <pidfile_dir>/sidecar.pid and returns the path."""
    path = write_pidfile_atomic(str(tmp_path), pid=1234, port=18791)

    assert path == str(tmp_path / "sidecar.pid")
    content = (tmp_path / "sidecar.pid").read_text()
    assert json.loads(content) == {"pid": 1234, "port": 18791}


def test_write_pidfile_atomic_creates_dir_if_missing(tmp_path):
    """If pidfile_dir doesn't exist, it is created (mkdir -p)."""
    target_dir = tmp_path / "new" / "nested" / ".openclaw"
    assert not target_dir.exists()

    path = write_pidfile_atomic(str(target_dir), pid=1, port=2)

    assert target_dir.exists()
    assert os.path.exists(path)


def test_write_pidfile_atomic_no_tmp_residue_on_success(tmp_path):
    """After a successful write, no .sidecar.pid.* tmp files remain."""
    write_pidfile_atomic(str(tmp_path), pid=1, port=2)

    residue = list(tmp_path.glob(".sidecar.pid.*"))
    assert residue == [], f"unexpected tmp files: {residue}"


def test_write_pidfile_atomic_cleans_tmp_on_failure(tmp_path, monkeypatch):
    """If os.replace fails, the .tmp file is unlinked, not orphaned."""
    def boom(*args, **kwargs):
        raise OSError("simulated rename failure")
    monkeypatch.setattr("os.replace", boom)

    with pytest.raises(OSError, match="simulated rename failure"):
        write_pidfile_atomic(str(tmp_path), pid=1, port=2)

    residue = list(tmp_path.glob(".sidecar.pid.*"))
    assert residue == [], f"orphaned tmp files: {residue}"
