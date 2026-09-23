"""JFW-6: Stabile Run-Identität und monotone Zeitpunkte (I/O-frei).

Spec „Hotkey, Run-Identität und Sichtbarkeit": genau ein neuer Run mit
stabiler ID; Stopp-Intents tragen einen monotonen Zeitpunkt (``qpc_100ns``,
Zeitmodell-Einheit wie JFW-11). Die Run-ID ist über den gesamten
Lebenszyklus inklusive Recovery unveraendert.
"""
from __future__ import annotations

import re
import uuid

RUN_ID_PREFIX = "jfw6-run"
_TAIL = re.compile(r"^[0-9a-f]{32}$")


class RunIdentityError(RuntimeError):
    """Fail-closed: unzulaessige Run-ID oder nicht-monotoner Zeitpunkt."""


def new_run_id() -> str:
    """Stabile Run-ID ``jfw6-run-<uuid4hex>`` — genau einmal je Run erzeugt."""
    return f"{RUN_ID_PREFIX}-{uuid.uuid4().hex}"


def is_valid_run_id(run_id: str) -> bool:
    if not isinstance(run_id, str):
        return False
    prefix = f"{RUN_ID_PREFIX}-"
    if not run_id.startswith(prefix):
        return False
    return bool(_TAIL.match(run_id[len(prefix) :]))


class RunClock:
    """Liefert streng monoton steigende ``qpc_100ns``-Zeitpunkte.

    Auto-Repeat oder Ruecklauf (nahezu gleichzeitig mehrfach gemeldete
    Uebergaenge) erzeugen keinen zweiten Punkt — fail-closed.
    """

    def __init__(self) -> None:
        self._last_100ns = 0

    def point(self, t_100ns: int) -> int:
        t = int(t_100ns)
        if t <= 0:
            raise RunIdentityError("zeitpunkt_unzulaessig")
        if t <= self._last_100ns:
            raise RunIdentityError("zeitpunkt_nicht_monoton")
        self._last_100ns = t
        return t
