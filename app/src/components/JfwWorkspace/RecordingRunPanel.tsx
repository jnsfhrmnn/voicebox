import { useCallback, useEffect, useRef, useState } from 'react';
import { PillAudioBars } from '@/components/CapturePill/CapturePill';
import { cn } from '@/lib/utils/cn';
import { jfwApi, type SoundCueMark } from '@/lib/api/jfwApi';
import { useCaptureSettings } from '@/lib/hooks/useSettings';
import { JfwButton, Row, SectionCard, StateChip, StatePreview } from './JfwCommon';

/**
 * JFW-6: Aufnahme-Pill an die RunSession, Sound-Cues ins Run-Manifest,
 * Shift+Shift-Registrierung.
 *
 * Die Pill zeigt den RunSession-Zustand (starting/recording/stopping/secured/
 * failed/canceled) mit Text UND Form — nie nur Farbe — und bleibt waehrend
 * einer aktiven Aufnahme dauerhaft sichtbar (schwebend). Sound-Cues
 * (start_ton/stopp_ton/fehler_ton) werden als `sound_cue_marks` ins
 * Run-Manifest gebunden und sind nie Sprach-/Aufnahme-Evidenz.
 */

export type RunPillState =
  | 'aus'
  | 'nicht_bereit'
  | 'startet'
  | 'aufnahme'
  | 'wird_gesichert'
  | 'gesichert'
  | 'verworfen'
  | 'fehlgeschlagen';

const RUN_PILL_STATES: readonly RunPillState[] = [
  'aus',
  'nicht_bereit',
  'startet',
  'aufnahme',
  'wird_gesichert',
  'gesichert',
  'verworfen',
  'fehlgeschlagen',
];

const PILL_META: Record<
  RunPillState,
  { label: string; icon: string; tone: 'neutral' | 'ok' | 'warn' | 'error' | 'active'; visible: boolean }
> = {
  aus: { label: 'Bereit (kein Run)', icon: '○', tone: 'neutral', visible: false },
  nicht_bereit: { label: 'Nicht bereit — Hotkey nicht registriert', icon: '⊘', tone: 'error', visible: true },
  startet: { label: 'Startet', icon: '◐', tone: 'active', visible: true },
  aufnahme: { label: 'Aufnahme läuft', icon: '●', tone: 'active', visible: true },
  wird_gesichert: { label: 'Wird gesichert', icon: '◔', tone: 'active', visible: true },
  gesichert: { label: 'Gesichert', icon: '✓', tone: 'ok', visible: true },
  verworfen: { label: 'Verworfen', icon: '⊘', tone: 'warn', visible: true },
  fehlgeschlagen: { label: 'Aufnahme fehlgeschlagen', icon: '✕', tone: 'error', visible: true },
};

const CUE_KINDS: readonly SoundCueMark['kind'][] = ['start_ton', 'stopp_ton', 'fehler_ton'];

function randomRunId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  return `jfw6-run-${hex}`;
}

/** Schwebende Pill — waehrend aktiver Aufnahme dauerhaft erkennbar. */
export function RunSessionPill({ state, elapsedMs }: { state: RunPillState; elapsedMs: number }) {
  const meta = PILL_META[state];
  const total = Math.max(0, Math.floor(elapsedMs / 1000));
  const time = `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
  return (
    <div
      data-testid="run-session-pill"
      data-state={state}
      role="status"
      aria-live="assertive"
      aria-label={`Aufnahmezustand: ${meta.label}`}
      className={cn(
        'inline-flex items-center gap-3 px-4 h-10 rounded-full',
        'bg-white/85 ring-1 ring-black/10 shadow-lg backdrop-blur-xl',
        'dark:bg-black/60 dark:ring-0',
        meta.tone === 'error' && 'ring-2 ring-red-500/60',
        meta.tone === 'ok' && 'shadow-[inset_0_0_0_2px_hsl(var(--accent)/0.6)]',
        meta.visible ? 'opacity-100' : 'opacity-0 pointer-events-none',
      )}
    >
      <span aria-hidden="true" className="text-base leading-none w-4 text-center">
        {meta.icon}
      </span>
      <span className="text-sm font-medium shrink-0" style={{ minWidth: '120px' }}>
        {meta.label}
      </span>
      <PillAudioBars
        mode={state === 'aufnahme' ? 'playing' : state === 'startet' || state === 'wird_gesichert' ? 'generating' : 'idle'}
      />
      <span className="text-xs tabular-nums text-accent/80 font-medium shrink-0 -ml-1">
        {time}
      </span>
    </div>
  );
}

export function RecordingRunPanel() {
  const { settings, update } = useCaptureSettings();
  const [preview, setPreview] = useState<RunPillState>('startet');
  const [liveState, setLiveState] = useState<RunPillState>('aus');
  const [elapsedMs, setElapsedMs] = useState(0);
  const [cues, setCues] = useState<SoundCueMark[]>([]);
  const [identityHash, setIdentityHash] = useState<string | null>(null);
  const [lastSummary, setLastSummary] = useState<string | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [hotkeyState, setHotkeyState] = useState<'unbekannt' | 'registriert' | 'nicht_bereit'>(
    'unbekannt',
  );
  const [discardOpen, setDiscardOpen] = useState(false);
  const startedAtRef = useRef<number | null>(null);
  const clockRef = useRef(0);

  // Waehrend der Aufnahme laeuft die Pill-Uhr sichtbar mit.
  useEffect(() => {
    if (liveState !== 'aufnahme' || startedAtRef.current == null) return;
    const timer = window.setInterval(() => {
      setElapsedMs(Date.now() - (startedAtRef.current ?? Date.now()));
    }, 500);
    return () => window.clearInterval(timer);
  }, [liveState]);

  /** Monotone Cue-Uhr in 100-ns-Schritten (Muster `qpc_100ns`). */
  const nextClock = useCallback((spanMs: number) => {
    const start = clockRef.current;
    clockRef.current = start + spanMs * 10000;
    return { start_100ns: start, end_100ns: clockRef.current };
  }, []);

  const pushCue = useCallback(
    (kind: SoundCueMark['kind'], spanMs = 120) => {
      const cueWindow = nextClock(spanMs);
      setCues((prev) => [...prev, { kind, ...cueWindow }]);
    },
    [nextClock],
  );

  const startRun = useCallback(async () => {
    if (hotkeyState === 'nicht_bereit') {
      setLiveState('nicht_bereit');
      return;
    }
    setLiveState('startet');
    setLastError(null);
    const runId = randomRunId();
    try {
      const submitted = await jfwApi.submitRecording({
        run_id: runId,
        device_stable_id_hash: 'pending-open',
        format: { sample_rate: 48000, channels: 1, sample_format: 's16le' },
      });
      setIdentityHash(submitted.identity_hash);
      await jfwApi.beginRecording(submitted.identity_hash);
      pushCue('start_ton');
      startedAtRef.current = Date.now();
      setElapsedMs(0);
      setLiveState('aufnahme');
    } catch (err) {
      pushCue('fehler_ton');
      setLastError(err instanceof Error ? err.message : String(err));
      setLiveState('fehlgeschlagen');
    }
  }, [hotkeyState, pushCue]);

  const stopAndSecure = useCallback(async () => {
    if (!identityHash) return;
    setLiveState('wird_gesichert');
    pushCue('stopp_ton');
    try {
      await jfwApi.stopRecording(identityHash, 'toggle');
      await jfwApi.commitRecording(identityHash, {
        stop_reason: 'toggle',
        audio_hash: 'pending-capture',
        manifest: { contract_version: 'jfw6_run_v1' },
        sound_cue_marks: cues.concat([
          { kind: 'stopp_ton', start_100ns: clockRef.current, end_100ns: clockRef.current + 1200000 },
        ]),
      });
      const summary = await jfwApi.getRecording(identityHash);
      setLastSummary(
        `Run ${summary.run_id} gesichert · sound_cue_marks=${JSON.stringify(summary.sound_cue_marks ?? [])}`,
      );
      setLiveState('gesichert');
    } catch (err) {
      pushCue('fehler_ton');
      setLastError(err instanceof Error ? err.message : String(err));
      setLiveState('fehlgeschlagen');
    }
  }, [identityHash, cues, pushCue]);

  const confirmDiscard = useCallback(async () => {
    setDiscardOpen(false);
    if (!identityHash) {
      setLiveState('verworfen');
      return;
    }
    try {
      await jfwApi.cancelRecording(identityHash, true, null);
      setLiveState('verworfen');
    } catch {
      setLiveState('fehlgeschlagen');
    }
  }, [identityHash]);

  const registerShiftShift = useCallback(() => {
    update(
      { chord_toggle_to_talk_keys: ['Shift', 'Shift'], hotkey_enabled: true },
      {
        onSuccess: () => setHotkeyState('registriert'),
        onError: () => setHotkeyState('nicht_bereit'),
      },
    );
  }, [update]);

  const activeState = liveState === 'aus' ? preview : liveState;
  const toggleChord = settings?.chord_toggle_to_talk_keys?.join('+') ?? '—';

  return (
    <div className="space-y-4">
      <StatePreview
        label="Zustandsvorschau Pill:"
        states={RUN_PILL_STATES}
        active={preview}
        onChange={setPreview}
        testId="pill-state-preview"
      />

      <SectionCard
        title="Aufnahme-Pill an der RunSession"
        description="Zustand, Text und Form statt Farbe allein; die Pill bleibt bei aktiver Aufnahme dauerhaft sichtbar."
        testId="recording-pill-card"
      >
        <div className="flex flex-wrap items-center gap-3">
          <RunSessionPill state={activeState} elapsedMs={elapsedMs} />
          <StateChip tone={PILL_META[activeState].tone} icon={PILL_META[activeState].icon}>
            RunSession: {activeState}
          </StateChip>
        </div>
        <dl className="mt-2">
          <Row label="Run-ID / Identität">{identityHash ?? 'kein Run angefordert'}</Row>
          <Row label="Letzter Manifest-Stand">{lastSummary ?? '—'}</Row>
          <Row label="Letzter Fehler (inhaltsfrei)">{lastError ?? '—'}</Row>
        </dl>
        <div className="flex flex-wrap gap-2">
          <JfwButton variant="primary" testId="run-start" onClick={startRun}>
            Run anfordern (Toggle)
          </JfwButton>
          <JfwButton testId="run-stop" onClick={stopAndSecure}>
            Stoppen & sichern
          </JfwButton>
          <JfwButton variant="danger" testId="run-discard" onClick={() => setDiscardOpen(true)}>
            Verwerfen …
          </JfwButton>
        </div>
        {discardOpen && (
          <div
            role="alertdialog"
            aria-label="Verwerfen bestätigen"
            data-testid="discard-dialog"
            className="rounded-lg border border-red-500/40 bg-red-500/5 p-3 space-y-2"
          >
            <p className="text-sm font-medium">Verwerfen — Bestätigung erforderlich</p>
            <p className="text-sm text-muted-foreground">
              Kein Handoff an die Transkription. Das Löschen erfolgt erst nach gebundenem
              Löschvertrag; ohne Bestätigung passiert nichts.
            </p>
            <div className="flex gap-2">
              <JfwButton variant="ghost" testId="discard-cancel" onClick={() => setDiscardOpen(false)}>
                Fortsetzen
              </JfwButton>
              <JfwButton variant="danger" testId="discard-confirm" onClick={confirmDiscard}>
                Verwerfen bestätigen
              </JfwButton>
            </div>
          </div>
        )}
      </SectionCard>

      <SectionCard
        title="Sound-Cues (start_ton · stopp_ton · fehler_ton)"
        description="Cue-Fenster werden als sound_cue_marks ins Run-Manifest gebunden (Pflichtinhalt des JFW-7-Handoffs)."
        testId="sound-cues-card"
      >
        <p className="text-xs text-muted-foreground">
          Sound-Cue-Marken sind nie Sprach-, Aufnahme- oder Namensevidenz (is_cue_window).
        </p>
        <ul className="space-y-1" data-testid="cue-list">
          {CUE_KINDS.map((kind) => {
            const marks = cues.filter((c) => c.kind === kind);
            return (
              <li key={kind} className="flex items-center justify-between text-sm border-b border-border/60 py-1">
                <span className="font-mono text-xs">{kind}</span>
                <span data-testid={`cue-${kind}`}>
                  {marks.length > 0
                    ? marks
                        .map((m) => `[${m.start_100ns}; ${m.end_100ns}]`)
                        .join(' ')
                    : 'noch kein Fenster markiert'}
                </span>
              </li>
            );
          })}
        </ul>
        <div className="flex gap-2">
          <JfwButton testId="cue-test-start" onClick={() => pushCue('start_ton')}>
            Startton markieren
          </JfwButton>
          <JfwButton testId="cue-test-stop" onClick={() => pushCue('stopp_ton')}>
            Stopton markieren
          </JfwButton>
          <JfwButton testId="cue-test-error" onClick={() => pushCue('fehler_ton')}>
            Fehlerton markieren
          </JfwButton>
        </div>
      </SectionCard>

      <SectionCard
        title="Shift+Shift-Registrierung (Toggle-Hotkey)"
        description="Belegung wird sichtbar registriert; bei Kollision/Nicht-Registrierbarkeit bleibt Diktat als „Nicht bereit“ sichtbar — keine stille Ersatzbelegung."
        testId="hotkey-card"
      >
        <dl>
          <Row label="Aktuelle Toggle-Belegung">{toggleChord}</Row>
          <Row label="Registrierstatus">
            <StateChip
              tone={hotkeyState === 'registriert' ? 'ok' : hotkeyState === 'nicht_bereit' ? 'error' : 'neutral'}
              icon={hotkeyState === 'registriert' ? '✓' : hotkeyState === 'nicht_bereit' ? '⊘' : '○'}
            >
              {hotkeyState === 'registriert'
                ? 'Shift+Shift registriert'
                : hotkeyState === 'nicht_bereit'
                  ? 'Nicht bereit — Registrierung fehlgeschlagen'
                  : 'unbekannt — noch nicht registriert'}
            </StateChip>
          </Row>
        </dl>
        <JfwButton
          variant="primary"
          testId="register-shift-shift"
          ariaLabel="Shift+Shift als Toggle-Hotkey registrieren"
          onClick={registerShiftShift}
        >
          Shift+Shift registrieren
        </JfwButton>
      </SectionCard>
    </div>
  );
}
