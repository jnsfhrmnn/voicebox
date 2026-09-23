"""JFW-2: Wort-/Zeichenvertrag — rein zeichenbasierte Abbildung (No-text-change).

Der Vertrag bildet eine unveraenderte Transkriptrevision auf Token ab:

* Jedes Token ist ein exaktes Substring der Revision mit lueckenlosen,
  monoton steigenden Zeichenpositionen (Rekonstruktion == Originaltext).
* Token ausschliesslich aus Leerraum, Satzzeichen, Symbolen oder Markup sind
  ``not_applicable`` (kein akustisches Signal) und erhalten nie Zeiten.
* Alle uebrigen Token sind ausrichtbar (Status ``alignable`` im Grundvertrag,
  im Ergebnis ``aligned``/``unaligned``). Der Anzeigetext bleibt exakt
  erhalten — auch bei Zahlen, Abkuerzungen, URLs, Pfaden, Code und
  wiederholten Woertern; die Trennung erfolgt ueber ``order`` und
  ``char_start``. Normalisierung/Romanisierung ist ausschliesslich
  Alignment-Metadatum und aendert niemals ``text``.
"""
from __future__ import annotations

#: Statuswerte im Grundvertrag (vor dem Lauf).
STATUS_ALIGNABLE = "alignable"
STATUS_NOT_APPLICABLE = "not_applicable"


def _tokenize(text: str) -> list[tuple[int, int]]:
    """Lueckenlose Zerlegung in Leerraum- und Nicht-Leerraum-Bloecke."""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            j = i
            while j < n and text[j].isspace():
                j += 1
        else:
            j = i
            while j < n and not text[j].isspace():
                j += 1
        spans.append((i, j))
        i = j
    return spans


def _core_span(run: str) -> tuple[int, int]:
    """Kern eines Nicht-Leerraum-Blocks: umgebende Nicht-Alphanumerik abschneiden.

    Interne Zeichen (Punkte in ``1.2.3``, Doppelpunkt/Slash in URLs, Binde-
    striche usw.) bleiben erhalten — nur fuehrende/abschliessende
    Satz-/Symbolzeichen werden eigene ``not_applicable``-Token.
    """
    start = 0
    while start < len(run) and not run[start].isalnum():
        start += 1
    end = len(run)
    while end > start and not run[end - 1].isalnum():
        end -= 1
    return start, end


def build_word_contract(text: str) -> list[dict]:
    """Bildet ``text`` lueckenlos auf den Wortvertrag ab (stabil, deterministisch).

    Jeder Eintrag: ``word_id`` (stabil, ``w-%04d``), ``order``, ``char_start``,
    ``char_end``, ``text`` (exaktes Substring) und ``status`` (``alignable``
    oder ``not_applicable``).
    """
    tokens: list[dict] = []
    order = 0
    for span_start, span_end in _tokenize(text):
        run = text[span_start:span_end]
        if run.isspace():
            parts = [(span_start, span_end, STATUS_NOT_APPLICABLE)]
        else:
            core_s, core_e = _core_span(run)
            parts = []
            if core_s > 0:
                parts.append((span_start, span_start + core_s, STATUS_NOT_APPLICABLE))
            if core_e > core_s:
                parts.append((span_start + core_s, span_start + core_e, STATUS_ALIGNABLE))
            if core_e < len(run):
                parts.append((span_start + core_e, span_end, STATUS_NOT_APPLICABLE))
        for start, end, status in parts:
            tokens.append(
                {
                    "word_id": f"w-{order:04d}",
                    "order": order,
                    "char_start": start,
                    "char_end": end,
                    "text": text[start:end],
                    "status": status,
                }
            )
            order += 1
    return tokens


def verify_no_text_change(text: str, words: list[dict]) -> None:
    """Heiliges Gate: wirft ``ValueError``, sobald die Abbildung den Text nicht
    exakt reproduziert (mutationierte Anzeigetexte, Luecken, Ueberlappungen,
    Positionen ausserhalb des Textes)."""
    pos = 0
    for w in words:
        if w["char_start"] != pos:
            raise ValueError(
                f"Wortvertrag deckt den Text nicht lueckenlos ab (Luecke/Ueberlappung bei {pos})"
            )
        if not (0 <= w["char_start"] <= w["char_end"] <= len(text)):
            raise ValueError(f"Zeichenposition ausserhalb des Textes: {w['word_id']}")
        if w["text"] != text[w["char_start"]:w["char_end"]]:
            raise ValueError(f"Anzeigetext mutiert: {w['word_id']}")
        pos = w["char_end"]
    if pos != len(text):
        raise ValueError("Wortvertrag endet vor dem Textende")
