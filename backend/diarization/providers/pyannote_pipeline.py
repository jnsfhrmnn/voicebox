"""JFW-3: pyannote.audio-Adapter (V1, Zielsystem-Seam, strikt lokal).

Lokale Pipeline ``pyannote/speaker-diarization-community-1`` (CC-BY-4.0,
gated; Beleg: features/evidence/JFW-3-modell-lizenz-entscheidung.md). Der
Adapter ist die einzige Stelle mit ML-Laufzeitbezug und bleibt strikt lokal:
``pyannote.audio`` wird lazy importiert, Artefakte werden ausschliesslich aus
dem lokalen Installationsverzeichnis geladen (nutzerinitiierte Installation,
``diarization-artifact.json``), es gibt keinen Netzwerk- oder Cloud-Fallback.

``SpeakerSpec`` wird auf die Pipeline-Parameter abgebildet:
``exact`` -> ``num_speakers``, ``range`` -> ``min_speakers``/``max_speakers``,
``auto`` -> keine Vorgabe. Langleiodige Dateien werden fensterweise verarbeitet
(Emissions-/Embedding-Schritt); JFW-2 und JFW-3 laufen sequenziell und geben
ihre Modelle nach dem Lauf frei (OOM-Sicherheit).

Ohne installiertes ``pyannote.audio`` oder ohne lokales Artefakt endet der Lauf
fail-closed mit ``provider_runtime_missing`` beziehungsweise
``artifact_missing`` — die Ergebniszustandslogik zeigt
``waiting_for_local_artifact``.
"""
from __future__ import annotations

from pathlib import Path

from ..artifacts import ArtifactGateError

MODEL_ID = "pyannote/speaker-diarization-community-1"
PIPELINE_NAME = "pyannote/speaker-diarization-community-1"


def speaker_params(speaker_spec) -> dict:
    """Übersetzt die Sprecheranzahlvorgabe exakt auf Pipeline-Argumente:
    ``exact`` -> ``num_speakers``, ``range`` -> ``min_speakers``/``max_speakers``,
    ``auto`` -> keine Vorgabe. Rein und ohne ML-Laufzeit testbar."""
    params: dict = {}
    if getattr(speaker_spec, "mode", "auto") == "exact":
        params["num_speakers"] = speaker_spec.count
    elif getattr(speaker_spec, "mode", "auto") == "range":
        params["min_speakers"] = speaker_spec.minimum
        params["max_speakers"] = speaker_spec.maximum
    return params


def make_provider(*, audio_path: str, artifact_dir: Path | None = None):
    """Baut den injizierbaren Provider ``provider(duration_ms, speaker_spec) ->
    [raw_turn]`` fuer die Engine; laedt die Pipeline erst beim Aufruf."""

    def provider(duration_ms: float, speaker_spec):
        try:
            from pyannote.audio import Pipeline
        except ImportError as exc:  # fehlende Runtime ist ein Artefakt-/Laufzeitmangel
            raise ArtifactGateError("provider_runtime_missing") from exc

        base = Path(artifact_dir) if artifact_dir is not None else None
        if base is None or not base.is_dir():
            raise ArtifactGateError("artifact_missing")

        # Lokales Laden ohne Netzwerk: ausschliesslich das verifizierte
        # Installationsverzeichnis (Manifest-Gate hat Revision/Hashes belegt).
        try:
            pipeline = Pipeline.from_pretrained(
                str(base), token=None  # nie ein HF-Token, nie ein Download
            )
        except Exception as exc:
            raise ArtifactGateError("artifact_missing") from exc

        params: dict = speaker_params(speaker_spec)

        try:
            diarization = pipeline(str(audio_path), **params)
            raw_turns = []
            for turn, _track, speaker in diarization.itertracks(yield_label=True):
                raw_turns.append(
                    {
                        "start_ms": float(turn.start) * 1000.0,
                        "end_ms": float(turn.end) * 1000.0,
                        "speaker_key": str(speaker),
                        "score": None,
                        # Zeitliche Ueberschneidungen erkennt der Vertragskern
                        # selbst (Overlap-Gruppen); der Hint bleibt unvoreingenommen.
                        "overlap_hint": "unknown",
                    }
                )
        finally:
            # Modelle nach dem Lauf freigeben (sequenzieller Ressourcenwechsel).
            del pipeline
        return raw_turns

    return provider
