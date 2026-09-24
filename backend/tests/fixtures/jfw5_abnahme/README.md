# JFW-5-Abnahme-Korpus `jfw5-abnahme-korpus-v1`

Gefrorener, versionierter Testsatz fuer den Einzel-vs.-Batch-Vergleich der
Batch-Verarbeitung (Spec `features/JFW-5-batch-verarbeitung.md`, AC L139/L142;
Evidenz `features/evidence/JFW-5-zielsystem-qa.md`).

## Inhalt

| Datei | Zweck |
|---|---|
| `quellen/*.wav` | 8 echte RIFF/WAVE-PCM16-Quellen — das Format, das die Batch-Pipeline real verarbeitet (unterstuetzte Mediendateien laut `backend/batch/discovery.py`) |
| `MANIFEST.json` | Korpus-Revision, Markerplan (synthetische Referenz), SHA-256 und Groesse je Quelle |
| `generate_korpus.py` | deterministischer Generator (nur stdlib); `--verify` reproduziert alle Bytes bitidentisch und prueft gegen das Manifest |

## Regeln dieses Satzes

* **Echt verarbeitbar, inhaltsfrei:** digitale Rechteck-Marker auf digitaler
  Stille — keine Sprache, keine echten Aufnahmen, kein Personenbezug
  (Abgrenzung wie im JFW-11-Capture-Spike: generierte Marker, keine Gespraeche).
* **Gefroren:** Quellen und Manifest sind versioniert; jede Inhaltsaenderung ist
  eine neue, nachvollziehbare Korpus-Revision (neue `korpus_revision` im
  Manifest), nie ein stiller Umschrieb.
* **Belegbar:** `python generate_korpus.py --verify` muss Exit 0 liefern; der
  Vergleichslauf prueft SHA-256 und Groesse jeder Quelle fail-closed gegen
  `MANIFEST.json`, bevor er verarbeitet.
* **Markerplan = synthetische Referenz:** die akustischen Modellaueufe
  (JFW-7/JFW-2/JFW-3) sind Zielsystem-Seams; der Plan definiert die
  synthetische Soll-Belegung jeder Quelle (Marker mit Zeitmarken, eine Quelle
  mit bewusst unscharfem Marker fuer Teilqualitaet), aus der die Seams ihre
  inhaltsfreien Ergebnis-Payloads ableiten.

## Nutzung

```
cd backend/tests/fixtures/jfw5_abnahme
python generate_korpus.py --verify        # Korpus-Integritaet (Exit 0 = intakt)

# Vergleichslauf (aus application/):
backend/.venv/Scripts/python.exe -m pytest backend/tests/test_einzel_vs_batch_abnahme.py -s
```
