"""JFW-2: Modell-/Lizenz-Gate (fail-closed, JFW-2-Regel).

Kein Alignment-Modell wird ohne gueltiges lokales Installationsmanifest
geladen. Das Manifest bindet Repo, IMMMUTABLE Revision (40-Hex), Datei-SHA-256
je Artefakt, Lizenz-ID, Installationsweg und das ausdrueckliche Bekenntnis zur
persoenlichen (nicht kommerziellen) Nutzung. Unklare oder unvereinbare Lizenz
blockiert Bündelung und Release.

Registry-Stand 2026-09-23 (Beleg: features/evidence/JFW-2-modell-lizenz-entscheidung.md):
``facebook/mms_fa`` (torchaudio ``MMS_FA``/``ctc_alignment_mling_uroman``) ist
CC-BY-NC-4.0 — persoenliche Nutzung zulaessig, Bündelung und Distribution des
Modells NICHT zulaessig (``bundle_allowed = False``).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "alignment-artifact.json"
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ArtifactSpec:
    model_id: str
    repo_url: str
    license_id: str
    personal_use_allowed: bool
    commercial_use_allowed: bool
    bundle_allowed: bool
    distribution_allowed: bool


ALIGNMENT_ARTIFACTS: dict[str, ArtifactSpec] = {
    "facebook/mms_fa": ArtifactSpec(
        model_id="facebook/mms_fa",
        repo_url="https://huggingface.co/facebook/mms_fa",
        license_id="CC-BY-NC-4.0",
        personal_use_allowed=True,
        commercial_use_allowed=False,
        bundle_allowed=False,
        distribution_allowed=False,
    ),
}


class ArtifactGateError(RuntimeError):
    """Fail-closed Artefakt-/Lizenzfehler mit inhaltsfreiem Grundcode."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_artifact(model_id: str, base_dir: Path) -> dict:
    """Prueft eine lokale Modellinstallation fail-closed und liefert die
    Provenienz fuer den Ergebniskopf (``model_id``/``model_revision``/
    ``model_sha256``/``model_license``)."""
    spec = ALIGNMENT_ARTIFACTS.get(model_id)
    if spec is None:
        raise ArtifactGateError("artifact_missing")

    manifest_path = Path(base_dir) / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ArtifactGateError("waiting_for_local_artifact")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactGateError("waiting_for_local_artifact") from exc

    if manifest.get("model_id") != model_id or manifest.get("repo") != spec.repo_url:
        raise ArtifactGateError("artifact_provenance_mismatch")
    revision = str(manifest.get("revision", ""))
    if not _REVISION_RE.match(revision):
        raise ArtifactGateError("artifact_provenance_mismatch")
    if manifest.get("license_id") != spec.license_id:
        raise ArtifactGateError("license_unapproved")
    if not spec.personal_use_allowed or manifest.get("personal_use_ack") is not True:
        raise ArtifactGateError("license_ack_missing")
    if not manifest.get("install_source"):
        raise ArtifactGateError("artifact_provenance_mismatch")

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ArtifactGateError("artifact_provenance_mismatch")
    digest = hashlib.sha256()
    for rel in sorted(files):
        fpath = Path(base_dir) / rel
        if not fpath.is_file():
            raise ArtifactGateError("artifact_hash_mismatch")
        actual = _sha256_file(fpath)
        if actual != files[rel]:
            raise ArtifactGateError("artifact_hash_mismatch")
        digest.update(f"{rel}:{actual}\n".encode("utf-8"))

    return {
        "model_id": model_id,
        "model_revision": revision,
        "model_sha256": digest.hexdigest(),
        "model_license": spec.license_id,
    }


def check_bundle_policy(spec: ArtifactSpec, packaged_files: list[str]) -> None:
    """Erzwingt die Bündelungssperre: kein Modellgewicht im Release-Inventar."""
    for entry in packaged_files:
        normalized = entry.replace("\\", "/")
        if normalized.startswith("models/alignment") or "/models/alignment" in normalized:
            if not spec.bundle_allowed:
                raise ArtifactGateError("model_bundling_forbidden")
