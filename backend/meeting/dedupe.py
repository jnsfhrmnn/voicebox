"""JFW-11: Konservative Eigenstimmen-Deduplizierung (I/O-frei).

Spec „Deduplizierungsvertrag": Deduplizierung ist eine revisionsgebundene
Annotation auf unveränderten JFW-2-/JFW-3-Ergebnissen. ``duplikat_bestaetigt``
nur bei freigegebener Sicherheit der Quellenäquivalenz; **unsichere Fälle
entfernen nie Inhalt** (``dedupe_unsicher``); die Remote-Spur wird nie allein
wegen Mikrofonaktivität unterdrückt (Fremdstimmenerhalt-Hartregel).

Methode (Architektur-Entscheidung 2026-09-23): regelbasiert-konservativ über
Rollenlogik + Wellenform-Kreuzkorrelation der betroffenen Fenster auf der
gemeinsamen Zeitbasis — **kein** neuronales Voice-Embedding (Spec schließt
wiederverwendbare Sprechermerkmale aus). Entscheidungsdatensätze sind inhaltsfrei
(IDs/Hashes/Status/Konfidenz/Provenienz, nie Text oder Audio).
"""
from __future__ import annotations

from ..minutes.provenance import canonical_hash

DEDUPE_CONTRACT_VERSION = "dedupe_annotation_v1"

UNRATED = "unbewertet"
KEPT_SEPARATE = "getrennt_behalten"
CONFIRMED = "duplikat_bestaetigt"
UNCERTAIN = "dedupe_unsicher"
USER_RESOLVED = "nutzeraufgeloest"
INVALIDATED = "invalidiert"

DEFAULT_THRESHOLD = 0.9
DEFAULT_GRAY = 0.5

EVIDENCE_EQUIVALENT = "quellenaequivalenz_belegt"
EVIDENCE_UNCERTAIN = "quellenaequivalenz_unsicher"
EVIDENCE_NONE = "keine_aequivalenz"


def evaluate_pair(
    *,
    corr: float,
    same_speaker_candidate: bool,
    foreign_overlap: bool,
    threshold: float = DEFAULT_THRESHOLD,
    gray: float = DEFAULT_GRAY,
) -> str:
    """Konservative Regelauswertung für ein Quellenpaar (zwei Repräsentationen).

    * ``foreign_overlap``: im selben Zeitfenster spricht nachweislich eine
      fremde Stimme mit → nie deduplizieren (Fremdstimmenerhalt).
    * ``same_speaker_candidate``: das Paar ist Kandidat für dieselbe Äußerung
      (z. B. eigene Stimme: Mikrofon primär + verzögertes Remote-Echo).
    * ``corr``: Kreuzkorrelationswert der Fenster auf der gemeinsamen Zeitbasis.
    """
    if foreign_overlap or not same_speaker_candidate:
        return KEPT_SEPARATE
    if corr >= threshold:
        return CONFIRMED
    if corr >= gray:
        return UNCERTAIN
    return KEPT_SEPARATE


def remote_suppression_forbidden(*, mic_active: bool, foreign_overlap: bool) -> bool:
    """Hartregel (Spec-Produktentscheidung): pauschales Stummschalten, Absenken
    oder Verwerfen der Remote-Spur wegen Mikrofonaktivität ist **immer**
    verboten — auch ohne sichtbares Übersprechen."""
    del mic_active, foreign_overlap
    return True


def _evidence_class(state: str) -> str:
    if state == CONFIRMED:
        return EVIDENCE_EQUIVALENT
    if state == UNCERTAIN:
        return EVIDENCE_UNCERTAIN
    return EVIDENCE_NONE


def make_decision(
    *,
    ref_a: dict,
    ref_b: dict,
    state: str,
    corr: float,
    rule_id: str,
    parent_revision: str | None = None,
    user_action: str | None = None,
) -> dict:
    """Inhaltsfreier, revisionsgebundener Entscheidungsdatensatz."""
    decision = {
        "contract_version": DEDUPE_CONTRACT_VERSION,
        "ref_a": dict(ref_a),
        "ref_b": dict(ref_b),
        "state": state,
        "confidence": max(0.0, min(1.0, abs(float(corr)))),
        "evidence_class": _evidence_class(state),
        "rule_id": rule_id,
        "parent_revision": parent_revision,
        "user_action": user_action,
    }
    # USCRX-2026-16006 (RL-06): decision_id inhaltsdeterministisch (Prefix + Hash,
    # Vorbild minutes/pseudonym.py) — Byte-Regel JFW-4: identische Eingaben
    # muessen identische Bytes ergeben; uuid4 brach Wiederholungen.
    decision["decision_id"] = "dec_" + canonical_hash(decision)[:24]
    return decision


def build_combined_contributions(contributions: list[dict]) -> list[dict]:
    """Bildet die kombinierte Darstellung ohne Inhaltsverlust.

    Nur ``duplikat_bestaetigt``-Paare derselben Entscheidung erscheinen **einmal**
    (mit beiden Quellenreferenzen); ``dedupe_unsicher`` und
    ``getrennt_behalten`` bleiben vollständig als getrennte Beiträge erhalten.
    """
    groups: dict[str, list[dict]] = {}
    standalone: list[dict] = []
    for contribution in contributions:
        if contribution.get("decision_state") == CONFIRMED and contribution.get("decision_id"):
            groups.setdefault(contribution["decision_id"], []).append(contribution)
        else:
            standalone.append(contribution)

    combined: list[dict] = []
    for decision_id, members in groups.items():
        combined.append(
            {
                "decision_id": decision_id,
                "dedupe_state": CONFIRMED,
                "canonical_repr_id": members[0]["repr_id"],
                "source_references": [m["ref"] for m in members],
            }
        )
    for contribution in standalone:
        combined.append(
            {
                "decision_id": contribution.get("decision_id"),
                "dedupe_state": contribution["decision_state"],
                "canonical_repr_id": contribution["repr_id"],
                "source_references": [contribution["ref"]],
            }
        )
    return combined
