"""JFW-11: Zeitmodell ``qpc_offset_drift_v1`` (I/O-frei).

Gemeinsame Zeitbasis ist QPC (100-ns-Einheiten) je Paket. Das Zeitmodell je
Spur/Abschnitt ist ``offset + frame * frame_to_100ns`` — Offset **plus linearer
Driftterm** aus der Regression Geräteframeposition (bzw. kumulierte Samples bei
Prozess-Loopback, dort meldet das virtuelle Geraet ``devicePosition = 0``)
gegen die Paket-QPC-Werte.

Beleg fuer die Pflicht des Drift-Terms:
``features/evidence/JFW-11-capture-spike-report.md`` §9.1 — Offset-only verfehlt
die JFW-11-Zielwerte ueber 45 min (Median 30,7 ms), mit Drift-Term 0,326 ms.
"""
from __future__ import annotations

from dataclasses import dataclass, field

TIME_MODEL_VERSION = "qpc_offset_drift_v1"
QUALITY_OK = "sync_ok"
QUALITY_UNCERTAIN = "sync_unsicher"

#: JFW-11-Zielwerte fuer den absoluten Zuordnungsfehler (Spec, Quellentrennung).
TARGET_MEDIAN_MS = 20.0
TARGET_P95_MS = 50.0
TARGET_MAX_MS = 100.0


@dataclass(frozen=True)
class PacketRecord:
    """Ein Journal-Paket (``journal.csv``) des Capture-Helfers."""

    seq: int
    qpc_100ns: int
    devpos_frames: int | None
    frames: int
    bytes: int = 0
    flags: int = 0
    callback_ts: float | None = None


def is_real_packet(packet: PacketRecord) -> bool:
    """Filtert die leeren NAudio-Schatten-Callbacks (bytes=0, qpc=0, devpos=0).

    Befund Spike §4.1: rund die Haelfte der ``DataAvailable``-Callbacks ist leer
    und darf nicht als Daten gewertet werden.
    """
    return packet.bytes > 0 and packet.frames > 0


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = (len(ordered) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _lsq(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Kleinste-Quadrate-Gerade y = a + b*x; ``None`` bei degenerierter Lage."""
    n = len(points)
    if n < 2:
        return None
    sum_x = sum(x for x, _ in points)
    sum_y = sum(y for _, y in points)
    sum_xx = sum(x * x for x, _ in points)
    sum_xy = sum(x * y for x, y in points)
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return None
    b = (n * sum_xy - sum_x * sum_y) / denom
    a = (sum_y - b * sum_x) / n
    return a, b


@dataclass(frozen=True)
class TrackTimeModel:
    """Versionierte Zeitabbildung einer Spur/eines Abschnitts auf die QPC-Basis."""

    version: str
    rate_hz: int
    offset_100ns: float
    frame_to_100ns: float
    drift: float
    residual_max_ms: float
    sample_count: int
    quality: str
    reason_code: str | None = None
    gaps: tuple[tuple[int, int], ...] = field(default_factory=tuple)

    @property
    def drift_ppm(self) -> float:
        return self.drift * 1e6

    def to_shared_100ns(self, frame: float) -> float:
        return self.offset_100ns + frame * self.frame_to_100ns

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "rate_hz": self.rate_hz,
            "offset_100ns": self.offset_100ns,
            "frame_to_100ns": self.frame_to_100ns,
            "drift": self.drift,
            "drift_ppm": self.drift_ppm,
            "residual_max_ms": self.residual_max_ms,
            "sample_count": self.sample_count,
            "quality": self.quality,
            "reason_code": self.reason_code,
            "gaps": [list(g) for g in self.gaps],
        }


def fit_track_model(
    packets: list[PacketRecord],
    rate_hz: int,
    *,
    min_packets: int = 2,
    max_residual_ms: float = 5.0,
    gaps: tuple[tuple[int, int], ...] = (),
) -> TrackTimeModel:
    """Regession Frames ↔ QPC: Offset + linearer Driftterm (``qpc_offset_drift_v1``).

    Die Geräte-Zeitachse nutzt die Geräteframeposition, soweit vorhanden; bei
    Prozess-Loopback (``devicePosition = 0``, Spike §10.4) wird sie aus den
    kumulierten Samples gebildet.
    """
    real = [p for p in packets if is_real_packet(p)]
    points: list[tuple[float, float]] = []
    cumulative = 0
    use_devpos = any((p.devpos_frames or 0) > 0 for p in real)
    for p in real:
        frame = float(p.devpos_frames) if (use_devpos and p.devpos_frames) else float(cumulative)
        points.append((frame, float(p.qpc_100ns)))
        cumulative += p.frames

    nominal = 1e7 / rate_hz if rate_hz else 0.0
    fit = _lsq(points)
    if fit is None or len(real) < min_packets:
        return TrackTimeModel(
            version=TIME_MODEL_VERSION,
            rate_hz=rate_hz,
            offset_100ns=points[0][1] if points else 0.0,
            frame_to_100ns=nominal,
            drift=0.0,
            residual_max_ms=0.0,
            sample_count=len(real),
            quality=QUALITY_UNCERTAIN,
            reason_code="zu_wenig_pakete" if len(real) < min_packets else "degenerierte_regression",
            gaps=gaps,
        )
    a, b = fit
    residual_max = max(abs(y - (a + b * x)) for x, y in points) / 1e4
    drift = (b / nominal - 1.0) if nominal else 0.0
    if residual_max > max_residual_ms:
        quality, reason = QUALITY_UNCERTAIN, "restfehler_zu_gross"
    else:
        quality, reason = QUALITY_OK, None
    return TrackTimeModel(
        version=TIME_MODEL_VERSION,
        rate_hz=rate_hz,
        offset_100ns=a,
        frame_to_100ns=b,
        drift=drift,
        residual_max_ms=residual_max,
        sample_count=len(real),
        quality=quality,
        reason_code=reason,
        gaps=gaps,
    )


@dataclass(frozen=True)
class PairAlignment:
    """Zuordnungsfehler zweier Spuren fuer Referenzereignis-Paare.

    Der Fehler je Paar ist der Betrag des Restwerts nach dem deklarierten Modell
    (nur Offset oder Offset + Drift) — dieselbe Messgroesse wie im Spike §1.2.
    """

    include_drift: bool
    offset_100ns: float
    slope: float
    residuals_ms: tuple[float, ...]
    quality: str
    reason_code: str | None = None

    def summary_ms(self) -> dict:
        vals = [abs(r) for r in self.residuals_ms]
        return {
            "median": _percentile(vals, 0.5),
            "p95": _percentile(vals, 0.95),
            "max": max(vals) if vals else 0.0,
        }

    def meets_targets(self) -> bool:
        s = self.summary_ms()
        return (
            self.quality == QUALITY_OK
            and s["median"] <= TARGET_MEDIAN_MS
            and s["p95"] <= TARGET_P95_MS
            and s["max"] <= TARGET_MAX_MS
        )


def fit_pair_alignment(
    samples: list[tuple[float, float]],
    *,
    include_drift: bool,
    min_pairs: int = 2,
) -> PairAlignment:
    """``samples`` = Paare ``(t_a_100ns, t_b_100ns)`` gemeinsamer Ereignisse.

    Modell: ``t_b - t_a = offset (+ slope * t_a)``. Ohne Drift-Term ist nur der
    mittlere Pfadversatz modelliert (verfehlt die Zielwerte ueber 45 min).
    """
    xs = [a for a, _ in samples]
    deltas = [b - a for a, b in samples]
    if include_drift and len(samples) >= 2:
        fit = _lsq(list(zip(xs, deltas, strict=True)))
    else:
        fit = None
    if fit is not None:
        offset, slope = fit
    elif deltas:
        offset, slope = sum(deltas) / len(deltas), 0.0
    else:
        offset, slope = 0.0, 0.0
    residuals = tuple(
        (d - (offset + slope * x)) / 1e4 for x, d in zip(xs, deltas, strict=True)
    )
    if len(samples) < min_pairs:
        quality, reason = QUALITY_UNCERTAIN, "zu_wenig_paare"
    elif include_drift and fit is None:
        quality, reason = QUALITY_UNCERTAIN, "degenerierte_regression"
    else:
        quality, reason = QUALITY_OK, None
    return PairAlignment(
        include_drift=include_drift,
        offset_100ns=offset,
        slope=slope,
        residuals_ms=residuals,
        quality=quality,
        reason_code=reason,
    )
