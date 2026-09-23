"""JFW-5: reale Discovery-Probe fuer Windows (einzige I/O-Stelle der Discovery).

Reparse Points/Symlinks/Junctions werden erkannt und NICHT gefolgt; versteckte
und System-Eintraege werden als ausgeschlossen gemeldet. Der Kern bleibt ueber
das ``DiscoveryFs``-Protokoll I/O-frei testbar.
"""
from __future__ import annotations

import os
from pathlib import Path

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class LocalDiscoveryFs:
    """``DiscoveryFs``-Implementierung gegen das lokale Dateisystem."""

    def list_dir(self, path: str) -> list[dict]:
        entries = []
        with os.scandir(path) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    st = None
                attrs = int(getattr(st, "st_file_attributes", 0) or 0) if st else 0
                is_symlink = entry.is_symlink()
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
                hidden = bool(attrs & FILE_ATTRIBUTE_HIDDEN)
                system = bool(attrs & FILE_ATTRIBUTE_SYSTEM)
                reparse = bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT) or is_symlink
                if os.name != "nt":
                    hidden = hidden or entry.name.startswith(".")
                entries.append(
                    {
                        "name": entry.name,
                        "is_dir": is_dir,
                        "is_file": is_file,
                        "is_reparse": reparse,
                        "hidden": hidden,
                        "system": system,
                    }
                )
        return entries

    def file_facts(self, path: str) -> dict:
        st = os.stat(path, follow_symlinks=False)
        return {
            "size": int(st.st_size),
            "mtime_ns": int(getattr(st, "st_mtime_ns", 0) or 0),
        }

    def read_chunks(self, path: str, chunk_size: int = 8 * 1024 * 1024):
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    return
                yield chunk


def target_exists(target: str) -> bool:
    """Vorhandenes Ziel erkennen (fuer die Nicht-ueberschreiben-Regel)."""
    return Path(target).exists()
