import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { jfwApi } from '@/lib/api/jfwApi';
import { JFW_FOCUS_RING, JfwButton, Row, SectionCard, StateChip, StatePreview } from './JfwCommon';
import { JFW_PROGRESS_POLL_MS, JfwProgress, type ProgressPhase } from './JfwProgress';
import { ModelInstallOffer, ModelMissingNotice, type ModelInstallCandidate } from './ModelInstallOffer';

/**
 * JFW-2/JFW-7: Fortschrittsanzeige ≤ 2 s (Alignment- und Diktat-Lauf) und
 * Installationsangebot fuer fehlende Modellartefakte.
 *
 * Das Fehlen eines Artefakts blockiert ausschliesslich den jeweiligen Lauf;
 * es gibt nie einen automatischen Netzwerkzugriff. Die Bedienbarkeit bleibt
 * waehrend langer CPU-Runs erhalten (Abbruch jederzeit, Navigation aktiv).
 */

const DICTATION_MODEL: ModelInstallCandidate = {
  name: 'openai/whisper-large-v3-turbo',
  feature: 'JFW-7 Diktat (Basis-Transkription)',
  source: 'Hugging Face (openai/whisper-large-v3-turbo), Modellregister jfw7-dictate-v1',
  revision: 'immutable Modellrevision (40-hex) laut Snapshot-Vertrag',
  license: 'MIT (Base large-v3: Apache-2.0) — permissiv, Distributionsrecht belegt',
  download: { size: 'laut Modellkarte (~809 M Parameter)', from: 'HF-Repo (nutzerinitiiert)', to: 'lokaler Modell-Cache' },
};

const ALIGN_MODEL: ModelInstallCandidate = {
  name: 'uroman (Transliteration) + Alignment-Artefakte',
  feature: 'JFW-2 präzise Wort-Zeitstempel',
  source: 'Artefaktmanifest JFW-2 (Repo + 40-Hex-Revision, Datei-SHA-256)',
  revision: 'immutable Revision laut Manifest (fail-closed bei Abweichung)',
  license: 'Lizenzbeleg im Modellregister (fail-closed: unbekannte Lizenz blockiert)',
};

export type DictationViewState =
  | 'waiting_for_model'
  | 'waiting_for_backend'
  | 'queued'
  | 'transcribing'
  | 'raw_ready'
  | 'committed'
  | 'no_speech'
  | 'failed';

const VIEW_STATES: readonly DictationViewState[] = [
  'waiting_for_model',
  'waiting_for_backend',
  'queued',
  'transcribing',
  'raw_ready',
  'committed',
  'no_speech',
  'failed',
];

const STATE_META: Record<
  DictationViewState,
  { label: string; tone: 'neutral' | 'ok' | 'warn' | 'error' | 'active'; icon: string }
> = {
  waiting_for_model: { label: 'Wartet auf Modell — getrenntes Installationsangebot, kein Download', tone: 'warn', icon: '⚠' },
  waiting_for_backend: { label: 'Wartet auf Backend — kein stiller Fallback', tone: 'warn', icon: '⚠' },
  queued: { label: 'In Warteschlange', tone: 'neutral', icon: '○' },
  transcribing: { label: 'Transkription läuft (Rohtext unverändert)', tone: 'active', icon: '◐' },
  raw_ready: { label: 'Rohtext bereit (unveränderliche Raw-Revision)', tone: 'ok', icon: '✓' },
  committed: { label: 'Autoritative Ergebnisrevision gespeichert', tone: 'ok', icon: '✓' },
  no_speech: { label: 'Kein Sprachinhalt (no_speech) — kein leerer Erfolgstext', tone: 'neutral', icon: '○' },
  failed: { label: 'Fehlgeschlagen — kontrolliert erneut startbar, kein Fallback-Text', tone: 'error', icon: '✕' },
};

export function DictationAlignmentPanel() {
  const [preview, setPreview] = useState<DictationViewState>('waiting_for_model');
  const [installOpen, setInstallOpen] = useState(false);
  const [installTarget, setInstallTarget] = useState<ModelInstallCandidate>(DICTATION_MODEL);
  const [dictationKey, setDictationKey] = useState<string | null>(null);
  const [alignmentKey, setAlignmentKey] = useState<string | null>(null);

  // Live-Status mit Fortschritts-Polling <= 2 s.
  const dictation = useQuery({
    queryKey: ['jfw7-dictation', dictationKey],
    queryFn: () => jfwApi.getDictation(dictationKey as string),
    enabled: Boolean(dictationKey),
    refetchInterval: JFW_PROGRESS_POLL_MS,
  });
  const alignment = useQuery({
    queryKey: ['jfw2-alignment', alignmentKey],
    queryFn: () => jfwApi.getAlignment(alignmentKey as string),
    enabled: Boolean(alignmentKey),
    refetchInterval: JFW_PROGRESS_POLL_MS,
  });

  const progressFor = (active: boolean, failed = false): ProgressPhase =>
    failed ? 'fehlgeschlagen' : active ? 'laufend' : 'bereit';

  return (
    <div className="space-y-4">
      <StatePreview
        label="Zustandsvorschau Diktatlauf:"
        states={VIEW_STATES}
        active={preview}
        onChange={setPreview}
        testId="dictation-state-preview"
      />

      <SectionCard
        title="JFW-7 Diktat-Lauf — Fortschritt (≤ 2 s) und Bedienbarkeit"
        description="Auch bei langen CPU-Runs bleibt die Oberfläche bedienbar; Cancel ist jederzeit möglich, es gibt keinen automatischen CUDA-Wechsel."
        testId="dictation-progress-card"
      >
        <div className="flex flex-wrap items-center gap-2">
          <StateChip tone={STATE_META[preview].tone} icon={STATE_META[preview].icon}>
            {STATE_META[preview].label}
          </StateChip>
        </div>
        <JfwProgress
          title="JFW-7 Diktat (dictation_raw_v1)"
          phase={progressFor(preview === 'transcribing' || preview === 'queued')}
          detail={
            dictation.data
              ? `status=${String(dictation.data.status)}`
              : 'Backend-/Modellbindung fail-closed: unklare Health → Run wartend, kein stiller Fallback.'
          }
          lastUpdatedAtMs={dictation.dataUpdatedAt || null}
          onCancel={() => undefined}
        />
        <div className="grid gap-2 md:grid-cols-2">
          <label className="flex items-center gap-2 text-sm">
            Diktat (identity_hash):
            <input
              type="text"
              value={dictationKey ?? ''}
              onChange={(e) => setDictationKey(e.target.value || null)}
              placeholder="identity_hash des Diktatlaufs"
              aria-label="Identity-Hash des Diktatlaufs"
              className={`min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1 ${JFW_FOCUS_RING}`}
              data-testid="dictation-identity-input"
            />
          </label>
          <label className="flex items-center gap-2 text-sm">
            Alignment (identity_hash):
            <input
              type="text"
              value={alignmentKey ?? ''}
              onChange={(e) => setAlignmentKey(e.target.value || null)}
              placeholder="identity_hash des Alignment-Laufs"
              aria-label="Identity-Hash des Alignment-Laufs"
              className={`min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1 ${JFW_FOCUS_RING}`}
              data-testid="alignment-identity-input"
            />
          </label>
        </div>
        <dl>
          <Row label="Rohtext">stets die unveränderte autoritative `raw_transcript`-Revision (kein LLM/Refine/Personality-Pfad)</Row>
          <Row label="Sprache">Auto-Erkennung ändert keine dauerhafte Einstellung; `translate` ist nicht erreichbar</Row>
        </dl>
      </SectionCard>

      <SectionCard
        title="JFW-2 Alignment — Fortschritt, Abbruch und Abdeckung"
        description="Modus, Status, Abdeckung, Alignment-Version und Hinweis auf nicht ausgerichtete Wörter — erkennbar ohne ausschließlich farbliche Codierung."
        testId="alignment-card"
      >
        <JfwProgress
          title="JFW-2 Wort-Zeitstempel"
          phase={progressFor(preview === 'transcribing' || preview === 'queued')}
          detail="Spätestens nach 2 s belastbarer Fortschritts- oder Aktivitätszustand; Abbruch beendet ausschließlich den Alignment-Attempt."
          lastUpdatedAtMs={alignment.dataUpdatedAt || null}
          onCancel={() => alignmentKey && jfwApi.cancelAlignment(alignmentKey).catch(() => undefined)}
        />
        <dl>
          <Row label="Modus">Wort-Zeitstempel (no-text-change-Gate: Text bleibt zeichengenau unverändert)</Row>
          <Row label="Status / Abdeckung">
            {alignment.data
              ? `status=${String(alignment.data.status)}`
              : 'unaligned bleibt sichtbares Ergebnis, nie eine falsche Grenze'}
          </Row>
          <Row label="Alignment-Version">im Ergebnis gebunden (Coverage-Felder + Provenienz)</Row>
          <Row label="Nicht ausgerichtete Wörter">
            sichtbarer Hinweis mit Symbol ◠ und Text — nicht nur Farbe
          </Row>
        </dl>
        <div className="flex flex-wrap gap-2">
          <JfwButton
            testId="open-dictation-install"
            onClick={() => {
              setInstallTarget(DICTATION_MODEL);
              setInstallOpen(true);
            }}
          >
            Installationsangebot Diktatmodell …
          </JfwButton>
          <JfwButton
            testId="open-alignment-install"
            onClick={() => {
              setInstallTarget(ALIGN_MODEL);
              setInstallOpen(true);
            }}
          >
            Installationsangebot Alignment-Artefakte …
          </JfwButton>
        </div>
      </SectionCard>

      <SectionCard
        title="Fehlende Artefakte — fail-closed"
        description="Kein automatischer Netzwerkzugriff; getrennter Download mit Größe, Quelle, Ziel und ausdrücklicher Bestätigung."
        testId="dictation-missing-card"
      >
        <ModelMissingNotice
          candidate={preview === 'waiting_for_model' ? DICTATION_MODEL : ALIGN_MODEL}
          onOfferInstall={() => {
            setInstallTarget(preview === 'waiting_for_model' ? DICTATION_MODEL : ALIGN_MODEL);
            setInstallOpen(true);
          }}
        />
        <p className={`text-xs text-muted-foreground mt-2 ${JFW_FOCUS_RING}`}>
          Upload-/Retranskription behalten stabile Identitaet; erneuter Lauf = neue STT-Revision
          eigener Provenienz, frühere bleibt erhalten.
        </p>
      </SectionCard>

      <ModelInstallOffer open={installOpen} onOpenChange={setInstallOpen} candidate={installTarget} />
    </div>
  );
}
