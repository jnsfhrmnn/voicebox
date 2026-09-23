"""JFW-7: Modell-/Lizenz-Gate (fail-closed, I/O-frei).

JFW-7-Regel (jfw-fork-block „Model- und Lizenz-Bindung"): Herkunft, Lizenz und
Distributionsrecht werden belegt ODER die Nutzung wird abgelehnt. Unbekannte oder
fehlende Lizenz **blockiert** — der Zustand „unbekannt" erlaubt nie die Nutzung.

Beleg: ``features/evidence/JFW-7-modell-lizenz-entscheidung.md`` (HF-Modellkarten,
2026-09-23): ``openai/whisper-large-v3-turbo`` = MIT (Base-Linie large-v3 =
Apache-2.0, upstream-bekannte Inkonsistenz → kumulative Pflichten); alle
Register-Repos tragen ``mit`` oder ``apache-2.0``.
"""
from __future__ import annotations

import re

MODEL_GATE_VERSION = "jfw7-model-gate-v1"

#: Bekannte, geprüfte Lizenzen mit Distributionspflichten.
KNOWN_LICENSES = {
    "mit": ["copyright_hinweis", "lizenztext_mitliefern"],
    "apache-2.0": ["lizenzkopie_mitliefern", "notice_mitliefern", "aenderungen_kennzeichnen"],
}

#: Freigegebene Register-Repos mit erwarteter Lizenz-ID (fail-closed).
ALLOWED_MODEL_REPOS = {
    "openai/whisper-base": "apache-2.0",
    "openai/whisper-small": "apache-2.0",
    "openai/whisper-medium": "apache-2.0",
    "openai/whisper-large-v3": "apache-2.0",
    "openai/whisper-large-v3-turbo": "mit",
}

#: Pflichtartefakte eines Installationsmanifests.
REQUIRED_ARTIFACT_FILES = ("model.safetensors", "config.json")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


class ModellGateError(RuntimeError):
    """Fail-closed: Modell-/Lizenzvertrag nicht belegt."""


def check_model_manifest(manifest: dict) -> list[str]:
    """Fail-closed Prüfung; leere Liste = Manifest belegt und zulässig."""
    errors: list[str] = []
    manifest = manifest or {}
    repo = manifest.get("repo")
    if repo not in ALLOWED_MODEL_REPOS:
        errors.append("repo_nicht_freigegeben")
    rev = manifest.get("model_revision")
    if not isinstance(rev, str) or not _HEX40.match(rev):
        errors.append("modellrevision_unzulaessig")
    files = manifest.get("files") or {}
    for name in REQUIRED_ARTIFACT_FILES:
        if name not in files:
            errors.append(f"datei_hash_fehlt:{name}")
    for name, digest in files.items():
        if not isinstance(digest, str) or not _HEX64.match(digest):
            errors.append(f"datei_hash_unzulaessig:{name}")
    lic = manifest.get("license_id")
    if lic not in KNOWN_LICENSES:
        # Unbekannte oder fehlende Lizenz blockiert (nie „erlaubt“).
        errors.append("lizenz_unbekannt")
    elif repo in ALLOWED_MODEL_REPOS and lic != ALLOWED_MODEL_REPOS[repo]:
        errors.append("lizenz_widerspricht_register")
    if not manifest.get("install_path"):
        errors.append("installationspfad_fehlt")
    return errors


def manifest_ok(manifest: dict) -> bool:
    return not check_model_manifest(manifest)


def bundle_policy(license_id: str) -> dict:
    """Distributionsrecht inklusive Pflichten; unbekannt = fail-closed gesperrt."""
    known = license_id in KNOWN_LICENSES
    return {
        "license_id": license_id,
        "known": known,
        "distribution_allowed": known,
        "obligations": list(KNOWN_LICENSES.get(license_id, ())),
    }


def check_download_request(
    *, repo: str, size_mb: int, source: str, target: str, user_acknowledged: bool
) -> dict:
    """Getrennter, bewusster Modell-Download mit Größe, Quelle und Ziel.

    Kein automatischer Netzwerkzugriff: ohne ausdrückliche Bestätigung wird nicht
    heruntergeladen; die Anzeige nennt Größe, Quelle und Ziel.
    """
    errors: list[str] = []
    if repo not in ALLOWED_MODEL_REPOS:
        errors.append("modell_nicht_freigegeben")
    if not user_acknowledged:
        errors.append("download_nicht_bestaetigt")
    try:
        if int(size_mb) <= 0:
            errors.append("groesse_unbekannt")
    except (TypeError, ValueError):
        errors.append("groesse_unbekannt")
    if not source:
        errors.append("quelle_fehlt")
    if not target:
        errors.append("ziel_fehlt")
    return {
        "ok": not errors,
        "errors": errors,
        "display": {"size_mb": size_mb, "source": source, "target": target},
    }
