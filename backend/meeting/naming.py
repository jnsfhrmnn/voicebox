"""JFW-11: Reversible Name-Mapping-Revisionen (I/O-frei).

Spec „Speaker Assignment Contract": Name-Mappings sind Annotationen auf einer
gebundenen JFW-3-Clusterrevision; sie ersetzen weder neutrale IDs noch
Transkripttext und gelten nur innerhalb des Meeting-Jobs. Vorschläge bleiben
Vorschläge (Status ``vorgeschlagen``); ausschließlich ausdrückliches Bestätigen
oder manuelles Zuordnen setzt einen autorisierten Anzeigenamen. Jede Aktion
erzeugt eine neue Revision mit Parent-Referenz (Historie bleibt nachvollziehbar);
ein Clusterrevisions-Wechsel macht alte Zuordnungen sichtbar ``invalidated``
**ohne** Auto-Transfer.

Konfidenz trägt einen sichtbaren Kalibrierungsstatus — solange das
Kalibrierungsprotokoll mit dem Referenzkorpus nicht eingefroren ist, gilt
``konfidenz_unkalibriert`` (keine automatische Namensübernahme).
"""
from __future__ import annotations

import uuid

NAME_MAPPING_CONTRACT_VERSION = "name_mapping_v1"

STATE_NEUTRAL = "neutral"
STATE_SUGGESTED = "suggested"
STATE_CONFIRMED = "confirmed"
STATE_MANUAL = "manual"
STATE_CONFLICT = "conflict"
STATE_REJECTED = "rejected"
STATE_INVALIDATED = "invalidated"

STATUS_PROPOSED = "vorgeschlagen"
CALIBRATION_UNCALIBRATED = "konfidenz_unkalibriert"

REQUIRED_EVIDENCE_KEYS = ("track_id", "turn_id", "word_ids", "span_100ns")


def _revision(base: dict, *, state: str, **overrides) -> dict:
    rev = dict(base)
    rev.update(
        {
            "contract_version": NAME_MAPPING_CONTRACT_VERSION,
            "mapping_id": uuid.uuid4().hex,
            "state": state,
            "status": STATUS_PROPOSED if state == STATE_SUGGESTED else state,
        }
    )
    rev.update(overrides)
    return rev


def suggest(
    *,
    cluster_id: str,
    name_candidate: str,
    evidence: list[dict],
    confidence: float,
    rule_id: str,
    cluster_revision: str,
    existing: list[dict] | None = None,
    min_confidence: float = 0.8,
) -> dict:
    """Belegter Namensvorschlag — Kandidat **unverändert**, Status ``vorgeschlagen``.

    Mehrdeutige Evidenz (mehrere Cluster mit demselben Namen, widersprüchliche
    Namen eines Clusters) oder Konfidenz unter der Schwelle erzeugen
    ``conflict`` statt Übernahme.
    """
    for item in evidence:
        missing = [k for k in REQUIRED_EVIDENCE_KEYS if k not in item]
        if missing:
            raise ValueError(f"evidenz_unvollstaendig: {missing}")

    conflict = confidence < min_confidence
    for prior in existing or []:
        if prior.get("state") not in (STATE_SUGGESTED, STATE_CONFIRMED, STATE_MANUAL):
            continue
        if prior.get("cluster_id") != cluster_id and prior.get("name_candidate") == name_candidate:
            conflict = True
        if prior.get("cluster_id") == cluster_id and prior.get("name_candidate") != name_candidate:
            conflict = True

    return _revision(
        {
            "mapping_id": "",
            "cluster_id": cluster_id,
            "cluster_revision": cluster_revision,
            "name_candidate": name_candidate,
            "display_name": None,
            "evidence": [dict(e) for e in evidence],
            "confidence": max(0.0, min(1.0, float(confidence))),
            "calibration": CALIBRATION_UNCALIBRATED,
            "rule_id": rule_id,
            "origin": "regelbasiert",
            "parent_revision": None,
            "user_action": None,
            "transferred_to": None,
        },
        state=STATE_CONFLICT if conflict else STATE_SUGGESTED,
    )


def confirm(mapping: dict, *, user: str) -> dict:
    """Nutzer bestätigt einen Vorschlag ausdrücklich → neue Revision."""
    if mapping.get("state") != STATE_SUGGESTED:
        raise ValueError("nur_vorschlaege_koennen_bestaetigt_werden")
    return _revision(
        mapping,
        state=STATE_CONFIRMED,
        display_name=mapping["name_candidate"],
        parent_revision=mapping["mapping_id"],
        user_action="bestaetigt",
        origin="vorschlag_bestaetigt",
    )


def set_manual(
    *, cluster_id: str, display_name: str, user: str, cluster_revision: str
) -> dict:
    """Nutzer setzt einen Namen selbst — Herkunft ``manuell``, jobinterne Gültigkeit."""
    return _revision(
        {
            "mapping_id": "",
            "cluster_id": cluster_id,
            "cluster_revision": cluster_revision,
            "name_candidate": display_name,
            "display_name": display_name,
            "evidence": [],
            "confidence": 1.0,
            "calibration": CALIBRATION_UNCALIBRATED,
            "rule_id": "manuell",
            "origin": "manuell",
            "parent_revision": None,
            "user_action": "manuell_gesetzt",
            "transferred_to": None,
        },
        state=STATE_MANUAL,
    )


def reject(mapping: dict, *, user: str) -> dict:
    """Vorschlag verwerfen — neutral bleiben, verworfener Vorschlag nie still erneut."""
    return _revision(
        mapping,
        state=STATE_REJECTED,
        display_name=None,
        parent_revision=mapping["mapping_id"],
        user_action="verworfen",
    )


def reset_to_neutral(mapping: dict, *, user: str) -> dict:
    """Auf neutrales Cluster zurücksetzen — reversibel, Historie bleibt."""
    return _revision(
        mapping,
        state=STATE_NEUTRAL,
        display_name=None,
        parent_revision=mapping["mapping_id"],
        user_action="zurueckgesetzt",
    )


def invalidate_for_cluster_revision(mapping: dict, *, new_cluster_revision: str) -> dict:
    """Cluster geteilt/vereinigt/ersetzt → alte Zuordnung sichtbar ungültig.

    Niemals automatisch auf andere Cluster übertragen (``transferred_to`` bleibt
    ``None``)."""
    return _revision(
        mapping,
        state=STATE_INVALIDATED,
        cluster_revision=new_cluster_revision,
        parent_revision=mapping["mapping_id"],
        user_action=None,
        transferred_to=None,
    )


def authorized_display_name(revisions: list[dict]) -> str | None:
    """Aktuell autorisierter Anzeigename einer Clustervorstellungshistorie.

    ``None`` = neutral (kein Name autorisiert). Vorschläge allein setzen nie
    einen Namen; spätere Zurücksetzungen/Verwerfungen/Invalidierungen gewinnen.
    """
    current: str | None = None
    for rev in revisions:
        state = rev.get("state")
        if state in (STATE_CONFIRMED, STATE_MANUAL):
            current = rev.get("display_name")
        elif state in (STATE_NEUTRAL, STATE_REJECTED, STATE_INVALIDATED, STATE_CONFLICT):
            current = None
    return current
