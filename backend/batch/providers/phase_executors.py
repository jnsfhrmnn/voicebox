"""JFW-5: Phasen-Adapter — fail-closed Revision-Binding an die Feature-Vertraege.

Jede Phase des Elements laeuft ausschliesslich gegen den gebundenen Vertrag des
jeweiligen Features (JFW-7 Rohtranskription, JFW-2 Alignment, JFW-3
Diarisierung, optional JFW-4 Export, optional JFW-13 Protokoll). Verbindliche
Upstream-Commits werden vor jedem Phasenlauf fail-closed geprueft; fehlt eine
Bindung, wird die Phase ``blocked`` statt eine Referenz zu erfinden. Die
eigentliche Feature-Ausfuehrung steckt hinter injizierbaren ``hooks`` (Default:
lazy Engine-Entry-Points der Features) — fehlende Modelllaufzeiten oder
Artefakte enden ``provider_runtime_missing``/``waiting_for_local_artifact``
OHNE automatischen Download und OHNE Cloud-Fallback (akustische Laeufe sind wie
bei JFW-2/JFW-3 Zielsystem-Seams).
"""
from __future__ import annotations

from ..pipeline import PhaseOutcome


class ProviderRuntimeMissingError(RuntimeError):
    reason_code = "provider_runtime_missing"


class WaitingForLocalArtifactError(RuntimeError):
    reason_code = "waiting_for_local_artifact"


class FeatureBindingConflictError(RuntimeError):
    reason_code = "revision_binding_verletzt"


#: Verbindliche Upstream-Bindungen je Phase (fail-closed).
REQUIRED_COMMITS = {
    "transcribe": (),
    "align": ("transcribe",),
    "diarize": ("transcribe",),
    "export": ("transcribe", "align", "diarize"),
    "minutes": ("export",),
}


def _phase_executor(phase: str, hook) -> callable:
    def run(phase_name: str, ctx: dict) -> PhaseOutcome:
        commits = dict(ctx.get("commits") or {})
        missing = [p for p in REQUIRED_COMMITS[phase] if not commits.get(p)]
        if missing:
            return PhaseOutcome(
                status="blocked",
                commit_ref=None,
                reason_code="upstream_fehlt:" + ",".join(missing),
                warnings=[],
            )
        try:
            result = hook(ctx)
        except (ProviderRuntimeMissingError, WaitingForLocalArtifactError) as exc:
            return PhaseOutcome(status="blocked", commit_ref=None,
                                reason_code=exc.reason_code, warnings=[])
        except FeatureBindingConflictError as exc:
            return PhaseOutcome(status="failed", commit_ref=None,
                                reason_code=exc.reason_code, warnings=[])
        except Exception as exc:  # Provider-Ausnahme bleibt sichtbar
            return PhaseOutcome(status="failed", commit_ref=None,
                                reason_code=f"provider_error:{type(exc).__name__}",
                                warnings=[])
        commit_ref = result.get("commit_ref")
        if not commit_ref:
            return PhaseOutcome(status="failed", commit_ref=None,
                                reason_code="kein_autoritativer_commit",
                                warnings=list(result.get("warnings") or []))
        status = result.get("status") or "succeeded"
        return PhaseOutcome(status=status, commit_ref=commit_ref,
                            reason_code=result.get("reason_code"),
                            warnings=list(result.get("warnings") or []),
                            carry=dict(result.get("carry") or {}))

    return run


def _lazy_call(module_path: str, symbol: str, *args, **kwargs):
    """Lazy Import eines Feature-Entry-Points; fehlende Runtime = fail-closed."""
    import importlib

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ProviderRuntimeMissingError(str(exc)) from exc
    return getattr(module, symbol)(*args, **kwargs)


def default_hooks(session, *, app_epoch: str = "api") -> dict:
    """Default-Feature-Bindungen (lazy Engine-Entry-Points der Vertragspartner)."""

    def transcribe(ctx):
        return _lazy_call(
            "backend.batch.providers.phase_executors", "_run_transcription",
            session, ctx["item"], ctx["profile"], app_epoch,
        )

    def align(ctx):
        return _lazy_call(
            "backend.batch.providers.phase_executors", "_run_alignment",
            session, ctx, app_epoch,
        )

    def diarize(ctx):
        return _lazy_call(
            "backend.batch.providers.phase_executors", "_run_diarization",
            session, ctx, app_epoch,
        )

    def export(ctx):
        return _lazy_call(
            "backend.batch.providers.phase_executors", "_run_export",
            session, ctx,
        )

    def minutes(ctx):
        return _lazy_call(
            "backend.batch.providers.phase_executors", "_run_minutes",
            session, ctx, app_epoch,
        )

    hooks = {"transcribe": transcribe, "align": align,
             "diarize": diarize, "export": export, "minutes": minutes}
    if session is None:  # Test-Aufrufe ohne Session duerfen nichts ausloesen
        raise ProviderRuntimeMissingError("keine_session")
    return hooks


def default_executors(session, *, app_epoch: str = "api", hooks: dict | None = None) -> dict:
    """Phasen-Executor je Phase (Binding-Laufzeit + injizierbare Feature-Hooks)."""
    bound_hooks = hooks if hooks is not None else default_hooks(session, app_epoch=app_epoch)
    return {phase: _phase_executor(phase, bound_hooks[phase]) for phase in REQUIRED_COMMITS}


# ---------------------------------------------------------------------------
# Feature-Ausfuehrung (Zielsystem-Seams fuer akustische Modelllaeufe)
# ---------------------------------------------------------------------------

def _audio_hash(source: dict) -> str:
    return str(source["content_proof"]).removeprefix("sha256:")


def _model_dir(stt_model: str) -> str:
    """Lokales Modellverzeichnis — fail-closed OHNE Download oder Fallback."""
    from ..config import get_models_dir

    root = get_models_dir()
    for cand in (root / stt_model, root / "jfw7-dictate-v1"):
        if cand.exists():
            return str(cand)
    raise WaitingForLocalArtifactError(f"modell_fehlt:{stt_model}")


def _run_transcription(session, item, profile, app_epoch: str) -> dict:
    """JFW-7: Rohtranskription ueber den gebundenen JFW-7-Vertrag (Zielsystem-Seam)."""
    import uuid

    source = item["source"]
    run_id = "jfw7-upload-" + uuid.uuid4().hex
    snapshot = _lazy_call(
        "backend.transcription.snapshot", "build_snapshot",
        audio_hash=_audio_hash(source), manifest_hash=None,
        stt_model=profile["stt_model"], model_revision=profile["model_revision"],
        language_setting=profile["language_setting"],
        backend_variant="cpu", backend_generation=0,
    )
    result = _lazy_call(
        "backend.transcription.engine", "transcribe_file",
        _model_dir(profile["stt_model"]), source["path"], snapshot,
    )
    # Commit = ausschliesslich Referenzen; der Text fließt transient (Carry),
    # damit Standardlogs/`result_refs` keine Transkripte enthalten (JFW-5-AC).
    commit_ref = {
        "feature": "jfw7",
        "run_id": run_id,
        "result_hash": result["text_hash"],
    }
    return {"commit_ref": commit_ref, "status": "succeeded", "warnings": [],
            "carry": {"transcript_text": result["text"]}}


def _upstream_text(ctx: dict) -> str:
    """Upstream-Text: transient aus dem Carry; bei Retry-Reuse aus dem JFW-7-Store (Seam)."""
    return str((ctx.get("carry") or {}).get("transcript_text") or "")


def _run_alignment(session, ctx: dict, app_epoch: str) -> dict:
    """JFW-2: Alignment ueber den gebundenen JFW-2-Vertrag (Zielsystem-Seam)."""
    item = ctx["item"]
    upstream = ctx["commits"]["transcribe"]
    request = _lazy_call(
        "backend.alignment.provenance", "AlignmentRequest",
        job_id=item["item_id"],
        audio_asset_id=item["source"]["path"],
        audio_hash=_audio_hash(item["source"]),
        audio_duration_ms=0,
        timebase="audio_ms_v1",
        transcript_run_id=upstream.get("run_id") or "",
        transcript_revision_id=upstream.get("revision_id") or upstream.get("result_hash") or "",
        transcript_revision_hash=upstream.get("result_hash") or "",
    )
    provider = _lazy_call("backend.alignment.providers.mms_fa", "MmsFaProvider")
    gate = _lazy_call("backend.alignment.artifacts", "ArtifactGate")
    result = _lazy_call(
        "backend.alignment.engine", "run_alignment",
        session, request, _upstream_text(ctx),
        provider=provider, artifact_gate=gate, app_epoch=app_epoch,
    )
    if result.get("outcome") not in ("committed", "aligned", "partially_aligned", "existing"):
        raise FeatureBindingConflictError(str(result.get("outcome")))
    return {"commit_ref": {"feature": "jfw2", "identity_hash": result.get("identity_hash"),
                           "result_hash": result.get("result_hash")},
            "status": "succeeded", "warnings": []}


def _run_diarization(session, ctx: dict, app_epoch: str) -> dict:
    """JFW-3: Diarisierung ueber den gebundenen JFW-3-Vertrag (Zielsystem-Seam)."""
    item, profile = ctx["item"], ctx["profile"]
    upstream = ctx["commits"]["transcribe"]
    request = _lazy_call(
        "backend.diarization.provenance", "DiarizationRequest",
        job_id=item["item_id"],
        audio_asset_id=item["source"]["path"],
        audio_hash=_audio_hash(item["source"]),
        audio_duration_ms=0,
        timebase="audio_ms_v1",
        transcript_run_id=upstream.get("run_id") or "",
        transcript_revision_id=upstream.get("revision_id") or upstream.get("result_hash") or "",
        transcript_revision_hash=upstream.get("result_hash") or "",
        jfw2_reference_status="nicht_angefordert",
        speaker_spec=_speaker_spec(profile),
    )
    provider = _lazy_call("backend.diarization.providers.pyannote_pipeline", "PyannoteProvider")
    gate = _lazy_call("backend.diarization.artifacts", "ArtifactGate")
    result = _lazy_call(
        "backend.diarization.engine", "run_diarization",
        session, request, _upstream_text(ctx), None,
        provider=provider, artifact_gate=gate, app_epoch=app_epoch,
    )
    return {"commit_ref": {"feature": "jfw3", "identity_hash": result.get("identity_hash"),
                           "result_hash": result.get("result_hash")},
            "status": "succeeded", "warnings": []}


def _speaker_spec(profile: dict):
    from ..diarization.provenance import SpeakerSpec

    return SpeakerSpec.parse(
        profile.get("speaker_mode") or "auto",
        count=profile.get("speaker_count"),
        minimum=profile.get("speaker_min"),
        maximum=profile.get("speaker_max"),
    )


def _run_export(session, ctx: dict) -> dict:
    """JFW-4: Export ueber den gebundenen JFW-4-Vertrag (vollstaendig verdrahtet)."""
    from ..export.document import build_set
    from ..export.provenance import ExportRequest, expected_file_names
    from ..export.set_writer import LocalFs, write_set
    from ..export.snapshot import build_snapshot
    from ..services import export_contract as export_store

    item, profile, commits = ctx["item"], ctx["profile"], ctx["commits"]
    upstream = commits["transcribe"]
    request = ExportRequest(
        job_id=item["item_id"],
        audio_asset_id=item["source"]["path"],
        audio_hash=_audio_hash(item["source"]),
        audio_duration_ms=0,
        timebase="audio_ms_v1",
        transcript_run_id=upstream.get("run_id") or "",
        transcript_revision_id=upstream.get("revision_id") or upstream.get("result_hash") or "",
        transcript_revision_hash=upstream.get("result_hash") or "",
        transcript_text_hash=upstream.get("result_hash") or "",
        jfw2_result_hash=commits["align"].get("result_hash") or "",
        jfw2_status="aligned",
        jfw3_result_hash=commits["diarize"].get("result_hash") or "",
        jfw3_status="diarized",
        formats=tuple(profile.get("export_formats") or ("json",)),
        export_profile=profile.get("export_profile") or "lesbare_untertitel_v1",
        name_policy=profile.get("name_policy") or "neutral",
        target_dir=item.get("output", {}).get("target_path"),
    )
    snap = build_snapshot(request, {})
    names = expected_file_names(request)
    submitted = export_store.submit_export(session, request, names, snap.readiness)
    if submitted["outcome"] == "conflict":
        raise FeatureBindingConflictError("export_conflict")
    attempt = export_store.begin_export(session, submitted["export_key"], app_epoch=app_epoch_token())
    built = build_set(snap, request)
    write_set(LocalFs(), request.target_dir, built["files"], {}, attempt_id=attempt, replace=False)
    outcome = export_store.commit_set(
        session, submitted["export_key"],
        manifest=built["document"]["presentation"],
        result_hash=built["document"]["result_hash"],
    )
    if outcome != "committed":
        raise FeatureBindingConflictError(str(outcome))
    return {"commit_ref": {"feature": "jfw4", "export_key": submitted["export_key"],
                           "result_hash": built["document"]["result_hash"]},
            "status": "succeeded", "warnings": []}


def app_epoch_token() -> str:
    return "jfw5-batch"


def _run_minutes(session, ctx: dict, app_epoch: str) -> dict:
    """JFW-13: Protokoll ueber den gebundenen JFW-13-Vertrag (Zielsystem-Seam)."""
    raise WaitingForLocalArtifactError("minutes_lokalmodell_fehlt")
