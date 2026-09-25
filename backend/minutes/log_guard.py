# -*- coding: utf-8 -*-
"""USCRX-2026-16007/RL-08: Log-Gate — Zuordnungen nie in Logs/Metriken/Crash-Dumps.

``ContentFreeLogFilter`` bindet ``assert_content_free``-Semantik generisch an die
tatsächliche Log-Serialisierung (die Handler): alle Records der App laufen durch
das Gate; ein Payload mit verbotenen Zuordnungs-Schlüsseln (FORBIDDEN_CONTENT_KEYS)
wird fail-closed abgelehnt statt still geloggt. Installation in
``backend/app.py`` (_run_startup) auf den Root-Handlern.
"""
from __future__ import annotations

import logging

from .pseudonym import FORBIDDEN_CONTENT_KEYS

#: Standard-Attribute eines LogRecord — alles andere wird als Payload geprüft.
_STANDARD_RECORD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}


def _assert_payload_content_free(payload, where: str) -> None:
    if isinstance(payload, dict):
        for key in payload:
            if key in FORBIDDEN_CONTENT_KEYS:
                raise ValueError(f"inhalt_im_log:{key} ({where})")
        for value in payload.values():
            _assert_payload_content_free(value, where)
    elif isinstance(payload, (list, tuple, set)):
        for item in payload:
            _assert_payload_content_free(item, where)


class ContentFreeLogFilter(logging.Filter):
    """Prüft Msg-/Arg-/Extra-Payloads jedes Records auf Zuordnungsinhalte.

    Verhalten (USCRX-2026-16007/RL-08, Review-Korrektur): verbotene Inhalte werden
    **redigiert statt eine Exception zu werfen** — der Leak bleibt fail-closed
    (Inhalt erreicht den Handler nie), aber ein benigner Log-Aufruf mit generischem
    Schlüssel (z. B. ``{"name": "whisper"}``) kann keinen Produktivpfad crashen.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            _assert_payload_content_free(record.msg, "msg")
            if record.args:
                _assert_payload_content_free(record.args, "args")
            extras = {k: v for k, v in vars(record).items() if k not in _STANDARD_RECORD_ATTRS}
            _assert_payload_content_free(extras, "extra")
        except ValueError:
            record.msg = "[log-gate: Zuordnungsinhalt unterdrückt]"
            record.args = None
            for key in list(vars(record)):
                if key not in _STANDARD_RECORD_ATTRS:
                    delattr(record, key)
        return True


def install_log_gate(root: logging.Logger | None = None) -> int:
    """Hängt das Gate an alle Handler des Root-Loggers. Liefert die Zahl der Handler."""
    logger = root if root is not None else logging.getLogger()
    installed = 0
    for handler in logger.handlers:
        if not any(isinstance(f, ContentFreeLogFilter) for f in handler.filters):
            handler.addFilter(ContentFreeLogFilter())
            installed += 1
    return installed
