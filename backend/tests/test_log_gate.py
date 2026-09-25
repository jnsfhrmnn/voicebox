# -*- coding: utf-8 -*-
"""USCRX-2026-16007/RL-08: Log-Gate — Negativtests (fail-closed, generisch)."""
import logging

import pytest

from backend.minutes.log_guard import ContentFreeLogFilter, install_log_gate
from backend.minutes.pseudonym import assert_content_free


def _logger_with_filter():
    logger = logging.getLogger("test-log-gate")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.filters.clear()
    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record)
    handler.addFilter(ContentFreeLogFilter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger, records


def test_rl08_zuordnung_im_log_msg_wird_redigiert():
    logger, records = _logger_with_filter()
    logger.info({"original_text": "Klarname"})
    assert len(records) == 1, "Record läuft weiter, nur redigiert"
    assert records[0].msg == "[log-gate: Zuordnungsinhalt unterdrückt]"
    assert "Klarname" not in str(records[0].msg)


def test_rl08_zuordnung_in_extra_wird_redigiert():
    logger, records = _logger_with_filter()
    logger.info("laufende Verarbeitung", extra={"register_mapping": {"a": "b"}})
    assert len(records) == 1
    assert records[0].msg == "[log-gate: Zuordnungsinhalt unterdrückt]"


def test_rl08_listen_nestung_wird_redigiert_und_crashed_nicht():
    logger, records = _logger_with_filter()
    logger.info({"entries": [{"name": "Klarname"}]})
    assert len(records) == 1
    assert records[0].msg == "[log-gate: Zuordnungsinhalt unterdrückt]"


def test_rl08_benigne_dict_logs_crashen_nicht():
    """Review-Befund: generische Schlüssel dürfen keinen Produktivpfad werfen."""
    logger, records = _logger_with_filter()
    logger.info({"name": "whisper-turbo"})
    assert len(records) == 1
    assert records[0].msg == "[log-gate: Zuordnungsinhalt unterdrückt]"


def test_rl08_inhaltsfreie_records_bleiben_erlaubt():
    logger, records = _logger_with_filter()
    logger.info("Verarbeitung gestartet")
    assert len(records) == 1


def test_rl08_install_log_gate_ist_idempotent():
    root = logging.getLogger("test-log-gate-root")
    root.handlers.clear()
    handler = logging.Handler()
    handler.emit = lambda record: None
    root.addHandler(handler)
    assert install_log_gate(root) == 1
    assert install_log_gate(root) == 0, "zweite Installation muss ein No-Op sein"


def test_rl08_assert_content_free_bleibt_verwendbar():
    assert assert_content_free({"entries": [{"entry_id": "e1"}]}) is None
    with pytest.raises(ValueError):
        assert_content_free({"klarname": "x"})
