"""JFW-13: Pseudonymregister `pseudonym_v1` — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_minutes_pseudonym.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.minutes.pseudonym import (
    PERSON_UNKLAR,
    STATE_BESTAETIGT,
    STATE_GELOESCHT,
    STATE_NICHT_ERSATZBAR,
    STATE_UMBENANNT,
    STATE_UNKLAR,
    apply_action,
    assert_content_free,
    check_export_binding,
    confirm_register,
    consistency_errors,
    delete_register,
    org_label,
    propose_register,
    register_labels,
)
from backend.tests.jfw13_sources import candidates_names

TURN_ORDER = {"t1": 0, "t2": 1, "t3": 2}


def reg(**over) -> dict:
    cands = over.pop("candidates", None) or candidates_names()
    return propose_register(cands, TURN_ORDER)


def entry(register, candidate_id):
    return next(e for e in register["entries"] if e["candidate_id"] == candidate_id)


def test_assignment_order_first_occurrence():
    r = reg()
    assert entry(r, "c1")["pseudonym"] == "Person 1"
    assert entry(r, "c2")["pseudonym"] == "Organisation A"
    assert entry(r, "c4")["pseudonym"] == "Person 2"


def test_org_labels_beyond_z():
    assert org_label(1) == "Organisation A"
    assert org_label(26) == "Organisation Z"
    assert org_label(27) == "Organisation AA"
    assert org_label(28) == "Organisation AB"
    assert org_label(53) == "Organisation BA"


def test_kontakt_is_removed_not_pseudonymized():
    r = reg()
    e = entry(r, "c3")
    assert e["kind"] == "kontakt"
    assert e["pseudonym"] is None


def test_one_entity_one_pseudonym_consistent():
    r = reg()
    assert consistency_errors(r) == []
    labels = register_labels(r)
    assert len(labels) == len(set(labels))


def test_two_entities_never_share_a_pseudonym():
    cands = [
        {"candidate_id": "x1", "kind": "person", "text": "A",
         "occurrences": [{"turn_id": "t1", "start": 0, "end": 1}]},
        {"candidate_id": "x2", "kind": "person", "text": "B",
         "occurrences": [{"turn_id": "t1", "start": 2, "end": 3}]},
    ]
    r = propose_register(cands, TURN_ORDER)
    assert entry(r, "x1")["pseudonym"] != entry(r, "x2")["pseudonym"]


def test_ambiguous_candidate_is_marked_not_guessed():
    cands = [{"candidate_id": "u1", "kind": "person", "text": "Unklar", "ambiguous": True,
              "occurrences": [{"turn_id": "t1", "start": 0, "end": 6}]}]
    r = propose_register(cands, TURN_ORDER)
    e = entry(r, "u1")
    assert e["state"] == STATE_UNKLAR
    assert e["pseudonym"] == PERSON_UNKLAR


def test_confirm_creates_new_revision():
    r = reg()
    assert r["revision"] == 1
    c = confirm_register(r)
    assert c["revision"] == 2
    assert c["parent_revision_id"] == r["revision_id"]
    assert all(e["state"] == STATE_BESTAETIGT for e in c["entries"])
    # alte Revision bleibt unveraendert
    assert all(e["state"] != STATE_BESTAETIGT for e in r["entries"])


def test_rename_action_is_manual_and_versioned():
    r = confirm_register(reg())
    n = apply_action(r, {"kind": "umbenennen", "entry_id": entry(r, "c1")["entry_id"],
                         "pseudonym": "Frau M."})
    assert n["revision"] == r["revision"] + 1
    e = entry(n, "c1")
    assert e["state"] == STATE_UMBENANNT
    assert e["pseudonym"] == "Frau M."
    assert e["provenance"]["herkunft"] == "manuell"


def test_merge_action_keeps_one_pseudonym():
    r = confirm_register(reg())
    ids = [entry(r, "c1")["entry_id"], entry(r, "c4")["entry_id"]]
    n = apply_action(r, {"kind": "zusammenlegen", "entry_ids": ids})
    persons = [e for e in n["entries"] if e["kind"] == "person"]
    assert len(persons) == 1
    assert len(persons[0]["occurrences"]) == 2
    assert consistency_errors(n) == []


def test_split_action_creates_distinct_pseudonyms():
    cands = [{"candidate_id": "s1", "kind": "person", "text": "Schmidt",
              "occurrences": [{"turn_id": "t1", "start": 0, "end": 7},
                              {"turn_id": "t2", "start": 3, "end": 10}]}]
    r = confirm_register(propose_register(cands, TURN_ORDER))
    old = entry(r, "s1")
    n = apply_action(r, {"kind": "aufteilen", "entry_id": old["entry_id"],
                         "splits": [[0], [1]]})
    persons = [e for e in n["entries"] if e["kind"] == "person"]
    assert len(persons) == 2
    assert persons[0]["pseudonym"] != persons[1]["pseudonym"]
    assert consistency_errors(n) == []


def test_not_replaceable_action():
    r = confirm_register(reg())
    n = apply_action(r, {"kind": "nicht_ersetzbar", "entry_id": entry(r, "c1")["entry_id"]})
    assert entry(n, "c1")["state"] == STATE_NICHT_ERSATZBAR


def test_delete_register_removes_mapping_keeps_pseudonyms():
    r = confirm_register(reg())
    labels_before = sorted(register_labels(r))
    d = delete_register(r)
    assert d["status"] == STATE_GELOESCHT
    assert sorted(register_labels(d)) == labels_before  # Pseudonyme bleiben gueltig
    for e in d["entries"]:
        assert e.get("original_text") is None
        assert e.get("occurrences") in (None, [])


def test_export_dialog_bound_to_deleted_register_is_rejected():
    r = confirm_register(reg())
    assert check_export_binding(r, r["revision_id"]) == "ok"
    d = delete_register(r)
    # offener Dialog haengt an der beim Oeffnen gebundenen Registerrevision
    assert check_export_binding(d, r["revision_id"]) == "register_geloescht"
    assert check_export_binding(d, d["revision_id"]) == "register_geloescht"
    assert check_export_binding(r, "fremde-revision") == "register_revision_fremd"


def test_assert_content_free_rejects_mapping_fields():
    assert_content_free({"ids": ["x"], "hashes": {"a": "b"}, "status": "ok"})
    for bad in ({"original_text": "Erika"}, {"zuordnung": {"Person 1": "Erika"}},
                {"name": "Erika"}, {"register_mapping": {}}, {"email": "a@b.de"}):
        with pytest.raises(ValueError, match="inhalt_im_log"):
            assert_content_free(bad)


def test_org_labels_run_over_z_to_aa_ab():
    """AC 7: Reihenfolge erstmaligen Vorkommens, Organisation A..Z, danach AA, AB."""
    import copy
    orgs = [
        {
            "id": f"org-{i}", "kind": "organisation", "label": f"Firma {i} GmbH",
            "occurrences": [{"turn_id": "t1", "start": i * 10, "end": i * 10 + 5}],
        }
        for i in range(28)
    ]
    reg = propose_register(orgs, TURN_ORDER, register_id="reg")
    labels = [e["pseudonym"] for e in reg["entries"]]
    assert labels[0] == "Organisation A"
    assert labels[25] == "Organisation Z"
    assert labels[26] == "Organisation AA"
    assert labels[27] == "Organisation AB"
    # deterministisch bei identischer Eingabe
    reg2 = propose_register(copy.deepcopy(orgs), TURN_ORDER, register_id="reg")
    assert reg2["revision_id"] == reg["revision_id"]


def test_shared_surnames_stay_distinct_and_umlauts_survive():
    """AC 37: gleiche Familiennamen trennscharf, Umlaute/Nicht-Ersetztes unverandert."""
    cands = [
        {"id": "c1", "kind": "person", "label": "Anna Mueller",
         "occurrences": [{"turn_id": "t1", "start": 0, "end": 4}]},
        {"id": "c2", "kind": "person", "label": "Bernd Mueller",
         "occurrences": [{"turn_id": "t1", "start": 10, "end": 14}]},
        {"id": "c3", "kind": "organisation", "label": "M\u00fcller AG",
         "occurrences": [{"turn_id": "t2", "start": 5, "end": 10}]},
    ]
    reg = confirm_register(propose_register(cands, TURN_ORDER, register_id="reg"))
    labels = [e["pseudonym"] for e in reg["entries"]]
    assert len(labels) == len(set(labels))  # nie dasselbe Pseudonym
    assert reg["entries"][0]["pseudonym"] == "Person 1"
    assert reg["entries"][1]["pseudonym"] == "Person 2"
    assert reg["entries"][2]["pseudonym"] == "Organisation A"

    # Umlaut-Quelltext: nur markierte Stellen werden ersetzt, Rest bleibt bytetreu
    text = "Anna M\u00fcller und Bernd M\u00fcller bei der M\u00fcller AG \u2014 sch\u00f6n, gar nicht!"
    spans = [
        {"start": 0, "end": 11, "replacement": "Person 1", "state": "bestaetigt",
         "entry_id": "reg-e1", "conflict": False},
        {"start": 16, "end": 28, "replacement": "Person 2", "state": "bestaetigt",
         "entry_id": "reg-e2", "conflict": False},
        {"start": 37, "end": 46, "replacement": "Organisation A", "state": "bestaetigt",
         "entry_id": "reg-e3", "conflict": False},
    ]
    from backend.minutes.redaction import redact_text
    out = redact_text(text, spans)
    assert out == "Person 1 und Person 2 bei der Organisation A \u2014 sch\u00f6n, gar nicht!"
    # Reihenfolge und Wortlaut der nicht ersetzten Passagen bleiben unveraendert
    assert "sch\u00f6n, gar nicht!" in out


def test_homonym_surface_forms_flag_overlap_visibly():
    """AC 37: identische Namensformen mit ueberlappenden Markierungen werden
    sichtbar als Mehrdeutigkeit markiert statt still zugeordnet (kein Raten)."""
    cands = [
        {"id": "c1", "kind": "person", "label": "Herr M\u00fcller",
         "occurrences": [{"turn_id": "t1", "start": 0, "end": 6}]},
        {"id": "c2", "kind": "person", "label": "Herr M\u00fcller (Intern)",
         "occurrences": [{"turn_id": "t1", "start": 3, "end": 9}]},
    ]
    reg = confirm_register(propose_register(cands, TURN_ORDER, register_id="reg"))
    from backend.minutes.redaction import build_replacement_plan
    plan = build_replacement_plan(reg)
    conflict = [p for p in plan if p.get("conflict")]
    assert conflict, "Ueberschneidung muss sichtbar markiert sein"
    assert conflict[0]["replacement"] == PERSON_UNKLAR
