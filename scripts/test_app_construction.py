#!/usr/bin/env python3
"""JFW-1 App-Konstruktionstest (ohne torch).

Konstruiert die ECHTE FastAPI-App (`backend.app.create_app()`), aber mit allen
schweren ML-/Audio-Paketen gestubbt. Damit wird jeder Module-Level-Import im
Erreichbarkeitsgraph ausgefuehrt: bricht ein Import (z.B. durch einen TTS-Cut),
scheitert dieser Test sofort — ohne dass torch installiert sein muss.

Das ist die statische Bruecke zwischen "Importgraph-Analyse" und dem echten
Build/Smoke: es fängt genau die Fehlerklasse, die der Analyzer nicht sieht
(fehlende Attribute, kaputte Importketten), aber noch ohne ML-Runtime.

Aufruf (aus dem Fork-Root):
    .venv-test/Scripts/python scripts/test_app_construction.py
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _stub(name: str, **attrs) -> None:
    """Legt ein leeres Modul in sys.modules an (mit optionalen Attributen)."""
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


def install_stubs() -> None:
    """Alle schweren ML-/Audio-Pakete stubben, die im App-Graph importiert werden."""

    class _Anything:
        def __init__(self, *a, **k):
            pass

        def __call__(self, *a, **k):
            return self

        def __getattr__(self, item):
            return _Anything()

    # torch (inkl. Submodule) — wird in app.py/health.py auf Module-Level importiert.
    # Module-level __getattr__-Fallback: jedes nicht explizit gesetzte Attribut
    # liefert _Anything, damit Import-Zeit-Zugriffe (torch.Tensor, torch.float32,
    # torch.cuda.is_available ...) nicht brechen.
    def _make_torch_module(name):
        m = types.ModuleType(name)

        def __getattr__(item, _m=m):
            return _Anything()

        m.__getattr__ = __getattr__
        m.cuda = _Anything()
        m.backends = _Anything()
        m.Tensor = _Anything()
        m.float32 = _Anything()
        m.float16 = _Anything()
        m.device = _Anything()
        return m

    for sub in ["torch", "torch.cuda", "torch.backends", "torch.backends.mps",
                "torch.nn", "torch.utils"]:
        sys.modules[sub] = _make_torch_module(sub)

    # transformers / huggingface_hub (Whisper-Modelle) — constants braucht echte
    # String-Werte, weil hf_offline_patch sie auf Module-Level als Pfad nutzt.
    import tempfile
    _cache = Path(tempfile.gettempdir()) / "hf-cache"
    _stub("transformers")
    _stub("huggingface_hub", constants=None)  # Platzhalter, wird unten gesetzt
    _constants = types.ModuleType("huggingface_hub.constants")
    _constants.HF_HUB_CACHE = str(_cache)
    _constants.HF_HOME = str(_cache.parent / "hf-home")
    _constants.HUGGINGFACE_CO_URL_DEFAULT = "https://huggingface.co"
    sys.modules["huggingface_hub"].constants = _constants
    sys.modules["huggingface_hub.constants"] = _constants
    _stub("huggingface_hub.utils", is_offline_mode=lambda: True)
    _stub("tqdm")

    # Audio-Kette
    for name in ["soundfile", "librosa", "pedalboard", "dac", "numpy"]:
        if name == "numpy":
            continue  # numpy ist echt installiert
        _stub(name)

    # TTS-/LLM-Engines (müssen NICHT importierbar sein — Stub verhindert, dass
    # ein versehentlicher Import den Test grün macht; der Analyzer prüft das statisch)
    for name in ["chatterbox", "kokoro", "qwen_tts", "zipvoice", "tada",
                 "mlx", "mlx_audio", "mlx_lm", "intel_extension_for_pytorch",
                 "torch_directml"]:
        _stub(name)

    # transformers.WhisperForConditionalGeneration etc. — Attribute werden lazily
    # in Methoden genutzt; Module-Level reicht ein leeres Modul.


# Pfade, die im Transkriptionsprofil ERREICHBAR sein muessen (STT-Kern).
REQUIRED_PATHS = ["/health", "/captures", "/diarization"]

# Pfade, die NICHT erreichbar sein duerfen (TTS-/LLM-Flaechen).
# Prefixe fuer ganze Flaechen + exakte Endpunkte, die in aktiven Routern
# verbleiben (aus dem OpenAPI-Vertrag gemessen).
FORBIDDEN_PATH_PREFIXES = [
    "/speak",          # TTS-Sprechen
    "/generate",       # TTS-Generierung
    "/stories",        # Story-Struktur
    "/voices",         # Voice-Profile
    "/effects",        # Effects
    "/profiles",       # TTS-Voice-Profile
    "/audio/",         # generiertes TTS-Audio (Generation-/Versionen)
]

# Exakte verbotene Endpunkte (Template-Pfade), die in aktiven Routern sitzen.
FORBIDDEN_PATHS_EXACT = [
    "/models/load",                    # laedt das TTS-Modell
    "/models/unload",                  # entlaedt das TTS-Modell
    "/settings/generation",            # TTS-Generierungs-Einstellungen
    "/samples/{sample_id}",            # Voice-Profil-Samples (TTS)
    "/captures/{capture_id}/refine",   # LLM-Refinement (kein lokales Textmodell)
]


def main() -> int:
    install_stubs()
    sys.path.insert(0, str(REPO_ROOT))

    try:
        from backend.app import create_app  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        print(f"[app-construction] FEHLER beim Import von backend.app: {type(e).__name__}: {e}")
        return 1

    try:
        app = create_app()
    except Exception as e:  # noqa: BLE001
        print(f"[app-construction] FEHLER bei create_app(): {type(e).__name__}: {e}")
        return 1

    # OpenAPI-Schema loest die lazy Routen auf — das ist der echte HTTP-Vertrag.
    try:
        spec = app.openapi()
    except Exception as e:  # noqa: BLE001
        print(f"[app-construction] FEHLER bei openapi(): {type(e).__name__}: {e}")
        return 1

    paths = sorted(spec.get("paths", {}).keys())
    missing_required = [p for p in REQUIRED_PATHS if not any(pth == p or pth.startswith(p + "/") for pth in paths)]
    forbidden_present = sorted(
        set(pth for pth in paths for pref in FORBIDDEN_PATH_PREFIXES if pth.startswith(pref))
        | (set(FORBIDDEN_PATHS_EXACT) & set(paths))
    )

    print(f"[app-construction] FastAPI-App konstruiert, {len(paths)} Pfade im HTTP-Vertrag:")
    for pth in paths:
        print(f"  - {pth}")

    ok = True
    if missing_required:
        print(f"[app-construction] FEHLER: erforderliche STT-Pfade fehlen: {missing_required}")
        ok = False
    if forbidden_present:
        print(f"[app-construction] FEHLER: verbotene TTS-/LLM-Pfade erreichbar: {forbidden_present}")
        ok = False

    if ok:
        print("[app-construction] OK: STT-Kern erreichbar, keine TTS-/LLM-Flaechen im Vertrag.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
