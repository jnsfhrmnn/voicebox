"""JFW-5: Discovery (Fundmenge, Ausschluesse, Deduplizierung) — Vertragstests (TDD).

Fake-``DiscoveryFs`` gegen den I/O-freien Kern; Reparse-/Hidden-/Unicode-/
Duplikat-/100+-Element-Faelle. Ausfuehrung:
backend/.venv/Scripts/python.exe -m pytest backend/tests/test_batch_discovery.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.batch.discovery import discover


class FakeFs:
    """Pfad -> (Eintraege, Fakten). Fakten=None => nicht lesbar; KeyError => fehlt.

    Lookups normalisieren den Anfragepfad ueber ``path_identity_key`` — der Kern
    spricht die Probe ausschliesslich mit kanonischen Pfaden an.
    """

    def __init__(self, tree, facts):
        self.tree = tree
        self.facts = facts

    @staticmethod
    def _get(mapping, path):
        from backend.batch.identity import path_identity_key

        key = path_identity_key(path)
        for k, v in mapping.items():
            if path_identity_key(k) == key:
                return v
        raise FileNotFoundError(path)

    def list_dir(self, path):
        return list(self._get(self.tree, path))

    def file_facts(self, path):
        value = self._get(self.facts, path)
        if value is None:
            raise PermissionError(path)
        return dict(value)

    def read_chunks(self, path):
        yield self._get(self.facts, path)["content"]


def entry(name, is_dir=False, is_file=False, is_reparse=False, hidden=False, system=False):
    return {"name": name, "is_dir": is_dir, "is_file": is_file,
            "is_reparse": is_reparse, "hidden": hidden, "system": system}


def facts(size=4, mtime_ns=1, content=b"data"):
    return {"size": size, "mtime_ns": mtime_ns, "content": content}


def tree_fs():
    tree = {
        "C:/data": [
            entry("sub", is_dir=True),
            entry("clip.wav", is_file=True),
            entry("notes.txt", is_file=True),
            entry("secret", is_dir=True, hidden=True),
            entry("link", is_dir=True, is_reparse=True),
            entry("junction_target.wav", is_file=True, is_reparse=True),
        ],
        "C:/data/sub": [
            entry("deep.mp3", is_file=True),
            entry("zero.wav", is_file=True),
        ],
    }
    f = {
        "C:/data/clip.wav": facts(content=b"same"),
        "C:/data/sub/deep.mp3": facts(content=b"deep"),
        "C:/data/sub/zero.wav": facts(size=0, content=b""),
        "C:/data/other.wav": facts(content=b"same"),
        "C:/data/secret/hidden.wav": facts(),
        "C:/data/junction_target.wav": facts(),
    }
    return FakeFs(tree, f)


def test_folder_selection_recurses_and_collects_supported_media():
    out = discover([{"kind": "folder", "path": "C:/data"}], tree_fs())
    paths = [e["path"] for e in out["elements"]]
    assert any(p.endswith("clip.wav") for p in paths)
    assert any(p.endswith("deep.mp3") for p in paths)
    assert not any(p.endswith("notes.txt") for p in paths)


def test_exclusions_carry_concrete_reasons():
    out = discover([{"kind": "folder", "path": "C:/data"}], tree_fs())
    reasons = {(x["path"], x["reason_code"]) for x in out["excluded"]}
    assert ("C:/data/notes.txt", "format_nicht_unterstuetzt") in reasons
    assert ("C:/data/secret", "versteckt_oder_system") in reasons
    assert ("C:/data/link", "reparse_point_nicht_gefolgt") in reasons
    assert ("C:/data/junction_target.wav", "reparse_point_nicht_gefolgt") in reasons


def test_hidden_and_reparse_subtrees_are_not_traversed():
    out = discover([{"kind": "folder", "path": "C:/data"}], tree_fs())
    assert not any("hidden.wav" in x["path"] for x in out["elements"] + out["excluded"])


def test_missing_and_unreadable_paths_report_reasons():
    fs = tree_fs()
    out = discover(
        [{"kind": "file", "path": "C:/data/clip.wav"},
         {"kind": "file", "path": "C:/data/missing.wav"},
         {"kind": "file", "path": "C:/data/sub/zero.wav"}],
        fs,
    )
    reasons = {x["path"]: x["reason_code"] for x in out["excluded"]}
    assert reasons.get("C:/data/missing.wav") == "pfad_fehlt"
    # zero.wav ist vorhanden, aber leer -> Element mit sichtbarem Hinweis.
    zero = [e for e in out["elements"] if e["path"].endswith("zero.wav")]
    assert zero
    assert "leere_datei" in zero[0]["hints"]
    fs.facts["C:/data/sub/zero.wav"] = None
    out2 = discover([{"kind": "file", "path": "C:/data/sub/zero.wav"}], fs)
    assert out2["excluded"][0]["reason_code"] == "nicht_lesbar"


def test_same_physical_source_via_file_and_folder_merges_with_all_refs():
    out = discover(
        [{"kind": "file", "path": "C:/data/clip.wav"},
         {"kind": "folder", "path": "C:/data"},
         {"kind": "file", "path": "C:/data/./clip.wav"}],
        tree_fs(),
    )
    clips = [e for e in out["elements"] if e["path"].endswith("clip.wav")]
    assert len(clips) == 1
    assert len(clips[0]["selection_refs"]) == 3


def test_identical_bytes_at_different_paths_stay_separate_with_dup_hint():
    fs = tree_fs()
    fs.tree["C:/data"].append(entry("other.wav", is_file=True))
    out = discover([{"kind": "folder", "path": "C:/data"}], fs)
    by_name = {e["path"].split("\\")[-1].split("/")[-1]: e for e in out["elements"]}
    assert "other.wav" in by_name
    assert "clip.wav" in by_name
    assert by_name["other.wav"]["duplicate_hint"]
    assert by_name["clip.wav"]["duplicate_hint"]
    assert by_name["other.wav"]["item_id"] != by_name["clip.wav"]["item_id"]
    assert out["duplicate_groups"]


def test_order_is_deterministic_and_ids_stable():
    a = discover([{"kind": "folder", "path": "C:/data"}], tree_fs())
    b = discover([{"kind": "folder", "path": "C:/data"}], tree_fs())
    assert [e["item_id"] for e in a["elements"]] == [e["item_id"] for e in b["elements"]]
    assert [e["item_id"] for e in a["elements"]] == sorted(
        (e["item_id"] for e in a["elements"]), key=lambda i: [x["item_id"] for x in a["elements"]].index(i)
    )
    keys = [e["path_key"] for e in a["elements"]]
    assert keys == sorted(keys)


def test_hundred_plus_elements_stay_controllable():
    tree = {"C:/big": [entry(f"f{i:03d}.wav", is_file=True) for i in range(120)]}
    f = {f"C:/big/f{i:03d}.wav": facts(content=f"c{i}".encode()) for i in range(120)}
    out = discover([{"kind": "folder", "path": "C:/big"}], FakeFs(tree, f))
    assert out["counts"]["included"] == 120
    assert out["total_size"] == 120 * 4
    assert len({e["item_id"] for e in out["elements"]}) == 120


def test_preview_reports_trust_boundary_for_unc_sources():
    fs = FakeFs({"\\\\nas\\share": [entry("m.wav", is_file=True)]},
                {"\\\\nas\\share\\m.wav": facts()})
    out = discover([{"kind": "folder", "path": "\\\\nas\\share"}], fs)
    assert out["trust_boundary_paths"]
