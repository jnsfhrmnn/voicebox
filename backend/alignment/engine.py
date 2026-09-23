"""JFW-2: Orchestrierung eines Alignment-Laufs (injizierbarer Provider).

``run_alignment`` treibt die Schritte Submit → Artefakt-Gate → Wortabbildung →
Provider → Ergebnisvalidierung → atomarer Commit. Der Provider ist eine
injizierbare Funktion ``provider(words, duration_ms, language_ranges) ->
{word_id: (start_ms, end_ms, score) | None}``; das Artefakt-Gate ist ein
injizierbarer Callable, der die Modellprovenienz liefert oder
:class:`ArtifactGateError` wirft. Damit ist die gesamte Ablauflogik ohne
Modell, Audio und Netzwerk unit-testbar; der echte Provider
(``providers.mms_fa``) ist die Zielsystem-Seam.

Fail-closed-Verhalten: fehlendes/korruptes Artefakt oder fehlende Runtime ->
``waiting_for_local_artifact`` ohne Netzwerkzugriff; Provider-Ausnahme ->
``failed`` OHNE Ergebnis-Commit (identischer Retry darf neu rechnen).
"""
from __future__ import annotations

from .artifacts import ArtifactGateError
from .contract import build_result
from .provenance import AlignmentRequest
from .text_map import build_word_contract
from ..services import alignment_contract as store

#: Frameaufloessung des V1-Providers (MMS_FA: 320 Samples @ 16 kHz = 20 ms).
PROVIDER_FRAME_MS = 20.0


def run_alignment(
    session,
    request: AlignmentRequest,
    text: str,
    *,
    provider,
    artifact_gate,
    app_epoch: str,
    provider_name: str = "custom",
) -> dict:
    identity_hash = request.identity_hash()
    sub = store.submit_alignment(session, request, text)

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
            }

    attempt_id = store.begin_attempt(session, identity_hash, app_epoch)
    if attempt_id is None:
        return {
            "outcome": "already_running",
            "identity_hash": identity_hash,
            "status": "aligning",
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

    words = build_word_contract(text)
    try:
        assignments = provider(words, request.audio_duration_ms, request.language_ranges)
    except ArtifactGateError as exc:
        store.mark_waiting_for_artifact(session, identity_hash, exc.reason_code)
        return {
            "outcome": "waiting_for_local_artifact",
            "identity_hash": identity_hash,
            "status": "waiting_for_local_artifact",
            "result_hash": None,
        }
    except Exception:  # noqa: BLE001 -- Provider-Ausnahme ist ein Lauffehler
        store.fail_attempt(session, identity_hash, "provider_error")
        return {
            "outcome": "failed",
            "identity_hash": identity_hash,
            "status": "failed",
            "result_hash": None,
        }

    try:
        built = build_result(
            text,
            words,
            assignments or {},
            duration_ms=request.audio_duration_ms,
            provider=provider_name,
            frame_ms=PROVIDER_FRAME_MS,
        )
    except ValueError:
        # Heiliges No-text-change-Gate: nie wird mutierter Text committet.
        store.fail_attempt(session, identity_hash, "text_change_detected")
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
        "coverage_alignable": built["coverage_alignable"] if committed else None,
        "coverage_aligned": built["coverage_aligned"] if committed else None,
        "model_id": provenance.get("model_id"),
    }
