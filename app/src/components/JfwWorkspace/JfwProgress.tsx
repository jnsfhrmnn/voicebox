import { useEffect, useState } from 'react';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { JFW_FOCUS_RING, JfwButton, StateChip } from './JfwCommon';

/**
 * JFW-2/JFW-4/JFW-13: Fortschritts-/Abbruch-UI.
 *
 * Vertrag: laeuft ein Schritt laenger als 2 s, zeigt die App spätestens nach
 * 2 s einen belastbaren Fortschritts- oder Aktivitaetszustand, bleibt
 * bedienbar und laesst sich kontrolliert abbrechen. Umgesetzt als sofort
 * sichtbare Aktivitaetsanzeige plus Statuspolling im Abstand von 1,5 s
 * (JFW_PROGRESS_POLL_MS <= 2000). Der Abbruch verlangt eine ausdrueckliche
 * Bestaetigung im Abbruchdialog; beendet wird ausschliesslich der eigene
 * Versuch (`Abgebrochen`).
 */

export const JFW_PROGRESS_POLL_MS = 1500;

export type ProgressPhase =
  | 'bereit'
  | 'laufend'
  | 'abgebrochen'
  | 'zu_spaet'
  | 'fehlgeschlagen'
  | 'abgeschlossen';

const PHASE_META: Record<ProgressPhase, { label: string; tone: 'neutral' | 'ok' | 'warn' | 'error' | 'active'; icon: string }> = {
  bereit: { label: 'Bereit — kein Lauf aktiv', tone: 'neutral', icon: '○' },
  laufend: { label: 'Vorgang läuft — Aktivität wird angezeigt', tone: 'active', icon: '◐' },
  abgebrochen: { label: 'Abgebrochen — ausschließlich dieser Versuch endet', tone: 'warn', icon: '⊘' },
  zu_spaet: { label: 'Abbruch zu spät — Ergebnis bereits autoritativ gespeichert', tone: 'warn', icon: '⊘' },
  fehlgeschlagen: { label: 'Fehlgeschlagen — kontrolliert erneut startbar', tone: 'error', icon: '✕' },
  abgeschlossen: { label: 'Abgeschlossen — Ergebnis autoritativ gespeichert', tone: 'ok', icon: '✓' },
};

function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

export function JfwProgress({
  title,
  phase,
  detail,
  progressPercent,
  startedAtMs,
  lastUpdatedAtMs,
  onCancel,
  extraActions,
}: {
  title: string;
  /** Vorschau/Live-Zustand. */
  phase: ProgressPhase;
  /** Zustzliche Detailzeile (Phase, reason_code, Modell, …). */
  detail?: string | null;
  /** Belastbarer Fortschritt, sofern die API einen Wert liefert. */
  progressPercent?: number | null;
  startedAtMs?: number | null;
  lastUpdatedAtMs?: number | null;
  /** Kontrollierter Abbruch (nach Bestaetigungsdialog). */
  onCancel?: () => void;
  extraActions?: React.ReactNode;
}) {
  const [now, setNow] = useState(() => Date.now());
  const [confirmOpen, setConfirmOpen] = useState(false);

  // Aktivitaetsanzeige bleibt frisch: Ticker 1 s, Statuspolling uebernimmt
  // der Aufrufer (<= JFW_PROGRESS_POLL_MS).
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const meta = PHASE_META[phase];
  const elapsed = startedAtMs ? now - startedAtMs : null;
  const sinceUpdate = lastUpdatedAtMs ? Math.max(0, now - lastUpdatedAtMs) : null;

  return (
    <div
      data-testid="jfw-progress"
      data-phase={phase}
      role="status"
      aria-live="polite"
      className="rounded-lg border border-border bg-background/60 p-3 space-y-2"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <StateChip tone={meta.tone} icon={meta.icon}>
            {meta.label}
          </StateChip>
          <span className="text-sm font-medium truncate">{title}</span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {phase === 'laufend' && onCancel && (
            <JfwButton
              variant="danger"
              testId="progress-cancel"
              ariaLabel={`${title} kontrolliert abbrechen`}
              onClick={() => setConfirmOpen(true)}
            >
              Abbrechen
            </JfwButton>
          )}
          {extraActions}
        </div>
      </div>

      <dl className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <div>
          <dt>Laufzeit</dt>
          <dd className="tabular-nums" data-testid="progress-elapsed">
            {elapsed !== null ? formatElapsed(elapsed) : '—'}
          </dd>
        </div>
        <div>
          <dt>Anzeige-Intervall</dt>
          <dd data-testid="progress-interval">
            {JFW_PROGRESS_POLL_MS / 1000} s (Anforderung ≤ 2 s)
          </dd>
        </div>
        <div>
          <dt>Letzte Statusmeldung</dt>
          <dd className="tabular-nums" data-testid="progress-age">
            {sinceUpdate !== null ? `vor ${Math.round(sinceUpdate / 1000)} s` : '—'}
          </dd>
        </div>
        <div>
          <dt>Fortschritt</dt>
          <dd className="tabular-nums" data-testid="progress-percent">
            {progressPercent != null ? `${progressPercent} %` : 'Aktivitätsanzeige'}
          </dd>
        </div>
      </dl>

      {detail && (
        <p className="text-xs text-muted-foreground" data-testid="progress-detail">
          {detail}
        </p>
      )}
      <p className="text-xs text-muted-foreground">
        Bedienbarkeit bleibt erhalten: Abbruch, Navigation und alle Bedienelemente sind
        waehrend des Laufs aktiv.
      </p>

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent data-testid="abort-dialog">
          <AlertDialogHeader>
            <AlertDialogTitle>Abbruch bestätigen</AlertDialogTitle>
            <AlertDialogDescription>
              Beendet wird ausschließlich dieser Versuch als „Abgebrochen“. Quelldatei,
              Basistranskript und frühere autoritative Ergebnisse bleiben unverändert erhalten.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className={JFW_FOCUS_RING}>Fortsetzen</AlertDialogCancel>
            <AlertDialogAction
              className={JFW_FOCUS_RING}
              data-testid="abort-confirm"
              onClick={() => {
                setConfirmOpen(false);
                onCancel?.();
              }}
            >
              Abbruch bestätigen
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
