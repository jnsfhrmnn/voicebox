"""JFW-13: anonymisiertes Transkript (Vollstaendigkeit, Rollen, Unsicherheit) — TDD.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_minutes_transcript.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.export.document import build_set
from backend.export.snapshot import build_snapshot
from backend.minutes.pseudonym import confirm_register, propose_register
from backend.minutes.transcript import build_transcript, verify_transcript_complete
from backend.tests.jfw4_sources import req, req_jfw11
from backend.tests.jfw13_sources import (
    TURN_ORDER,
    candidates_names,
    names_doc,
    sources_names,
)


def confirmed(cands=None):
    return confirm_register(propose_register(cands or candidates_names(), TURN_ORDER))


def test_every_turn_exactly_once_in_order():
    doc = names_doc()
    out = build_transcript(doc, confirmed(), None)
    assert [e["turn_id"] for e in out["entries"]] == ["t1", "t2", "t3"]
    assert verify_transcript_complete(doc, out["entries"]) == []


def test_turn_text_is_exact_source_slice():
    doc = names_doc()
    out = build_transcript(doc, confirmed(), None)
    text = doc["transcript"]["text"]
    for entry in out["entries"]:
        assert entry["source_text"] == text[entry["char_start"]:entry["char_end"]]


def test_speaker_labels_neutral_without_names():
    doc = names_doc()
    out = build_transcript(doc, confirmed(), None)
    labels = {e["sprecher"]["label"] for e in out["entries"]}
    assert labels == {"Sprecher 1", "Sprecher 2"}


def test_authorized_name_is_pseudonymized_never_raw():
    src = sources_names(with_jfw11=True)
    src["jfw11"]["name_mappings"] = [
        {"cluster_id": "speaker_01", "name": "Erika", "state": "confirmed",
         "revision": "nm-3", "provenance": {"quelle": "manuell", "beleg": "b1"}},
    ]
    doc = build_set(build_snapshot(req_jfw11(), src), req_jfw11())["document"]
    cands = [c for c in candidates_names() if c["candidate_id"] != "c1"]
    cands.append({"candidate_id": "c5", "kind": "person", "text": "Erika",
                  "occurrences": [{"turn_id": "t1", "start": 24, "end": 29}]})
    out = build_transcript(doc, confirmed(cands), None)
    labels = [e["sprecher"]["label"] for e in out["entries"]]
    assert "Person 1" in labels
    assert all("Erika" not in label for label in labels)


def test_unknown_authorized_name_never_leaks():
    src = sources_names(with_jfw11=True)
    src["jfw11"]["name_mappings"] = [
        {"cluster_id": "speaker_01", "name": "Erika", "state": "confirmed",
         "revision": "nm-3", "provenance": {"quelle": "manuell", "beleg": "b1"}},
    ]
    doc = build_set(build_snapshot(req_jfw11(), src), req_jfw11())["document"]
    out = build_transcript(doc, confirmed(), None)
    for entry in out["entries"]:
        assert "Erika" not in entry["sprecher"]["label"]


def test_unassigned_speaker_gets_uncertainty_label():
    src = sources_names()
    src["jfw3"]["word_assignments"][0] = {
        "word_id": "w1", "speaker_status": "nicht_zugeordnet",
        "cluster_id": None, "turn_id": "t1",
    }
    doc = build_set(build_snapshot(req(), src), req())["document"]
    out = build_transcript(doc, confirmed(), None)
    entry = out["entries"][0]
    assert entry["sprecher"]["uncertainty"] is True
    assert "unsicher" in entry["sprecher"]["label"]


def test_overlap_flag_survives():
    src = sources_names()
    src["jfw3"]["turns"][1] = dict(src["jfw3"]["turns"][1], overlap="ueberlappend")
    doc = build_set(build_snapshot(req(), src), req())["document"]
    out = build_transcript(doc, confirmed(), None)
    assert "ueberlappend" in out["entries"][1]["flags"]


def test_contribution_roles_only_with_dual_source_evidence():
    doc = names_doc()
    out = build_transcript(doc, confirmed(), None)
    assert all(e["contribution_role"] is None for e in out["entries"])

    src = sources_names(with_jfw11=True)
    src["jfw11"]["source_representations"] = [
        {"source_id": "src-a", "source": "jfw11_track_a", "turn_id": "t1",
         "start_ms": 0, "end_ms": 10, "status": "belegt", "contribution_role": "eigen"},
        {"source_id": "src-b", "source": "jfw11_track_b", "turn_id": "t2",
         "start_ms": 0, "end_ms": 10, "status": "belegt", "contribution_role": "fremd"},
    ]
    doc2 = build_set(build_snapshot(req_jfw11(), src), req_jfw11())["document"]
    out2 = build_transcript(doc2, confirmed(), None)
    roles = {e["turn_id"]: e["contribution_role"] for e in out2["entries"]}
    assert roles["t1"] == "eigen"
    assert roles["t2"] == "fremd"
    assert roles["t3"] is None  # ohne Rollenbeleg ausgeblendet


def test_verification_detects_dropped_and_duplicated_turn():
    doc = names_doc()
    out = build_transcript(doc, confirmed(), None)
    assert verify_transcript_complete(doc, out["entries"][:-1]) != []
    assert verify_transcript_complete(doc, out["entries"] + out["entries"][:1]) != []
    reordered = [out["entries"][1], out["entries"][0], out["entries"][2]]
    assert verify_transcript_complete(doc, reordered) != []
