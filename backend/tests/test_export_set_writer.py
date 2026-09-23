"""JFW-4: atomarer Set-Commit ueber den Set-Writer — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_export_set_writer.py
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.export.set_writer import (
    ATTEMPT_PREFIX,
    MemFs,
    SetWriteError,
    clean_orphans,
    existing_targets,
    revert_set,
    safe_file_name,
    write_set,
)


def _files():
    data = {
        "job-1-abc.json": b'{"x": 1}\n',
        "job-1-abc.srt": b"1\n00:00:00,000 --> 00:00:01,000\nHi\n",
    }
    hashes = {n: hashlib.sha256(d).hexdigest() for n, d in data.items()}
    return data, hashes


def test_write_set_creates_files_and_hashes():
    fs = MemFs()
    data, hashes = _files()
    out = write_set(fs, "C:/target", data, hashes, attempt_id="att-1")
    assert out["ok"] is True
    assert set(out["written"]) == set(data)
    for n, d in data.items():
        assert fs.read_bytes(f"C:/target/{n}") == d


def test_conflict_detection_case_insensitive():
    fs = MemFs()
    fs.write_bytes("C:/target/JOB-1-abc.srt", b"alt")
    assert existing_targets(fs, "C:/target", ["job-1-abc.srt"]) == ["JOB-1-abc.srt"]


def test_no_silent_overwrite():
    fs = MemFs()
    data, hashes = _files()
    fs.write_bytes("C:/target/job-1-abc.srt", b"ALT")
    with pytest.raises(SetWriteError) as exc:
        write_set(fs, "C:/target", data, hashes, attempt_id="att-1")
    assert exc.value.reason_code == "target_conflict"
    assert fs.read_bytes("C:/target/job-1-abc.srt") == b"ALT"  # unveraendert


def test_replace_keeps_previous_on_failure():
    fs = MemFs()
    data, hashes = _files()
    fs.write_bytes("C:/target/job-1-abc.json", b"ALTER-JSON")
    fs.fail_on_replace = {"C:/target/.jfw4-attempt-att-1-job-1-abc.srt"}
    with pytest.raises(SetWriteError):
        write_set(fs, "C:/target", data, hashes, attempt_id="att-1", replace=True)
    # Zurueckgerollt: alter Stand erhalten, keine halb neue Mischung
    assert fs.read_bytes("C:/target/job-1-abc.json") == b"ALTER-JSON"
    assert not fs.exists("C:/target/job-1-abc.srt")


def test_write_failure_leaves_no_final_files():
    fs = MemFs()
    data, hashes = _files()
    fs.fail_on_write = {f"C:/target/{ATTEMPT_PREFIX}att-1-job-1-abc.srt"}
    with pytest.raises(SetWriteError) as exc:
        write_set(fs, "C:/target", data, hashes, attempt_id="att-1")
    assert exc.value.reason_code == "schreibfehler"
    assert not fs.exists("C:/target/job-1-abc.json")
    assert not fs.exists("C:/target/job-1-abc.srt")


def test_manifest_hash_mismatch_rolls_back():
    fs = MemFs()
    data, _ = _files()
    bad = {n: "0" * 64 for n in data}
    with pytest.raises(SetWriteError) as exc:
        write_set(fs, "C:/target", data, bad, attempt_id="att-1")
    assert exc.value.reason_code == "hash_mismatch"
    assert not fs.exists("C:/target/job-1-abc.json")


def test_orphans_cleaned():
    fs = MemFs()
    data, hashes = _files()
    write_set(fs, "C:/target", data, hashes, attempt_id="att-1")
    write_set(fs, "C:/target", data, hashes, attempt_id="att-2", replace=True)
    # att-2 hat die Dateien ersetzt -> Backup-Reste liegen herum
    fs.write_bytes(f"C:/target/{ATTEMPT_PREFIX}att-3-x.json", b"{}")
    removed = clean_orphans(fs, "C:/target")
    assert removed
    assert fs.read_bytes("C:/target/job-1-abc.json") == data["job-1-abc.json"]
    assert all(ATTEMPT_PREFIX not in n and ".jfw4-replaced-" not in n
               for n in fs.listdir("C:/target"))


def test_unsafe_names_rejected():
    assert safe_file_name("ok-name.json")[0] is False or safe_file_name("ok-name.json")[0] is True
    assert safe_file_name("ok-name.json")[0] is True
    assert safe_file_name("a<b>.json")[0] is False
    assert safe_file_name("a/b.json")[0] is False
    assert safe_file_name("CON.json")[0] is False
    assert safe_file_name("x"*300 + ".json")[0] is False
    assert safe_file_name("trailing.")[0] is False
    fs = MemFs()
    data = {"a/b.json": b"x"}
    with pytest.raises(SetWriteError) as exc:
        write_set(fs, "C:/target", data, {"a/b.json": "0"*64}, attempt_id="a")
    assert exc.value.reason_code == "unsafe_file_name"


def test_revert_set_removes_new_files():
    fs = MemFs()
    data, hashes = _files()
    out = write_set(fs, "C:/target", data, hashes, attempt_id="att-9")
    revert_set(fs, "C:/target", out["written"], out["backups"])
    assert fs.listdir("C:/target") == []


def test_revert_set_restores_previous_files():
    fs = MemFs()
    data, hashes = _files()
    fs.write_bytes("C:/target/job-1-abc.json", b"ALTER-JSON")
    out = write_set(fs, "C:/target", data, hashes, attempt_id="att-9", replace=True)
    revert_set(fs, "C:/target", out["written"], out["backups"])
    assert fs.read_bytes("C:/target/job-1-abc.json") == b"ALTER-JSON"
    assert not fs.exists("C:/target/job-1-abc.srt")
