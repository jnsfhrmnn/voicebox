"""JFW-6: Aufnahmezustandsvertrag (State Contract, I/O-frei).

Spec „State Contract" (Tabelle exakt):

* ``idle`` — kein aktiver Run → ``starting``
* ``starting`` — Hotkey angenommen; Ziel, Mikrofon und Recovery werden
  vorbereitet (Pill „Startet" — keine Behauptung aktiver Aufnahme) →
  ``recording``, ``failed``, ``canceled``
* ``recording`` — erster Audioblock dauerhaft angenommen → ``stopping``,
  ``failed``
* ``stopping`` — Stopgrenze gesetzt; angenommene Frames werden finalisiert →
  ``secured``, ``failed``
* ``secured`` — Audio und Manifest atomar gebunden → JFW-7-Handoff oder
  lokale Recovery (terminal)
* ``failed`` — kein vollstaendiger Normalabschluss → Recovery (``secured``)
  oder Verwerfen (``canceled``); kein automatischer Neustart
* ``canceled`` — Start oder Run bewusst verworfen → Löschvertrag (terminal)

Exactly-once: ein bereits angenommener Start- oder Stopp-Uebergang dedupliziert
Auto-Repeat/Mehrfachfeuer („wenn ein Start- oder Stoppuebergang bereits
angenommen ist, entsteht weder ein zweiter Run noch ein zweiter Abschluss").
Der Verwerfen-Pfad ist aus jedem nicht-terminalen Zustand erreichbar, weil die
AC „Verwerfen wenn Audio vorhanden" ausdruecklich aus ``recording``/``stopping``
bedienbar sein muss; die Tabelle nennt die Lebenszyklus-Uebergaenge.
"""
from __future__ import annotations

from dataclasses import dataclass, field

IDLE = "idle"
STARTING = "starting"
RECORDING = "recording"
STOPPING = "stopping"
SECURED = "secured"
FAILED = "failed"
CANCELED = "canceled"

ALLOWED_TRANSITIONS = {
    IDLE: (STARTING,),
    STARTING: (RECORDING, FAILED, CANCELED),
    RECORDING: (STOPPING, FAILED),
    STOPPING: (SECURED, FAILED),
    SECURED: (),
    FAILED: (SECURED, CANCELED),
    CANCELED: (),
}

#: Zulaessige Stopp-Ursachen (Toggle, PTT-Release, Bedienung, Systemstopp).
STOP_CAUSES = (
    "toggle",
    "ptt_release",
    "bedienung",
    "system_gesperrt",
    "standby",
    "benutzerwechsel",
    "session_ende",
    "prozessfehler",
    "geraetefeher",
    "persistenz_ueberlauf",
)

#: Systembedingte Stopp-Ursachen (Sperren, Benutzerwechsel, Standby, Session-Ende).
SYSTEM_STOP_CAUSES = ("system_gesperrt", "standby", "benutzerwechsel", "session_ende")


class RunStateError(RuntimeError):
    """Fail-closed: unzulaessige Transition oder fehlende Bestätigung."""


@dataclass
class RunSession:
    """Reiner Aufnahme-Run-Zustand (I/O-frei) — genau ein Run je ``run_id``."""

    run_id: str
    started_at_100ns: int = 0
    state: str = IDLE
    format_bound: dict | None = None
    stop_intents: list = field(default_factory=list)
    reason_code: str | None = None
    deletion_contract_hash: str | None = None
    last_point_100ns: int = 0

    def _point(self, t_100ns: int) -> int:
        t = int(t_100ns)
        if t <= self.last_point_100ns:
            raise RunStateError("zeitpunkt_nicht_monoton")
        self.last_point_100ns = t
        return t

    def begin(self, *, hotkey_ready: bool, t_100ns: int) -> None:
        """Genau ein Start-Uebergang: ``idle → starting``.

        Auto-Repeat/Mehrfachfeuer waehrend eines bereits angenommenen
        Uebergangs erzeugt keinen zweiten Run; ohne registrierten Hotkey gibt
        es keine stille Ersatzbelegung und keinen Run.
        """
        if self.state != IDLE:
            if self.state in (SECURED, FAILED, CANCELED):
                raise RunStateError("kein_automatischer_neustart")
            raise RunStateError("start_bereits_angenommen")
        if not hotkey_ready:
            raise RunStateError("hotkey_nicht_bereit")
        self.started_at_100ns = self._point(t_100ns)
        self.state = STARTING

    def bind_format(self, sample_format: str, channels: int, rate_hz: int) -> dict:
        """Bindet das tatsaechlich geoeffnete Format an den Run (genau einmal)."""
        if self.state != STARTING:
            raise RunStateError("formatbindung_nur_beim_start")
        if self.format_bound is not None:
            raise RunStateError("format_bereits_gebunden")
        self.format_bound = {
            "sample_format": str(sample_format),
            "channels": int(channels),
            "rate_hz": int(rate_hz),
        }
        return dict(self.format_bound)

    def assert_format(self, *, sample_format: str, channels: int, rate_hz: int) -> None:
        """Unzulaessige Formatänderung waehrend des Runs ist blockiert, nie
        still konvertiert."""
        if self.format_bound is None:
            raise RunStateError("format_nicht_gebunden")
        actual = {
            "sample_format": str(sample_format),
            "channels": int(channels),
            "rate_hz": int(rate_hz),
        }
        if actual != self.format_bound:
            raise RunStateError("format_aenderung_blockiert")

    def activate(
        self,
        *,
        stream_open: bool,
        run_persisted: bool,
        first_block_accepted: bool,
        t_100ns: int,
    ) -> None:
        """``starting → recording`` erst wenn Stream offen, Run persistierbar
        und erster angenommener Audioblock bestätigt sind — sonst bleibt die
        Pill „Startet"."""
        if self.state != STARTING:
            raise RunStateError("aktivierung_nur_aus_starting")
        if self.format_bound is None:
            raise RunStateError("format_nicht_gebunden")
        if not (stream_open and run_persisted and first_block_accepted):
            raise RunStateError("start_unvollstaendig")
        self._point(t_100ns)
        self.state = RECORDING

    def request_stop(self, *, cause: str, t_100ns: int) -> dict:
        """Genau ein Stopp-Intent mit Ursache und monotonem Zeitpunkt."""
        if self.stop_intents:
            raise RunStateError("stopp_bereits_angenommen")
        if cause not in STOP_CAUSES:
            raise RunStateError("stopgrund_unbekannt")
        if self.state != RECORDING:
            raise RunStateError("stopp_nur_aus_recording")
        intent = {"cause": cause, "t_100ns": self._point(t_100ns), "seq": 1}
        self.stop_intents.append(intent)
        self.state = STOPPING
        return dict(intent)

    def secure(self, *, t_100ns: int) -> None:
        """``stopping →secured`` — erst nach gesetzter Stopgrenze."""
        if self.state != STOPPING:
            raise RunStateError("sicherung_nur_aus_stopping")
        if not self.stop_intents:
            raise RunStateError("stopp_grenze_fehlt")
        self._point(t_100ns)
        self.state = SECURED

    def fail(self, *, reason_code: str, t_100ns: int) -> None:
        """Fehlerpfad (Geräte-/Prozess-/Persistenzfehler) → ``failed``."""
        if self.state not in (STARTING, RECORDING, STOPPING):
            raise RunStateError("kein_fehlfaehiger_zustand")
        self._point(t_100ns)
        self.reason_code = str(reason_code)
        self.state = FAILED

    def complete_recovery(self, *, t_100ns: int) -> None:
        """``failed → secured``: lokales Recovery sichert das recoverbare Audio."""
        if self.state != FAILED:
            raise RunStateError("recovery_nur_aus_failed")
        self._point(t_100ns)
        self.state = SECURED

    def discard(
        self,
        *,
        confirmed: bool,
        has_audio: bool,
        deletion_contract_hash: str | None,
        t_100ns: int,
    ) -> None:
        """Ausdrückliches „Verwerfen" → ``canceled`` (Löschvertrag, kein Handoff).

        Mit vorhandenem Audio sind ausdrueckliche Bestätigung UND gebundener
        Löschvertrag Pflicht; geloescht wird erst nach dem Löschvertrag.
        """
        if self.state not in (STARTING, RECORDING, STOPPING, FAILED):
            raise RunStateError("verwerfen_nicht_moeglich")
        if has_audio and not confirmed:
            raise RunStateError("abbruch_bestaetigung_erforderlich")
        if has_audio and not deletion_contract_hash:
            raise RunStateError("loeschvertrag_erforderlich")
        self._point(t_100ns)
        self.deletion_contract_hash = deletion_contract_hash
        self.state = CANCELED
