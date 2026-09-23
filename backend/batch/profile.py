"""JFW-5: eingefrorene gemeinsame Profilrevision (I/O-frei).

Ein Batch besitzt GENAU EINE bestaetigte Profilrevision; jede spaetere
fachliche Aenderung erzeugt eine neue nachvollziehbare Revision statt eines
stillen Umschreibens. Fehlende/unbekannte Werte schliessen fail-closed ab.
"""
from __future__ import annotations

from .provenance import canonical_hash

#: Serielle Ressourcenstufe — ohne Zielsystem-Messnachweis die einzige erlaubte.
RESOURCE_POLICY_ID = "jfw5_serial_v1"

PARTIAL_FAILURE_POLICIES = ("mit_belegten_daten_fortfahren", "element_blockieren")
LANGUAGE_SETTINGS = ("auto", "de", "en")
SPEAKER_MODES = ("auto", "exact", "range")
CONFLICT_RULES = ("blockieren", "element_suffix")
OUTPUT_STRUCTURES = ("relative_source",)
DECODE_PROFILES = ("jfw7-transcribe-greedy-v1",)

REQUIRED_KEYS = (
    "language_setting", "stt_model", "model_revision", "decode_profile",
    "alignment_enabled", "alignment_profile",
    "diarization_enabled", "speaker_mode", "speaker_count",
    "speaker_min", "speaker_max", "diarization_profile",
    "export_enabled", "export_formats", "export_profile", "name_policy",
    "minutes_enabled", "minutes_profile",
    "partial_failure_policy", "fail_fast", "output_policy",
)


def resource_policy() -> dict:
    return {
        "policy_id": RESOURCE_POLICY_ID,
        "max_concurrent_ai_phases": 1,
        "max_workers": 1,
        "min_free_bytes": 0,
    }


def validate_profile(profile: dict) -> list[str]:
    """Fail-closed Profilpruefung; leere Liste = Profil vollstaendig gueltig."""
    errors: list[str] = []
    if not isinstance(profile, dict):
        return ["profil_kein_objekt"]
    for key in REQUIRED_KEYS:
        if key not in profile:
            errors.append(f"profilfeld_fehlt:{key}")
    if errors:
        return errors
    if profile["language_setting"] not in LANGUAGE_SETTINGS:
        errors.append("sprache_unbekannt")
    if profile["decode_profile"] not in DECODE_PROFILES:
        errors.append("decode_profil_unbekannt")
    if not str(profile["stt_model"] or ""):
        errors.append("stt_modell_fehlt")
    if not str(profile["model_revision"] or ""):
        errors.append("modellrevision_fehlt")
    if profile["partial_failure_policy"] not in PARTIAL_FAILURE_POLICIES:
        errors.append("teilfehlerpolitik_unbekannt")
    if profile["diarization_enabled"] and profile["speaker_mode"] not in SPEAKER_MODES:
        errors.append("sprecheranzahlmodus_unbekannt")
    # Profilstufen haengen an ihren Vertraegen: JFW-4 braucht JFW-2 UND JFW-3,
    # JFW-13 braucht das verlustfreie JFW-4-Dokument (fail-closed).
    if profile["export_enabled"] and not (profile["alignment_enabled"] and profile["diarization_enabled"]):
        errors.append("export_erfordert_alignment_und_diarisierung")
    if profile["minutes_enabled"] and not profile["export_enabled"]:
        errors.append("protokoll_erfordert_export")
    out = profile["output_policy"]
    if not isinstance(out, dict):
        errors.append("ausgabepolitik_fehlt")
    else:
        for key in ("target_root", "structure", "conflict_rule"):
            if key not in out:
                errors.append(f"ausgabepolitik_fehlt:{key}")
        if out.get("structure") not in OUTPUT_STRUCTURES:
            errors.append("ausgabestruktur_unbekannt")
        if out.get("conflict_rule") not in CONFLICT_RULES:
            errors.append("konfliktregel_unbekannt")
    return errors


def phases_for(profile: dict) -> tuple[str, ...]:
    """Bestaetigte Phasenreihenfolge: Basistranskription, JFW-2, JFW-3, JFW-4, JFW-13."""
    phases = ["transcribe"]
    if profile.get("alignment_enabled"):
        phases.append("align")
    if profile.get("diarization_enabled"):
        phases.append("diarize")
    if profile.get("export_enabled"):
        phases.append("export")
    if profile.get("minutes_enabled"):
        phases.append("minutes")
    return tuple(phases)


def profile_hash(profile: dict) -> str:
    return canonical_hash(dict(profile))


def change_reasons(old: dict, new: dict) -> list[str]:
    """Feldnamen, die zwischen zwei Profilen abweichen (leer = identisch)."""
    keys = set(old or {}) | set(new or {})
    return sorted(k for k in keys if (old or {}).get(k) != (new or {}).get(k))
