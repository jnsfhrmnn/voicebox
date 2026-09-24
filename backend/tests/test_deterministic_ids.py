# -*- coding: utf-8 -*-
"""USCRX-2026-16006 (RL-06): deterministische IDs (Byte-Regel JFW-4).

mapping_id/decision_id sind Prefix + Inhalts-Hash (Vorbild
minutes/pseudonym.py) — identische Eingaben müssen identische Bytes ergeben.
"""
import json

from backend.meeting.dedupe import make_decision
from backend.meeting.naming import STATE_SUGGESTED, _revision


def _rev(name: str = "Muster") -> dict:
    return _revision(
        {"cluster_id": "c1", "name_candidate": name, "evidence": []},
        state=STATE_SUGGESTED,
    )


def _decision(rule_id: str = "r1") -> dict:
    return make_decision(
        ref_a={"id": "a", "span": [0, 10]},
        ref_b={"id": "b", "span": [12, 22]},
        state="duplikat_bestaetigt",
        corr=0.91,
        rule_id=rule_id,
        parent_revision="p1",
        user_action="bestaetigt",
    )


def test_mapping_id_deterministisch():
    a, b = _rev(), _rev()
    assert a["mapping_id"] == b["mapping_id"]
    assert a["mapping_id"].startswith("map_")


def test_mapping_id_unterchiedlich_bei_unterchiedlichem_inhalt():
    assert _rev("Muster")["mapping_id"] != _rev("Anders")["mapping_id"]


def test_decision_id_deterministisch_und_byteidentisch():
    d1, d2 = _decision(), _decision()
    assert d1["decision_id"] == d2["decision_id"]
    assert d1["decision_id"].startswith("dec_")
    b1 = json.dumps(d1, sort_keys=True, ensure_ascii=False).encode("utf-8")
    b2 = json.dumps(d2, sort_keys=True, ensure_ascii=False).encode("utf-8")
    assert b1 == b2, "Byte-Regel JFW-4: identische Eingaben -> identische Bytes"


def test_decision_id_unterchiedlich_bei_unterchiedlichem_inhalt():
    assert _decision("r1")["decision_id"] != _decision("r2")["decision_id"]


def test_ganze_lauf_byteidentisch():
    """Namenspolitik bestaetigt + Dedupe aktiv -> byteidentische Datenobjekte."""
    runs = []
    for _ in range(2):
        payload = {"rev": _rev(), "decision": _decision()}
        runs.append(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    assert runs[0] == runs[1]
