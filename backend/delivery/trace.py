"""JFW-8: Fehlerpfad-Spuren — nachvollziehbar und konstruktiv inhaltsfrei.

Spec AC „Oberflaeche, Datenschutz und Abnahme": Logs und Crash-Dumps enthalten
IDs, Adapter, Zustaende, Dauer und Fehlercodes, aber KEINEN Rohtext, KEINEN
Clipboard-Inhalt, KEINE Fenstertitel mit Inhalt und KEINE Credentials.
``assert_trace_content_free`` macht die Regel testbar und fail-closed.
"""
from __future__ import annotations

from .payload import DeliveryVertragError

#: Felder, die Inhalte tragen koennten — in Spuren prinzipiell verboten.
FORBIDDEN_TRACE_KEYS = (
    "raw_text",
    "text",
    "clipboard",
    "clipboard_content",
    "window_title",
    "credential",
    "password",
    "token",
)


def assert_trace_content_free(trace: dict) -> None:
    """Fail-closed: inhaltstragende Felder duerfen nie in eine Spur."""
    for key, value in (trace or {}).items():
        if key in FORBIDDEN_TRACE_KEYS:
            raise DeliveryVertragError(f"spur_inhaltstragend:{key}")
        if isinstance(value, dict):
            assert_trace_content_free(value)


def build_error_trace(
    *,
    operation_id: str,
    adapter: str,
    states: list[str],
    duration_ms: int,
    error_code: str,
    extra: dict | None = None,
) -> dict:
    """Nachvollziehbare Fehlerpfad-Spur: IDs, Adapter, Zustaende, Dauer, Code."""
    trace = {
        "operation_id": operation_id,
        "adapter": adapter,
        "states": list(states or []),
        "duration_ms": int(duration_ms),
        "error_code": error_code,
    }
    if extra:
        for key, value in extra.items():
            if key in FORBIDDEN_TRACE_KEYS:
                raise DeliveryVertragError(f"spur_inhaltstragend:{key}")
            trace[key] = value
    assert_trace_content_free(trace)
    return trace
