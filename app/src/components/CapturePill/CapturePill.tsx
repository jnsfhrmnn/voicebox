import { motion } from 'framer-motion';
import { AlertCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { cn } from '@/lib/utils/cn';

/**
 * Pill state machine shared between the settings preview and the live
 * recording pill in the Captures tab.
 */
export type PillState =
  | 'recording'
  | 'transcribing'
  | 'refining'
  | 'speaking'
  | 'completed'
  | 'rest'
  | 'error';

/**
 * Sichtbare Herkunft des Pill-Inhalts (USCRX-2026-16039/16106 Punkt 3):
 * `preview` = Einstellungs-Vorschau, `live` = tatsächlicher Live-Zustand.
 * Die Pill kennzeichnet beides IMMER sichtbar (Chip + aria-label), damit
 * Vorschau und Live-Zustand im Bild nicht verwechselt werden können.
 */
export type PillDisplayMode = 'preview' | 'live';

const PILL_LABEL_KEYS: Record<Exclude<PillState, 'rest' | 'error'>, string> = {
  recording: 'captures.pill.recording',
  transcribing: 'captures.pill.transcribing',
  refining: 'captures.pill.refining',
  speaking: 'captures.pill.speaking',
  completed: 'captures.pill.completed',
};

function barModeFor(
  state: Exclude<PillState, 'error'>,
): 'generating' | 'playing' | 'idle' {
  if (state === 'recording' || state === 'speaking') return 'playing';
  if (state === 'completed' || state === 'rest') return 'idle';
  return 'generating';
}

export function PillAudioBars({ mode }: { mode: 'generating' | 'playing' | 'idle' }) {
  return (
    <div className="flex items-center gap-[2px] h-5 shrink-0">
      {[0, 1, 2, 3, 4].map((i) => (
        <motion.div
          key={`${mode}-${i}`}
          className={cn('w-[3px] rounded-full', mode === 'idle' ? 'bg-accent/30' : 'bg-accent')}
          animate={
            mode === 'generating'
              ? { height: ['6px', '16px', '6px'] }
              : mode === 'playing'
                ? { height: ['8px', '14px', '4px', '12px', '8px'] }
                : { height: '8px' }
          }
          transition={
            mode === 'generating'
              ? { duration: 0.6, repeat: Infinity, delay: i * 0.08, ease: 'easeInOut' }
              : mode === 'playing'
                ? { duration: 1.2, repeat: Infinity, delay: i * 0.15, ease: 'easeInOut' }
                : { duration: 0.4, ease: 'easeOut' }
          }
        />
      ))}
    </div>
  );
}

/** Zusatz-Chip: trennt Einstellungs-Vorschau und Live-Zustand immer sichtbar. */
function ModeChip({ mode }: { mode: PillDisplayMode }) {
  const { t } = useTranslation();
  return (
    <span
      data-pill-mode={mode}
      className={cn(
        'inline-flex shrink-0 items-center rounded-full border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide',
        mode === 'live'
          ? 'border-accent/50 bg-accent/10 text-accent'
          : 'border-border bg-muted/60 text-muted-foreground',
      )}
    >
      {t(mode === 'live' ? 'captures.pill.mode.live' : 'captures.pill.mode.preview')}
    </span>
  );
}

function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

/**
 * Floating pill shown during capture. `state` drives the label, dot animation,
 * and bar motion; `elapsedMs` freezes at whatever the caller last passed in
 * (recording advances the timer, transcribing/refining hold the final value).
 * The ``error`` state renders a destructive variant — a clickable pill that
 * copies its message to the clipboard on press and calls ``onDismiss``.
 * The pill ALWAYS shows whether it renders the settings preview or the live
 * state (`displayMode` chip + aria-label) — see ``PillDisplayMode``.
 */
export function CapturePill({
  state,
  elapsedMs,
  onStop,
  errorMessage,
  onDismiss,
  displayMode: displayModeProp,
  className,
}: {
  state: PillState;
  elapsedMs: number;
  onStop?: () => void;
  errorMessage?: string | null;
  onDismiss?: () => void;
  /** Explizite Herkunftsanzeige (Vorschau vs. Live); siehe `PillDisplayMode`. */
  displayMode?: PillDisplayMode;
  className?: string;
}) {
  const { t } = useTranslation();

  // Vorschau vs. Live bleibt IMMER sichtbar (Chip + aria-label). Rufer ohne
  // explizites `displayMode` gelten nur dann als live, wenn sie eine
  // Live-Session anbinden (Stop-/Dismiss-/Fehler-Props); die reine
  // Einstellungs-Vorschau uebergibt ausschliesslich state/elapsedMs.
  const hasLiveBinding = onStop !== undefined || onDismiss !== undefined || errorMessage !== undefined;
  const displayMode: PillDisplayMode = displayModeProp ?? (hasLiveBinding ? 'live' : 'preview');

  if (state === 'error') {
    return (
      <ErrorPill
        message={errorMessage ?? t('captures.pill.errorFallback')}
        displayMode={displayMode}
        onDismiss={onDismiss}
        className={className}
      />
    );
  }

  const visible = state !== 'rest';
  const labelText = t(state === 'rest' ? PILL_LABEL_KEYS.recording : PILL_LABEL_KEYS[state]);
  const barMode = barModeFor(state);

  const dot = (
    <span className="relative flex h-2 w-2 shrink-0">
      {state === 'recording' && (
        <span className="absolute inset-0 rounded-full bg-accent animate-ping opacity-70" />
      )}
      <span className="relative rounded-full h-2 w-2 bg-accent" />
    </span>
  );

  const stopButton = onStop && state === 'recording' ? (
    <button
      type="button"
      onClick={onStop}
      aria-label={t('captures.pill.stopAria')}
      className="relative flex h-2 w-2 shrink-0 items-center justify-center rounded-full focus:outline-none focus:ring-2 focus:ring-accent/50"
    >
      {dot}
    </button>
  ) : dot;

  // Completed gets an inset accent stroke (via box-shadow, not Tailwind's
  // ring — ring utility doesn't compose with arbitrary shadow-[…]) to mark
  // the success moment without changing the pill's dimensions.
  const completedStroke =
    state === 'completed'
      ? 'shadow-[inset_0_0_0_2px_hsl(var(--accent)/0.6)]'
      : null;

  return (
    <div
      role="status"
      aria-label={t('captures.pill.stateAria', {
        mode: t(displayMode === 'live' ? 'captures.pill.modeAria.live' : 'captures.pill.modeAria.preview'),
        state: labelText,
      })}
      data-pill-mode={displayMode}
      className={cn(
        'inline-flex items-center gap-3 px-4 h-10 rounded-full text-accent',
        'bg-white/80 ring-1 ring-black/5 shadow-lg backdrop-blur-xl',
        'dark:bg-black/55 dark:ring-0 dark:shadow-none dark:backdrop-blur-md',
        completedStroke,
        'transition-opacity duration-300 ease-out',
        visible ? 'opacity-100' : 'opacity-0 pointer-events-none',
        className,
      )}
    >
      {stopButton}
      <span className="text-sm font-medium shrink-0" style={{ minWidth: '104px' }}>
        {labelText}
      </span>
      <ModeChip mode={displayMode} />
      <PillAudioBars mode={barMode} />
      <span className="text-xs tabular-nums text-accent/70 font-medium shrink-0 -ml-1">
        {formatElapsed(elapsedMs)}
      </span>
    </div>
  );
}

function ErrorPill({
  message,
  displayMode,
  onDismiss,
  className,
}: {
  message: string;
  displayMode: PillDisplayMode;
  onDismiss?: () => void;
  className?: string;
}) {
  const { t } = useTranslation();
  const handleClick = async () => {
    try {
      await navigator.clipboard.writeText(message);
    } catch {
      // Clipboard access can be denied in rare webview configs — ignore,
      // we still want the dismiss to land.
    }
    onDismiss?.();
  };

  return (
    <button
      type="button"
      onClick={handleClick}
      title={t('captures.pill.errorCopyTooltip')}
      aria-label={t('captures.pill.stateAria', {
        mode: t(displayMode === 'live' ? 'captures.pill.modeAria.live' : 'captures.pill.modeAria.preview'),
        state: message,
      })}
      data-pill-mode={displayMode}
      className={cn(
        'inline-flex items-center gap-2.5 px-4 h-10 rounded-full',
        'bg-white/85 ring-1 ring-destructive/25 shadow-lg backdrop-blur-xl text-red-600 hover:bg-white',
        'dark:bg-black/65 dark:ring-0 dark:shadow-none dark:backdrop-blur-md dark:text-red-300 dark:hover:bg-black/80',
        'max-w-[380px] transition-colors',
        'focus:outline-none focus:ring-2 focus:ring-red-400/50',
        className,
      )}
    >
      <AlertCircle className="h-3.5 w-3.5 shrink-0" />
      <ModeChip mode={displayMode} />
      <span className="text-sm font-medium truncate">{message}</span>
    </button>
  );
}

