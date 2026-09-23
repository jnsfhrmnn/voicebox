"""JFW-5: Identitaet, Pfadkanonisierung und Inhaltsnachweis — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_batch_identity.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.batch.identity import (
    canonical_path,
    content_proof,
    element_id,
    is_unc,
    new_attempt_id,
    new_batch_id,
    path_identity_key,
    source_identity_hash,
)


def test_canonical_path_normalizes_without_following_reparse():
    # normpath/abspath, NIEMALS realpath: ein Reparse-/Symlink-Pfad bleibt als
    # gebundener Pfad erhalten und wird nicht still aufgeloest.
    p = canonical_path("C:/data/../data/clip.wav")
    assert p.endswith(("data\\clip.wav", "data/clip.wav"))
    link = "C:/data/link_to_dir/clip.wav"
    assert "target" not in canonical_path(link)


def test_path_identity_key_is_case_insensitive_on_windows():
    a = path_identity_key("C:/Data/Clip.wav")
    b = path_identity_key("c:/data/CLIP.WAV")
    assert a == b


def test_unc_paths_recognized_and_stable():
    assert is_unc("\\\\server\\share\\clip.wav")
    assert not is_unc("C:/share/clip.wav")
    assert path_identity_key("\\\\server\\share\\a.wav") == path_identity_key(
        "\\\\SERVER\\SHARE\\A.wav"
    )


def test_unicode_and_long_paths_survive():
    p = canonical_path("C:/Daten/Prüfung_ÄÖÜ_🎧/" + "x" * 120 + ".wav")
    assert "Prüfung_ÄÖÜ" in p
    assert len(path_identity_key(p)) > 10


def test_content_proof_is_full_sha256_over_all_bytes():
    data = b"abc" * 1000
    proof = content_proof(iter([data[:100], data[100:]]))
    import hashlib

    assert proof == "sha256:" + hashlib.sha256(data).hexdigest()
    # Chunk-Grenzen aendern den Nachweis nicht (vollstaendige Konsumierung).
    assert content_proof(iter([data])) == proof


def test_content_proof_detects_any_change():
    assert content_proof(iter([b"aaaa"])) != content_proof(iter([b"aaab"]))


def test_element_id_is_deterministic_from_source_identity():
    src = {"path": "C:/data/clip.wav", "size": 10, "mtime_ns": 5, "content_proof": "sha256:" + "a" * 64}
    a = element_id(src)
    b = element_id(dict(src))
    assert a == b
    assert a.startswith("jfw5-item-")
    assert len(a) == len("jfw5-item-") + 16


def test_element_ids_differ_for_identical_bytes_at_different_paths():
    proof = "sha256:" + "b" * 64
    one = {"path": "C:/data/a/clip.wav", "size": 1, "mtime_ns": 1, "content_proof": proof}
    two = {"path": "C:/data/b/clip.wav", "size": 1, "mtime_ns": 1, "content_proof": proof}
    assert element_id(one) != element_id(two)


def test_same_path_differs_in_case_gives_same_element_id():
    one = {"path": "C:/Data/Clip.wav", "size": 1, "mtime_ns": 1, "content_proof": "sha256:" + "c" * 64}
    two = {"path": "c:/data/clip.wav", "size": 1, "mtime_ns": 1, "content_proof": "sha256:" + "c" * 64}
    assert element_id(one) == element_id(two)


def test_source_identity_hash_binds_content_proof():
    base = {"path": "C:/data/clip.wav", "size": 1, "mtime_ns": 1, "content_proof": "sha256:" + "d" * 64}
    changed = dict(base, content_proof="sha256:" + "e" * 64)
    assert source_identity_hash(base) != source_identity_hash(changed)


def test_new_ids_are_unique_and_prefixed():
    ids = {new_batch_id() for _ in range(50)} | {new_attempt_id() for _ in range(50)}
    assert len(ids) == 100
    assert all(i.startswith(("jfw5-batch-", "jfw5-attempt-")) for i in ids)
