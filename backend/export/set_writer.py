"""JFW-4: atomarer Set-Commit auf dem Zielfs (injizierbare FS-Operationen).

Alle Inhalte werden vom Aufruf erzeugt und validiert, bevor irgendeine Datei
entsteht: erst ``.jfw4-attempt-<attempt_id>-…``-Tempdateien, dann pro Enddatei
Backup (``<name>.jfw4-replaced-<attempt_id>``) + ``os.replace``. Jeder Fehler im
Commit-Roll rollt zurueck (vorheriger vollstaendiger Set bleibt erhalten);
verwaiste Attempt-/Backup-Reste sind am Praefix erkennbar und bereinigbar.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re

ATTEMPT_PREFIX = ".jfw4-attempt-"
REPLACED_INFIX = ".jfw4-replaced-"

_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class SetWriteError(RuntimeError):
    def __init__(self, reason_code: str, details=None):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.details = details or []


def safe_file_name(name: str) -> tuple[bool, str | None]:
    if not name or len(name) > 255:
        return False, "name_leer_oder_zu_lang"
    if _INVALID_NAME_CHARS.search(name):
        return False, "name_enthaelt_reservierte_zeichen"
    if name.endswith((".", " ")):
        return False, "name_endet_mit_punkt_oder_leerzeichen"
    base = name.split(".")[0].upper()
    if base in _RESERVED_NAMES:
        return False, "name_reserviert"
    return True, None


def join(dirpath: str, name: str) -> str:
    base = str(dirpath)
    while base.endswith(("/", "\\")):
        base = base[:-1]
    return base + "/" + name


class LocalFs:
    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def write_bytes(self, path: str, data: bytes) -> None:
        with open(path, "wb") as handle:
            handle.write(data)

    def read_bytes(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    def replace(self, src: str, dst: str) -> None:
        os.replace(src, dst)

    def remove(self, path: str) -> None:
        os.remove(path)

    def listdir(self, dirpath: str) -> list[str]:
        try:
            return os.listdir(dirpath)
        except OSError:
            return []

    def makedirs(self, dirpath: str) -> None:
        os.makedirs(dirpath, exist_ok=True)


class MemFs:
    """In-Memory-FS fuer Vertragstests inklusive Fehlerinjektion."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.fail_on_write: set[str] = set()
        self.fail_on_replace: set[str] = set()
        self.fail_on_remove: set[str] = set()

    @staticmethod
    def _norm(path: str) -> str:
        return str(path).replace("\\", "/")

    def exists(self, path: str) -> bool:
        return self._norm(path) in self.files

    def write_bytes(self, path: str, data: bytes) -> None:
        path = self._norm(path)
        if path in self.fail_on_write:
            raise OSError(f"injizierter Schreibfehler: {path}")
        self.files[path] = data

    def read_bytes(self, path: str) -> bytes:
        return self.files[self._norm(path)]

    def replace(self, src: str, dst: str) -> None:
        src, dst = self._norm(src), self._norm(dst)
        if src in self.fail_on_replace:
            raise OSError(f"injizierter Replace-Fehler: {src}")
        self.files[dst] = self.files.pop(src)

    def remove(self, path: str) -> None:
        path = self._norm(path)
        if path in self.fail_on_remove:
            raise OSError(f"injizierter Remove-Fehler: {path}")
        self.files.pop(path, None)

    def listdir(self, dirpath: str) -> list[str]:
        prefix = self._norm(dirpath).rstrip("/") + "/"
        return sorted(p[len(prefix):] for p in self.files if p.startswith(prefix))

    def makedirs(self, dirpath: str) -> None:
        return None


def existing_targets(fs, target_dir: str, names) -> list[str]:
    """Vorhandene Zieldateien (Gross-/Kleinschreibung als Konflikt)."""
    present = {name.lower(): name for name in fs.listdir(target_dir)}
    return [present[n.lower()] for n in names if n.lower() in present]


def clean_orphans(fs, target_dir: str) -> list[str]:
    removed = []
    for name in fs.listdir(target_dir):
        if name.startswith(ATTEMPT_PREFIX) or REPLACED_INFIX in name:
            fs.remove(join(target_dir, name))
            removed.append(name)
    return removed


def write_set(fs, target_dir: str, files: dict, expected_hashes: dict,
              attempt_id: str, replace: bool = False) -> dict:
    """Schreibt den vollstaendigen Set atomar. Wirft :class:`SetWriteError`."""
    for name in files:
        ok, reason = safe_file_name(name)
        if not ok:
            raise SetWriteError("unsafe_file_name", [f"{name}:{reason}"])

    conflicts = existing_targets(fs, target_dir, list(files))
    if conflicts and not replace:
        raise SetWriteError("target_conflict", conflicts)

    fs.makedirs(target_dir)
    tmp_paths = {name: join(target_dir, f"{ATTEMPT_PREFIX}{attempt_id}-{name}") for name in files}

    written_tmp: list[str] = []
    try:
        for name, data in files.items():
            fs.write_bytes(tmp_paths[name], data)
            written_tmp.append(tmp_paths[name])
    except OSError as exc:
        for path in written_tmp:
            with contextlib.suppress(OSError):
                fs.remove(path)
        raise SetWriteError("schreibfehler", [str(exc)]) from exc

    committed: list[tuple[str, str | None]] = []
    try:
        for name in files:
            final = join(target_dir, name)
            backup = None
            if fs.exists(final):
                backup = join(target_dir, f"{name}{REPLACED_INFIX}{attempt_id}")
                fs.replace(final, backup)
            fs.replace(tmp_paths[name], final)
            committed.append((name, backup))
        actual_hashes = {}
        for name in files:
            actual = hashlib.sha256(fs.read_bytes(join(target_dir, name))).hexdigest()
            actual_hashes[name] = actual
            if actual != expected_hashes.get(name):
                raise SetWriteError("hash_mismatch", [name])
    except SetWriteError:
        _rollback(fs, target_dir, committed, files)
        raise
    except OSError as exc:
        _rollback(fs, target_dir, committed, files)
        raise SetWriteError("schreibfehler", [str(exc)]) from exc

    return {
        "ok": True,
        "written": sorted(files),
        "hashes": actual_hashes,
        "backups": [(name, backup) for name, backup in committed],
    }


def revert_set(fs, target_dir: str, written, backups) -> None:
    """Kompensiert einen geschriebenen, aber nicht autoritativ bestaetigten Set.

    Entfernt die neuen Enddateien und stellt ersetzte Vorsaetze aus den
    Backups wieder her — nach einem gewinnenden ``canceled`` entsteht kein
    Set (Export Result Contract).
    """
    backup_map = dict(backups or ())
    for name in reversed(list(written)):
        with contextlib.suppress(OSError):
            fs.remove(join(target_dir, name))
        backup = backup_map.get(name)
        if backup is not None:
            with contextlib.suppress(OSError):
                fs.replace(backup, join(target_dir, name))


def _rollback(fs, target_dir: str, committed: list, files: dict) -> None:
    for name, backup in reversed(committed):
        with contextlib.suppress(OSError):
            fs.remove(join(target_dir, name))
        if backup is not None:
            with contextlib.suppress(OSError):
                fs.replace(backup, join(target_dir, name))
    # Verbliebene Attempt-Tempdateien dieses Sets entfernen.
    for entry in fs.listdir(target_dir):
        if entry.startswith(ATTEMPT_PREFIX):
            with contextlib.suppress(OSError):
                fs.remove(join(target_dir, entry))
