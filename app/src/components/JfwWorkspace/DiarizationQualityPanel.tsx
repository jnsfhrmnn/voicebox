import { useState } from 'react';
import { jfwApi, type DiarizationSummary } from '@/lib/api/jfwApi';
import { JFW_FOCUS_RING, JfwButton, Row, SectionCard, StateChip, StatePreview } from './JfwCommon';
import { JFW_PROGRESS_POLL_MS, JfwProgress, type ProgressPhase } from './JfwProgress';
import { useQuery } from '@tanstack/react-query';

/**
 * JFW-3: Qualitaetsansicht mit Sprecheranzahl-Vorgabe und UI-Fortschritt.
 *
 * Die Vorgabe ist fail-closed: `auto`, `exakt` (1–8) oder `Bereich` (min ≤ max,
 * beide 1–8). Die Qualitaetsansicht zeigt Ergebniszustand, neutrale
 * Cluster-Kennungen (keine Identitaetsbehauptung), Ueberlappungs- und
 * Abdeckungshinweise sowie reale Zaehler — nie nur farblich codiert.
 */

type SpeakerMode = 'auto' | 'exact' | 'range';

export type DiarizationViewState =
  | 'wartend'
  | 'laeuft'
  | 'diarized'
  | 'partially_diarized'
  | 'no_speech'
  | 'failed';

const VIEW_STATES: readonly DiarizationViewState[] = [
  'wartend',
  'laeuft',
  'diarized',
  'partially_diarized',
  'no_speech',
  'failed',
];

const STATE_META: Record<
  DiarizationViewState,
  { label: string; tone: 'neutral' | 'ok' | 'warn' | 'error' | 'active'; icon: string }
> = {
  wartend: { label: 'Wartet auf Start', tone: 'neutral', icon: '○' },
  laeuft: { label: 'Diarisierung läuft', tone: 'active', icon: '◐' },
  diarized: { label: 'Sprecher zugeordnet (diarized)', tone: 'ok', icon: '✓' },
  partially_diarized: {
    label: 'Teilweise zugeordnet (partially_diarized) — sichtbar markiert',
    tone: 'warn',
    icon: '◑',
  },
  no_speech: { label: 'Keine Sprache erkannt (no_speech)', tone: 'neutral', icon: '○' },
  failed: { label: 'Fehlgeschlagen (failed)', tone: 'error', icon: '✕' },
};

function toProgressPhase(state: DiarizationViewState): ProgressPhase {
  if (state === 'laeuft') return 'laufend';
  if (state === 'failed') return 'fehlgeschlagen';
  if (state === 'diarized') return 'abgeschlossen';
  if (state === 'no_speech' || state === 'partially_diarized') return 'abgeschlossen';
  return 'bereit';
}

export function DiarizationQualityPanel() {
  const [mode, setMode] = useState<SpeakerMode>('auto');
  const [exact, setExact] = useState(2);
  const [min, setMin] = useState(1);
  const [max, setMax] = useState(4);
  const [preview, setPreview] = useState<DiarizationViewState>('laeuft');
  const [identityHash, setIdentityHash] = useState<string | null>(null);
  const [specError, setSpecError] = useState<string | null>(null);

  // Live-Status: Fortschrittsanzeige mit Polling <= 2 s (JFW_PROGRESS_POLL_MS).
  const live = useQuery({
    queryKey: ['jfw3-diarization', identityHash],
    queryFn: () => jfwApi.getDiarization(identityHash as string),
    enabled: Boolean(identityHash),
    refetchInterval: JFW_PROGRESS_POLL_MS,
  });

  const specValid = (): string | null => {
    if (mode === 'exact' && (exact < 1 || exact > 8)) return 'Exakte Vorgabe nur 1–8 Sprecher.';
    if (mode === 'range') {
      if (min < 1 || max > 8) return 'Bereich nur 1–8 Sprecher.';
      if (min > max) return 'Minimum ≤ Maximum (fail-closed).';
    }
    return null;
  };

  const submitSpec = () => {
    const error = specValid();
    setSpecError(error);
    if (error) return;
    // Vorgabe wird unveraendert an den Vertrag uebergeben (SpeakerSpec).
    // Ein echter Lauf startet erst mit den gebundenen Eingangsrevisionen.
  };

  const liveState: DiarizationSummary | undefined = live.data;
  const viewState: DiarizationViewState =
    liveState && !live.isError
      ? ((liveState.status as DiarizationViewState) ?? preview)
      : preview;

  const specSummary =
    mode === 'auto'
      ? 'auto (keine Vorgabe)'
      : mode === 'exact'
        ? `exakt: ${exact} Sprecher`
        : `Bereich: ${min}–${max} Sprecher`;

  return (
    <div className="space-y-4">
      <StatePreview
        label="Zustandsvorschau:"
        states={VIEW_STATES}
        active={preview}
        onChange={setPreview}
        testId="diarization-state-preview"
      />

      <SectionCard
        title="Sprecheranzahl-Vorgabe"
        description="Fail-closed: auto, exakt (1–8) oder Bereich (1–8, min ≤ max). Keine stillen Defaults nach dem Start."
        testId="speaker-spec-card"
      >
        <fieldset className="space-y-2">
          <legend className="text-sm font-medium mb-1">Modus</legend>
          {(
            [
              ['auto', 'Auto — Modell entscheidet (Sichtbehauptung: ≥ 90 % exakt ist Zielsystemmessung)'],
              ['exact', 'Exakt — eine feste Sprecheranzahl (1–8)'],
              ['range', 'Bereich — Minimum und Maximum (1–8)'],
            ] as const
          ).map(([value, label]) => (
            <label
              key={value}
              className={cnLabel(value === mode)}
            >
              <input
                type="radio"
                name="speaker-mode"
                value={value}
                checked={mode === value}
                onChange={() => setMode(value)}
                className={JFW_FOCUS_RING}
              />
              <span>{label}</span>
            </label>
          ))}
        </fieldset>

        {mode === 'exact' && (
          <label className="flex items-center gap-2 text-sm">
            Sprecheranzahl:
            <input
              type="number"
              min={1}
              max={8}
              value={exact}
              onChange={(e) => setExact(Number(e.target.value))}
              aria-label="Exakte Sprecheranzahl (1–8)"
              className={cnInput()}
            />
          </label>
        )}
        {mode === 'range' && (
          <div className="flex items-center gap-4 text-sm">
            <label className="flex items-center gap-2">
              Minimum:
              <input
                type="number"
                min={1}
                max={8}
                value={min}
                onChange={(e) => setMin(Number(e.target.value))}
                aria-label="Minimale Sprecheranzahl (1–8)"
                className={cnInput()}
              />
            </label>
            <label className="flex items-center gap-2">
              Maximum:
              <input
                type="number"
                min={1}
                max={8}
                value={max}
                onChange={(e) => setMax(Number(e.target.value))}
                aria-label="Maximale Sprecheranzahl (1–8)"
                className={cnInput()}
              />
            </label>
          </div>
        )}

        {specError && (
          <p role="alert" className="text-sm text-red-600 dark:text-red-300" data-testid="spec-error">
            Vorgabe abgelehnt (fail-closed): {specError}
          </p>
        )}
        <dl>
          <Row label="Wirksame Vorgabe">{specSummary}</Row>
        </dl>
        <JfwButton variant="primary" testId="submit-speaker-spec" onClick={submitSpec}>
          Vorgabe übernehmen
        </JfwButton>
      </SectionCard>

      <SectionCard
        title="UI-Fortschritt (≤ 2 s)"
        description="Statusabfrage und Abbruch laufen ueber /diarization/{identity_hash} bzw. /cancel."
        testId="diarization-progress-card"
      >
        <label className="flex items-center gap-2 text-sm">
          Auftrags-Identität (identity_hash):
          <input
            type="text"
            value={identityHash ?? ''}
            onChange={(e) => setIdentityHash(e.target.value || null)}
            placeholder="identity_hash des Diarisierungslaufs"
            aria-label="Identity-Hash des Diarisierungslaufs"
            className={`flex-1 rounded-md border border-border bg-background px-2 py-1 ${JFW_FOCUS_RING}`}
            data-testid="diarization-identity-input"
          />
        </label>
        <JfwProgress
          title="JFW-3 Diarisierung"
          phase={toProgressPhase(viewState)}
          detail={
            liveState
              ? `status=${String(liveState.status)}${liveState.reason_code ? ` · reason_code=${String(liveState.reason_code)}` : ''}`
              : 'Kein Auftrag gebunden — Zustandsvorschau aktiv.'
          }
          lastUpdatedAtMs={live.dataUpdatedAt || null}
          onCancel={() => identityHash && jfwApi.cancelDiarization(identityHash).catch(() => undefined)}
        />
      </SectionCard>

      <SectionCard
        title="Qualitätsansicht"
        description="Ergebniszustand, neutrale Cluster-Kennungen, Ueberlappung und Abdeckung — nie nur farblich."
        testId="quality-view-card"
      >
        <div className="flex flex-wrap items-center gap-2">
          <StateChip tone={STATE_META[viewState].tone} icon={STATE_META[viewState].icon}>
            {STATE_META[viewState].label}
          </StateChip>
          <StateChip tone="neutral" icon="◫">
            Vorgabe: {specSummary}
          </StateChip>
        </div>
        <dl>
          <Row label="Zustände (Vertrag)">
            diarized · partially_diarized · no_speech · failed (vertraglich unabänderlich, Wörter bleiben unverändert)
          </Row>
          <Row label="Cluster-Kennungen">
            neutral und deterministisch (Sprecher 1…n) — keine Personen- oder Identitätsbehauptung
          </Row>
          <Row label="Ueberlappung / Abdeckung">
            overlap_zaehler{' '}
            {String((liveState?.quality as Record<string, unknown> | undefined)?.overlap_count ?? '—')} ·
            Abdeckung{' '}
            {String((liveState?.quality as Record<string, unknown> | undefined)?.coverage ?? '—')}
          </Row>
          <Row label="Nicht ausgerichtete Wörter">
            werden als sichtbares Ergebnis geführt (`unaligned`), nie als falsche Grenze gewertet
          </Row>
        </dl>
        <p className="text-xs text-muted-foreground">
          Abnahme-Gate DER ≤ 15 % bleibt Zielsystem-Korpus-Aufgabe und wird hier nicht als Erfolg gewertet.
        </p>
      </SectionCard>
    </div>
  );
}

function cnLabel(active: boolean): string {
  return `flex items-start gap-2 rounded-md border px-3 py-2 text-sm cursor-pointer ${
    active ? 'border-accent bg-accent/5' : 'border-border hover:bg-muted'
  } ${JFW_FOCUS_RING}`;
}

function cnInput(): string {
  return `w-20 rounded-md border border-border bg-background px-2 py-1 ${JFW_FOCUS_RING}`;
}
