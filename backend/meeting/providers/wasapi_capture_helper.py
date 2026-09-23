"""JFW-11: Capture-Provider-Seam für den WASAPI-Capture-Helfer (fail-closed).

Der Helferprozess ``jf-whisper-capture`` (.NET 9 / NAudio.Wasapi 3.1.0) ist die
im Spike bewährte Capture-Schicht (``MMDeviceEnumerator`` +
``PKEY_AudioEndpoint_StableId``, ``WasapiRecorder`` mit Paket-QPC +
Frameposition je Paket, ``WithProcessLoopback``). Seine Implementierung und
Bündelung ist der Folge-Block (Zielsystem); bis dahin meldet diese Seam
fail-closed ``provider_runtime_missing`` — **kein** stiller Geräte- oder
Modusersatz, keine Cloud-Alternative.

Journal-Vertrag des Helfers (Spike §1.1/§4.1): je Spur headerlose ``data.bin``,
zeilenweises ``journal.csv`` (seq, qpc_100ns, devpos_frames, bytes, flags,
callback_timestamp) und ``meta.json``; Durability-Flush(``Flush(true)``) alle
≤ 100 ms; leere Schatten-Callbacks werden über
``backend.meeting.time_model.is_real_packet`` verworfen.
"""
from __future__ import annotations

HELPER_NAME = "jf-whisper-capture"
REASON_RUNTIME_MISSING = "provider_runtime_missing"


class ProviderRuntimeMissingError(RuntimeError):
    """Capture-Helfer nicht verfügbar — Start abgelehnt (fail-closed)."""

    def __init__(self) -> None:
        super().__init__(
            f"Capture-Helfer {HELPER_NAME!r} ist noch nicht gebündelt/verfügbar "
            f"({REASON_RUNTIME_MISSING})."
        )
        self.reason_code = REASON_RUNTIME_MISSING


def probe_capture_helper() -> dict:
    """Sichtbarer Preflight-Status der Capture-Schicht (ohne Gerätezugriff)."""
    return {
        "helper": HELPER_NAME,
        "available": False,
        "reason_code": REASON_RUNTIME_MISSING,
    }


def start_capture(*, role: str) -> dict:
    """Startet eine Capture-Spur (Folge-Block). Bis dahin fail-closed."""
    del role
    raise ProviderRuntimeMissingError()
