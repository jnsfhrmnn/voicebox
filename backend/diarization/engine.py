"""JFW-3: Orchestrierung eines Diarisierungslaufs (injizierbarer Provider).

``run_diarization`` treibt die Schritte Submit → Artefakt-Gate → Provider →
Ergebnisvalidierung → atomarer Commit. Der Provider ist eine injizierbare
Funktion ``provider(duration_ms, speaker_spec) -> [raw_turn]``; das
Artefakt-Gate ist ein injizierbarer Callable, der die Modellprovenienz liefert
oder :class:`ArtifactGateError` wirft. Damit ist die gesamte Ablauflogik ohne
Modell, Audio und Netzwerk unit-testbar; der echte Provider
(``providers.pyannote_pipeline``) ist die Zielsystem-Seam.

Fail-closed-Verhalten: fehlendes/korruptes Artefakt oder fehlende Runtime ->
``waiting_for_local_artifact`` ohne Netzwerkzugriff; Provider-Ausnahme ->
``failed`` OHNE Ergebnis-Commit (identischer Retry darf neu rechnen); jede
Mutation der JFW-2-Wortliste -> ``text_or_boundary_change_detected`` ohne
Commit (heiliger Unveränderlichkeits-Vertrag).
"""
from __future__ import annotations

from ..services import diarization_contract as store
from .artifacts import ArtifactGateError
from .contract import build_result, verify_words_unchanged
from .provenance import DiarizationRequest


def run_diarization(
    session,
    request: DiarizationRequest,
    text: str,
    words: list[dict] | None,
    *,
    provider,
    artifact_gate,
    app_epoch: str,
    provider_name: str = "custom",
) -> dict:
    identity_hash = request.identity_hash()
    sub = store.submit_diarization(session, request, text)

    if sub["outcome"] == "revision_hash_mismatch":
        return {
            "outcome": "revision_hash_mismatch",
            "identity_hash": identity_hash,
            "status": None,
            "result_hash": None,
        }
    if sub["outcome"] == "conflict":
        # Fail-closed: vorhandenes Ergebnis bleibt unangetastet.
        return {
            "outcome": "conflict",
            "identity_hash": identity_hash,
            "status": sub["status"],
            "result_hash": sub["result_hash"],
        }
    if sub["outcome"] == "existing":
        recomputable = sub["result_hash"] is None and sub["status"] in (
            "queued",
            "waiting_for_local_artifact",
            "failed",
            "canceled",
        )
        if not recomputable:
            # Idempotenz: existierendes versioniertes Ergebnis wird zurueckgegeben.
            return {
                "outcome": "existing",
                "identity_hash": identity_hash,
                "status": sub["status"],
                "result_hash": sub["result_hash"],
                "speaker_mode": sub.get("speaker_mode"),
            }

    attempt_id = store.begin_attempt(session, identity_hash, app_epoch)
    if attempt_id is None:
        return {
            "outcome": "already_running",
            "identity_hash": identity_hash,
            "status": "diarizing",
            "result_hash": None,
        }

    try:
        provenance = artifact_gate()
    except ArtifactGateError as exc:
        store.mark_waiting_for_artifact(session, identity_hash, exc.reason_code)
        return {
            "outcome": "waiting_for_local_artifact",
            "identity_hash": identity_hash,
            "status": "waiting_for_local_artifact",
            "result_hash": None,
        }

    base_words = words or []
    try:
        raw_turns = provider(request.audio_duration_ms, request.speaker_spec)
    except ArtifactGateError as exc:
        store.mark_waiting_for_artifact(session, identity_hash, exc.reason_code)
        return {
            "outcome": "waiting_for_local_artifact",
            "identity_hash": identity_hash,
            "status": "waiting_for_local_artifact",
            "result_hash": None,
        }
    except Exception:
        store.fail_attempt(session, identity_hash, "provider_error")
        return {
            "outcome": "failed",
            "identity_hash": identity_hash,
            "status": "failed",
            "result_hash": None,
        }

    try:
        built = build_result(
            base_words,
            raw_turns or [],
            duration_ms=request.audio_duration_ms,
            provider=provider_name,
            speaker_spec=request.speaker_spec.as_dict(),
        )
        verify_words_unchanged(base_words, built["words"], text)
    except ValueError:
        # Heiliger Unveränderlichkeits-Vertrag: nie wird mutierte Text-/
        # Grenzstruktur committet.
        store.fail_attempt(session, identity_hash, "text_or_boundary_change_detected")
        return {
            "outcome": "failed",
            "identity_hash": identity_hash,
            "status": "failed",
            "result_hash": None,
        }

    outcome = store.commit_result(session, identity_hash, built, provenance)
    committed = outcome == "committed"
    return {
        "outcome": outcome,
        "identity_hash": identity_hash,
        "status": built["status"] if committed else outcome,
        "result_hash": built["result_hash"] if committed else None,
        "coverage_speech_ms": built["coverage"]["speech_ms"] if committed else None,
        "coverage_usable_ms": built["coverage"]["usable_ms"] if committed else None,
        "cluster_count": built["counters"]["cluster_count"] if committed else None,
        "turn_count": built["counters"]["turn_count"] if committed else None,
        "overlap_count": built["counters"]["overlap_count"] if committed else None,
        "uncertainty_count": built["counters"]["uncertainty_count"] if committed else None,
        "speaker_mode": request.speaker_spec.mode,
        "model_id": provenance.get("model_id"),
    }
