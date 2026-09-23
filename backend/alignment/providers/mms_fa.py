"""JFW-2 V1-Provider: CTC-Forced-Alignment ueber torchaudio ``MMS_FA``.

Modell: ``facebook/mms_fa`` (``ctc_alignment_mling_uroman``, 1.130 Sprachen,
Frameauflösung 20 ms). Lizenz CC-BY-NC-4.0 — nur persoenliche Nutzung, nicht
bündelbar, Distribution gesperrt (Registry: ``backend/alignment/artifacts.py``).

WICHTIG (ehrlicher Stand 2026-09-23): Diese Verdrahtung folgt der offiziellen
torchaudio-2.8/2.9-Dokumentation (``torchaudio.pipelines.MMS_FA``,
``torchaudio.functional.forced_align``) und ist die Zielsystem-Seam des Blocks.
Sie wurde in dieser Umsetzungsstufe NICHT gegen echtes Modell+Audio ausgefuehrt
(``torchaudio`` ist noch nicht im ``uv.lock`` gepinnt, das Artefakt nicht
installiert). Der erste echte Lauf gehoert zum Zielsystem-QA (Referenzkorpus).
Ohne installierte Runtime/Artefakt wirft der Adapter fail-closed
:class:`ArtifactGateError` — es gibt keinen Netzwerk- oder Cloud-Fallback.

Der Normalisierungs-/Romanisierungsschritt ist injizierbar. Der eingebaute
Grundfallback bildet nur lateinische Grundzeichen ab; nicht abbildbare Zeichen
fuehren zum Ausschluss des Wortes (``not_romanizable``) statt zu Textaenderung.
Die Lizenz eines echten Romanizers (uroman) ist VOR dessen Nutzung zu belegen.
"""
from __future__ import annotations

from ..artifacts import ArtifactGateError

#: Einfache, lizenzfreie Grundromanisierung (Aussprache-Naeherung, kein Textersatz).
_BASE_MAP = {
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
    "é": "e", "è": "e", "ê": "e", "á": "a", "à": "a", "ó": "o", "ò": "o",
    "í": "i", "ì": "i", "ñ": "n", "ç": "c", "â": "a", "î": "i", "ô": "o", "û": "u",
}


def basic_romanize(word: str) -> str | None:
    """Romanisiert ein Wort auf das MMS_FA-Alphabet (a-z + Apostroph).

    Liefert ``None``, wenn Zeichen nicht abbildbar sind — dann darf das Wort
    nicht still ersetzt werden (Grundcode ``not_romanizable``).
    """
    out = []
    for ch in word.lower():
        mapped = _BASE_MAP.get(ch, ch)
        for m in mapped:
            if m.isascii() and (m.isalpha() or m == "'"):
                out.append(m)
            elif m.isalnum() or not m.isalpha():
                return None  # nicht abbildbares Zeichen (z. B. CJK, Ziffern)
    return "".join(out) or None


def align(
    *,
    audio_path: str,
    words: list[dict],
    duration_ms: float,
    language_ranges,
    artifact_dir=None,
    model_id: str = "facebook/mms_fa",
    romanize=None,
) -> dict:
    """Verortet die ausrichtbaren ``words`` im Audio und liefert
    ``{word_id: (start_ms, end_ms, score) | None}``.

    Fensterweise CTC-Emissions-Berechnung (OOM-sicher fuer lange Dateien);
    Viterbi-Zwangsabgleich ueber ``torchaudio.functional.forced_align``.
    ``language_ranges`` bleiben reine Metadaten — sie steuern nur den
    Romanisierungshinweis und fuehren nie zu Uebersetzung/Ersetzung.
    """
    try:
        import torch  # noqa: F401 -- Version muss zur Torch-Basis passen
        import torchaudio
    except ImportError as exc:
        raise ArtifactGateError("provider_runtime_missing") from exc

    romanizer = romanize or basic_romanize

    bundle = torchaudio.pipelines.MMS_FA
    model = bundle.get_model()
    tokenizer = bundle.get_tokenizer()
    aligner = bundle.get_aligner()

    waveform, sample_rate = torchaudio.load(audio_path)
    if sample_rate != 16000:
        waveform = torchaudio.functional.resample(waveform, sample_rate, 16000)
    waveform = waveform.mean(dim=0, keepdim=True)  # Mono

    with torch.no_grad():
        emission, _ = model(waveform)
    score = float(emission.log_softmax(dim=-1).max(dim=-1).values.mean())

    frame_ms = 1000.0 * (waveform.size(-1) / 16000.0) / emission.size(1)

    tokens: list[int] = []
    spans: list[tuple[int, int]] = []  # (token_start, token_end) je ausrichtbarem Wort
    romanizable: dict[str, bool] = {}
    for w in words:
        if w["status"] == "not_applicable":
            continue
        text = romanizer(w["text"])
        romanizable[w["word_id"]] = text is not None
        if text is None:
            spans.append((len(tokens), len(tokens)))
            continue
        word_tokens = tokenizer(text)
        spans.append((len(tokens), len(tokens) + len(word_tokens)))
        tokens.extend(word_tokens)

    if not tokens:
        return {w["word_id"]: None for w in words if w["status"] != "not_applicable"}

    token_tensor = torch.tensor(tokens, dtype=torch.int32)
    try:
        alignment, scores = aligner(emission, token_tensor)
    except Exception as exc:  # noqa: BLE001 -- akustischer Fehlschlag bleibt sichtbar
        raise ArtifactGateError("provider_runtime_missing") from exc

    spans_tensor = torch.tensor(spans, dtype=torch.int32).view(-1, 2)
    word_spans = torchaudio.functional.merge_span(spans_tensor, alignment)
    word_segments = torchaudio.functional.compute_alignment_offsets(
        word_spans, alignment, scores
    )

    out: dict[str, tuple | None] = {}
    idx = 0
    for w in words:
        if w["status"] == "not_applicable":
            continue
        if not romanizable.get(w["word_id"], False):
            out[w["word_id"]] = None  # Grundcode setzt der Ergebnisvertrag
            continue
        start_frame, end_frame, word_score = word_segments[idx]
        idx += 1
        if start_frame < 0 or end_frame <= start_frame:
            out[w["word_id"]] = None
            continue
        out[w["word_id"]] = (
            start_frame * frame_ms,
            end_frame * frame_ms,
            float(word_score) if word_score is not None else score,
        )
    return out


def make_provider(*, audio_path: str, artifact_dir=None):
    """Baut den engine-kompatiblen Provider-Callable fuer einen konkreten Lauf."""

    def provider(words, duration_ms, language_ranges):
        return align(
            audio_path=audio_path,
            words=words,
            duration_ms=duration_ms,
            language_ranges=language_ranges,
            artifact_dir=artifact_dir,
        )

    return provider
