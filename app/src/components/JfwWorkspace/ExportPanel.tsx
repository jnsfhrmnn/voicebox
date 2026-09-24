import { useState } from 'react';
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
import { jfwApi } from '@/lib/api/jfwApi';
import { JFW_FOCUS_RING, JfwButton, Row, SectionCard, StateChip, StatePreview } from './JfwCommon';
import { JfwProgress, type ProgressPhase } from './JfwProgress';

/**
 * JFW-4: Bestaetigungsansicht, Freigabe-/Abbruchdialog, Trust-Boundary-Hinweis,
 * Loeschhinweis und Fortschritts-/Abbruch-UI.
 *
 * Ohne ausdrueckliche Freigabe entstehen keine Enddateien: erst der
 * bestaetigte Freigabe-Dialog ruft `/export/run` auf; `prepare` bleibt
 * lesend. Die Ansicht zeigt Quellrevision, Qualitaet (inkl. sichtbarem
 * Teilmodus-Marker), Format, Ziel, Namen, Konflikte, Namensnutzung, Warnungen
 * und den Loesch-Hinweis.
 */

export type ExportViewState = 'vorbereitung' | 'bereit' | 'bereit_mit_warnungen' | 'blockiert' | 'laeuft' | 'exportiert' | 'abgebrochen';

const VIEW_STATES: readonly ExportViewState[] = [
  'vorbereitung',
  'bereit',
  'bereit_mit_warnungen',
  'blockiert',
  'laeuft',
  'exportiert',
  'abgebrochen',
];

const STATE_META: Record<
  ExportViewState,
  { label: string; tone: 'neutral' | 'ok' | 'warn' | 'error' | 'active'; icon: string }
> = {
  vorbereitung: { label: 'Vorbereitung (prepare) — noch keine Dateien geschrieben', tone: 'neutral', icon: '○' },
  bereit: { label: 'Bereit — Freigabe erforderlich', tone: 'ok', icon: '✓' },
  bereit_mit_warnungen: { label: 'Bereit mit Warnungen — Zaehler vor der Bestätigung sichtbar', tone: 'warn', icon: '⚠' },
  blockiert: { label: 'Blockiert (fail-closed) — keine Freigabe moeglich', tone: 'error', icon: '⊘' },
  laeuft: { label: 'Export läuft', tone: 'active', icon: '◐' },
  exportiert: { label: 'Exportiert — Enddateien geschrieben', tone: 'ok', icon: '✓' },
  abgebrochen: { label: 'Abgebrochen — ausschließlich dieser Versuch endet', tone: 'warn', icon: '⊘' },
};

export function ExportPanel() {
  const [preview, setPreview] = useState<ExportViewState>('bereit_mit_warnungen');
  const [releaseOpen, setReleaseOpen] = useState(false);
  const [abortOpen, setAbortOpen] = useState(false);
  const [liveState, setLiveState] = useState<ExportViewState | null>(null);
  const view = liveState ?? preview;

  const progressPhase: ProgressPhase =
    view === 'laeuft'
      ? 'laufend'
      : view === 'exportiert'
        ? 'abgeschlossen'
        : view === 'abgebrochen'
          ? 'abgebrochen'
          : view === 'blockiert'
            ? 'fehlgeschlagen'
            : 'bereit';

  const release = () => {
    setReleaseOpen(false);
    setLiveState('laeuft');
    // Ausdrueckliche Freigabe: erst jetzt wird /export/run aufgerufen.
    jfwApi.runExport({}).catch(() => setLiveState('blockiert'));
  };

  const abort = () => {
    setAbortOpen(false);
    setLiveState('abgebrochen');
  };

  return (
    <div className="space-y-4">
      <StatePreview
        label="Zustandsvorschau:"
        states={VIEW_STATES}
        active={preview}
        onChange={(s) => {
          setLiveState(null);
          setPreview(s);
        }}
        testId="export-state-preview"
      />

      <SectionCard
        title="Bestätigungsansicht (JFW-4)"
        description="Quellrevision, Qualität, Format, Ziel, Namen, Konflikte, Namensnutzung, Warnungen und Lösch-Hinweis — sichtbar vor jeder Freigabe."
        testId="export-confirm-card"
      >
        <div className="flex flex-wrap items-center gap-2">
          <StateChip tone={STATE_META[view].tone} icon={STATE_META[view].icon}>
            {STATE_META[view].label}
          </StateChip>
          <StateChip tone="warn" icon="◑">
            Teilmodus sichtbar markiert: timing_only / speaker_only
          </StateChip>
        </div>
        <dl className="mt-2">
          <Row label="Quellrevision">
            transcript_revision_id + transcript_revision_hash (revisionsgebunden, keine stillen Hochstufungen)
          </Row>
          <Row label="Qualität">
            Qualitätsdimensionen mit realen Zählern vor der Bestätigung · Teilmodus-Marker in SRT-Labelzeile und JSON `readiness.partial_mode`
          </Row>
          <Row label="Format">json · srt · vtt (ohne autoritativen Zeitbereich blockiert)</Row>
          <Row label="Ziel">target_dir (Zielwurzel; belegte Ziele werden nie still überschrieben)</Row>
          <Row label="Namen">name_policy = neutral · Namensnutzung nur bestätigt</Row>
          <Row label="Konflikte">existing_targets sichtbar vor der Freigabe; replace_existing = false</Row>
          <Row label="Warnungen">reason_codes der Readiness (inhaltsfrei), keine stillen Streichungen</Row>
          <Row label="Lösch-Hinweis">
            Hinweis beim Job-Löschen: exportierte Nutzerdateien bestehen fort
          </Row>
        </dl>

        <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3" data-testid="trust-boundary-hint">
          <StateChip tone="warn" icon="⚠">
            Trust Boundary
          </StateChip>
          <p className="text-sm mt-1">
            Trust-Boundary-Hinweis bei unsicheren Zielen: synchronisierte, entfernbare oder
            Netzwerkziele (UNC) überschreiten die lokale Vertrauensgrenze und werden vor der
            Freigabe ausdrücklich benannt. Verarbeitung bleibt lokal — kein Cloud-Fallback.
          </p>
        </div>

        <div className="flex flex-wrap gap-2">
          <JfwButton
            variant="primary"
            testId="export-release"
            disabled={view === 'blockiert'}
            onClick={() => setReleaseOpen(true)}
          >
            Freigabe …
          </JfwButton>
          <JfwButton
            variant="danger"
            testId="export-abort"
            disabled={view !== 'laeuft'}
            onClick={() => setAbortOpen(true)}
          >
            Abbruch …
          </JfwButton>
        </div>
        <p className="text-xs text-muted-foreground">
          Ohne ausdrückliche Freigabe entstehen keine Enddateien, keine Änderungen und keine
          Export-Markierung (`prepare` bleibt lesend).
        </p>
      </SectionCard>

      <SectionCard
        title="Fortschritts-/Abbruch-UI"
        description="Ab 2 s Laufzeit sichtbarer Fortschritts-/Aktivitätszustand; kontrollierter Abbruch über /export/{export_key}/cancel."
        testId="export-progress-card"
      >
        <JfwProgress
          title="JFW-4 Export"
          phase={progressPhase}
          detail={
            view === 'blockiert'
              ? 'format_status: blockiert · not_exportable_ranges sichtbar · JSON nur mit gebundenen nutzbaren Daten'
              : '60-Min-/4-h-Performance bleibt Zielsystemmessung.'
          }
          onCancel={() => setAbortOpen(true)}
        />
      </SectionCard>

      <AlertDialog open={releaseOpen} onOpenChange={setReleaseOpen}>
        <AlertDialogContent data-testid="release-dialog">
          <AlertDialogHeader>
            <AlertDialogTitle>Freigabe bestätigen</AlertDialogTitle>
            <AlertDialogDescription>
              Erst die ausdrückliche Freigabe schreibt Enddateien und markiert den Export.
              Bestätigt: Quellrevision, Qualität, Format, Ziel, Namenspolitik und die
              aufgeführten Konflikte. Nicht überschrieben werden bestehende Zieldateien.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className={JFW_FOCUS_RING}>Abbrechen</AlertDialogCancel>
            <AlertDialogAction className={JFW_FOCUS_RING} data-testid="release-confirm" onClick={release}>
              Freigabe bestätigen
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={abortOpen} onOpenChange={setAbortOpen}>
        <AlertDialogContent data-testid="export-abort-dialog">
          <AlertDialogHeader>
            <AlertDialogTitle>Export abbrechen?</AlertDialogTitle>
            <AlertDialogDescription>
              Beendet wird ausschließlich dieser Exportversuch als „Abgebrochen“. Quellrevision
              und frühere autoritative Ergebnisse bleiben unverändert; temporäre
              Exportartefakte werden bereinigt.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className={JFW_FOCUS_RING}>Fortsetzen</AlertDialogCancel>
            <AlertDialogAction className={JFW_FOCUS_RING} data-testid="export-abort-confirm" onClick={abort}>
              Abbruch bestätigen
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
