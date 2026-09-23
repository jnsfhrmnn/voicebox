"""Route registration for the JF Whisper API (Transkriptions-Produktprofil).

Nur die im Produktprofil aktiven Router werden importiert:
`product-profiles/jf-whisper.json` ist die einzige Build-Wahrheit. Verbotene
Flaechen (TTS, Generation, Stories, Voices, Effects, LLM, MCP, CUDA-Auto-Update)
bleiben aus dem Laufzeitvertrag entfernt — `scripts/verify_profile.py --check`
prueft diesen Importgraph fail-closed gegen das Profil.
"""

from fastapi import FastAPI


def register_routers(app: FastAPI) -> None:
    """Include the transcription-profile routers on the application."""
    from .health import router as health_router
    from .captures import router as captures_router
    from .transcription import router as transcription_router
    from .models import router as models_router
    from .settings import router as settings_router
    from .tasks import router as tasks_router
    from .alignment import router as alignment_router
    from .diarization import router as diarization_router
    from .meeting import router as meeting_router
    from .recording import router as recording_router
    from .dictation import router as dictation_router

    app.include_router(health_router)
    app.include_router(captures_router)
    app.include_router(transcription_router)
    app.include_router(models_router)
    app.include_router(settings_router)
    app.include_router(tasks_router)
    app.include_router(alignment_router)
    app.include_router(diarization_router)
    app.include_router(meeting_router)
    app.include_router(recording_router)
    app.include_router(dictation_router)
