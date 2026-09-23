"""JFW-11: Track-Vertrag — zwei getrennte autoritative Rohspuren (I/O-frei).

Spec „Quellentrennung": Mikrofon und Remote-Audio bleiben zwei getrennte
autoritative Spuren mit eigener Track-ID, Quellenrolle, Capture-Modus/-Identität,
Formatbeschreibung, Sample-/Frame-Zahl und Inhalts-Hash und werden niemals still
zu einer einzigen Rohspur zusammengemischt. Quellenwiederkehr erzeugt einen neuen
Abschnitt mit eigener Geräte-/Zeitprovenienz und sichtbarer Lücke.

Transparenzregel (Spike §7.4): der angezeigte Scope ist „gesamter Prozessbaum“,
„gesamter Ausgang“ oder „keine zuverlässige Quelle“ — **nie** „einzelner Tab“.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ROLE_MIC = "mic"
ROLE_REMOTE = "remote"
SOURCE_ROLES = (ROLE_MIC, ROLE_REMOTE)

MODE_PROCESS = "process_loopback"
MODE_ENDPOINT = "endpoint_loopback"
CAPTURE_MODES = (MODE_PROCESS, MODE_ENDPOINT)

SCOPE_PROCESS_TREE = "gesamter_prozessbaum"
SCOPE_ENDPOINT = "gesamter_ausgang"
SCOPE_UNRELIABLE = "keine_zuverlaessige_quelle"
VALID_SCOPES = (SCOPE_PROCESS_TREE, SCOPE_ENDPOINT, SCOPE_UNRELIABLE)
FORBIDDEN_SCOPES = ("einzelner_tab",)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class TrackSection:
    """Ein Track-Abschnitt mit eigener Geräte-/Zeitprovenienz."""

    section_id: str
    device_identity: dict
    fmt: dict
    frames: int
    content_hash: str
    started_at_100ns: int
    ended_at_100ns: int
    gaps: tuple[tuple[int, int], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "section_id": self.section_id,
            "device_identity": dict(self.device_identity),
            "fmt": dict(self.fmt),
            "frames": self.frames,
            "content_hash": self.content_hash,
            "started_at_100ns": self.started_at_100ns,
            "ended_at_100ns": self.ended_at_100ns,
            "gaps": [list(g) for g in self.gaps],
        }


@dataclass(frozen=True)
class Track:
    """Eine autoritative Rohspur (Rolle ``mic`` | ``remote``)."""

    track_id: str
    role: str
    capture_mode: str
    scope_label: str
    sections: tuple[TrackSection, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "role": self.role,
            "capture_mode": self.capture_mode,
            "scope_label": self.scope_label,
            "sections": [s.to_dict() for s in self.sections],
        }


def validate_tracks(tracks: list[Track]) -> list[str]:
    """Fail-closed Validierung; leere Liste = Vertrag erfüllt."""
    errors: list[str] = []
    if len(tracks) != 2:
        errors.append("zwei_quellen_erforderlich")
    roles = [t.role for t in tracks]
    if len(tracks) == 2 and len(set(roles)) != 2:
        errors.append("rollen_eindeutig")
    for role in roles:
        if role not in SOURCE_ROLES:
            errors.append("rolle_unbekannt")
    ids = [t.track_id for t in tracks]
    if len(set(ids)) != len(ids):
        errors.append("track_id_doppelt")

    seen_sections: set[str] = set()
    for track in tracks:
        if track.capture_mode not in CAPTURE_MODES:
            errors.append("modus_unbekannt")
        if track.scope_label in FORBIDDEN_SCOPES or track.scope_label not in VALID_SCOPES:
            errors.append("scope_unzulaessig")
        elif (
            (track.capture_mode == MODE_PROCESS and track.scope_label == SCOPE_ENDPOINT)
            or (track.capture_mode == MODE_ENDPOINT and track.scope_label == SCOPE_PROCESS_TREE)
        ):
            errors.append("scope_passt_nicht_zum_modus")
        if not track.sections:
            errors.append("abschnitte_fehlen")
        for sec in track.sections:
            if sec.section_id in seen_sections:
                errors.append("abschnitt_id_doppelt")
            seen_sections.add(sec.section_id)
            if sec.frames < 0:
                errors.append("frames_ungueltig")
            if not _HEX64.match(sec.content_hash or ""):
                errors.append("hash_ungueltig")
            if sec.ended_at_100ns < sec.started_at_100ns:
                errors.append("abschnitt_zeit_ungueltig")
            if track.capture_mode == MODE_ENDPOINT and not sec.device_identity.get("stable_id"):
                errors.append("stable_id_fehlt")
            if track.capture_mode == MODE_PROCESS and not (
                sec.device_identity.get("process_tree_id") or sec.device_identity.get("windows_pid")
            ):
                errors.append("prozess_identitaet_fehlt")
    return errors
