"""JFW-7: Modell-/Lizenz-Gate (fail-closed) — Vertragstests (TDD, RED zuerst).

Spec AC „Fehler, Retry und Recovery": kein automatischer Netzwerkzugriff bei fehlendem
Modell; getrennter Download mit Größe, Quelle und Ziel. JFW-7-Regel (fail-closed):
Herkunft/Lizenz/Distributionsrecht belegen ODER ablehnen — unbekannte Lizenz blockiert.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_transcription_artifacts.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.transcription.artifacts import (
    ALLOWED_MODEL_REPOS,
    KNOWN_LICENSES,
    bundle_policy,
    check_download_request,
    check_model_manifest,
)


def _manifest(**overrides) -> dict:
    base = dict(
        repo="openai/whisper-large-v3-turbo",
        model_revision="1" * 40,
        files={"model.safetensors": "c" * 64, "config.json": "d" * 64},
        license_id="mit",
        install_path="C:/Users/x/AppData/Local/JFWhisper/models/whisper-large-v3-turbo",
    )
    base.update(overrides)
    return base


def test_gueltiges_manifest_passiert_das_gate():
    assert check_model_manifest(_manifest()) == []


def test_nur_freigegebene_register_repos():
    errors = check_model_manifest(_manifest(repo="evil/not-whisper"))
    assert "repo_nicht_freigegeben" in errors


def test_immutable_revision_und_dateihashes_verpflichtend():
    assert "modellrevision_unzulaessig" in check_model_manifest(_manifest(model_revision="main"))
    assert "datei_hash_unzulaessig:model.safetensors" in check_model_manifest(
        _manifest(files={"model.safetensors": "xyz"})
    )
    assert "datei_hash_fehlt:config.json" in check_model_manifest(
        _manifest(files={"model.safetensors": "c" * 64})
    )


def test_unbekannte_oder_fehlende_lizenz_blockiert_fail_closed():
    assert "lizenz_unbekannt" in check_model_manifest(_manifest(license_id="cc-by-nc-4.0"))
    assert "lizenz_unbekannt" in check_model_manifest(_manifest(license_id=None))
    assert "lizenz_widerspricht_register" in check_model_manifest(_manifest(license_id="apache-2.0"))


def test_installationspfad_verpflichtend():
    assert "installationspfad_fehlt" in check_model_manifest(_manifest(install_path=""))


def test_register_traegt_nur_permissive_lizenzen():
    for repo, lic in ALLOWED_MODEL_REPOS.items():
        assert lic in KNOWN_LICENSES
        assert repo.startswith("openai/whisper-")


def test_bundling_policy_belegt_distributionsrecht():
    mit = bundle_policy("mit")
    assert mit["distribution_allowed"] is True
    assert "copyright_hinweis" in mit["obligations"]
    apa = bundle_policy("apache-2.0")
    assert apa["distribution_allowed"] is True
    assert "notice_mitliefern" in apa["obligations"]
    unknown = bundle_policy("cc-by-nc-4.0")
    assert unknown["distribution_allowed"] is False  # fail-closed


def test_download_nur_bewusst_mit_groesse_quelle_ziel():
    ok = check_download_request(
        repo="openai/whisper-large-v3-turbo",
        size_mb=1624,
        source="https://huggingface.co/openai/whisper-large-v3-turbo",
        target="C:/Users/x/AppData/Local/JFWhisper/models",
        user_acknowledged=True,
    )
    assert ok["ok"] is True
    assert ok["display"]["size_mb"] == 1624
    assert ok["display"]["source"].startswith("https://")
    assert ok["display"]["target"]

    denied = check_download_request(
        repo="openai/whisper-large-v3-turbo",
        size_mb=1624,
        source="https://huggingface.co/openai/whisper-large-v3-turbo",
        target="C:/Users/x/AppData/Local/JFWhisper/models",
        user_acknowledged=False,
    )
    assert denied["ok"] is False
    assert "download_nicht_bestaetigt" in denied["errors"]

    bad = check_download_request(
        repo="evil/model", size_mb=0, source="x", target="", user_acknowledged=True
    )
    assert "modell_nicht_freigegeben" in bad["errors"]
    assert "groesse_unbekannt" in bad["errors"]
    assert "ziel_fehlt" in bad["errors"]
