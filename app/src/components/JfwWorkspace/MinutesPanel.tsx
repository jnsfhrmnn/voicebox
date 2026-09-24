import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { jfwApi } from '@/lib/api/jfwApi';
import { JFW_FOCUS_RING, JfwButton, Row, SectionCard, StateChip, StatePreview } from './JfwCommon';
import { JFW_PROGRESS_POLL_MS, JfwProgress, type ProgressPhase } from './JfwProgress';
import { ModelInstallOffer, ModelMissingNotice, type ModelInstallCandidate } from './ModelInstallOffer';

/**
 * JFW-13: Meeting-Protokoll — Re-Identifizierungshinweis im Exportdialog,
 * Fortschrittsanzeige ≤ 2 s, getrenntes Installationsangebot mit Herkunfts-
 * und Lizenzhinweis sowie sichtbar „fortsetzbar" gefuehrte Unterbrechungen.
 */

const MINUTES_MODEL: ModelInstallCandidate = {
  name: 'protokoll-modell (JFW-13 Artefaktmanifest)',
  feature: 'JFW-13 Meeting-Protokoll',
  source: 'Modellregister / Artefaktmanifest (Repo-URL, lizenzgeprüft)',
  revision: 'immutable Revision (40-hex) laut Manifest',
  license: 'Apache-2.0 bzw. MIT — Distribution belegt, fail-closed Gate bei unbekannter Lizenz',
};

export type MinutesViewState = 'blockiert' | 'laeuft' | 'abgebrochen' | 'fortsetzbar' | 'fertig' | 'invalidiert';

const VIEW_STATES: readonly MinutesViewState[] = [
  'blockiert',
  'laeuft',
  'abgebrochen',
  'fortsetzbar',
  'fertig',
  'invalidiert',
];

const STATE_META: Record<
  MinutesViewState,
  { label: string; tone: 'neutral' | 'ok' | 'warn' | 'error' | 'active'; icon: string }
> = {
  blockiert: { label: 'Blockiert (fail-closed) — kein Teilprotokoll aus ungebundenen Quellen', tone: 'error', icon: '⊘' },
  laeuft: { label: 'Protokoll läuft', tone: 'active', icon: '◐' },
  abgebrochen: { label: 'Abgebrochen — ausschließlich dieser Versuch endet', tone: 'warn', icon: '⊘' },
  fortsetzbar: { label: 'Sichtbar fortsetzbar — temporäre Zwischenstände bereinigt oder markiert', tone: 'warn', icon: '⚠' },
  fertig: { label: 'Ergebnis autoritativ gespeichert', tone: 'ok', icon: '✓' },
  invalidiert: { label: 'Invalidiert bei Revisionswechsel — exportierter Bestand unverändert', tone: 'warn', icon: '◑' },
};

export function MinutesPanel() {
  const [preview, setPreview] = useState<MinutesViewState>('fortsetzbar');
  const [exportOpen, setExportOpen] = useState(false);
  const [installOpen, setInstallOpen] = useState(false);
  const [registerDeleted, setRegisterDeleted] = useState(false);
  const [minutesKey, setMinutesKey] = useState<string | null>(null);

  const live = useQuery({
    queryKey: ['jfw13-minutes', minutesKey],
    queryFn: () => jfwApi.getMinutes(minutesKey as string),
    enabled: Boolean(minutesKey),
    refetchInterval: JFW_PROGRESS_POLL_MS,
  });

  const progressPhase: ProgressPhase =
    preview === 'laeuft'
      ? 'laufend'
      : preview === 'fertig'
        ? 'abgeschlossen'
        : preview === 'abgebrochen'
          ? 'abgebrochen'
          : preview === 'blockiert'
            ? 'fehlgeschlagen'
            : 'bereit';

  return (
    <div className="space-y-4">
      <StatePreview
        label="Zustandsvorschau:"
        states={VIEW_STATES}
        active={preview}
        onChange={setPreview}
        testId="minutes-state-preview"
      />

      <SectionCard
        title="Fortschrittsanzeige (≤ 2 s) und kontrollierter Abbruch"
        description="Spätestens nach 2 s belastbarer Fortschritts-/Aktivitätszustand; Abbruch beendet ausschließlich den eigenen Versuch."
        testId="minutes-progress-card"
      >
        <JfwProgress
          title="JFW-13 Protokoll-Schritt"
          phase={progressPhase}
          detail={
            preview === 'fortsetzbar'
              ? 'Nach Crash/Neustart: kein unvollständiges Ergebnis ist autoritativ; Zwischenstände sind bereinigt oder sichtbar fortsetzbar.'
              : 'Abbruchfaehigkeit ist backendseitig belegt; Lauf-/Hardwareziele bleiben Zielsystemmessung.'
          }
          lastUpdatedAtMs={live.dataUpdatedAt || null}
          onCancel={() => minutesKey && jfwApi.cancelMinutes(minutesKey).catch(() => undefined)}
        />
        <label className="flex items-center gap-2 text-sm">
          Auftrags-Identität (minutes_key):
          <input
            type="text"
            value={minutesKey ?? ''}
            onChange={(e) => setMinutesKey(e.target.value || null)}
            placeholder="minutes_key des Protokollauftrags"
            aria-label="Minutes-Key des Protokollauftrags"
            className={`flex-1 rounded-md border border-border bg-background px-2 py-1 ${JFW_FOCUS_RING}`}
            data-testid="minutes-identity-input"
          />
        </label>
        <div className="flex items-center gap-2">
          <StateChip tone={STATE_META[preview].tone} icon={STATE_META[preview].icon}>
            {STATE_META[preview].label}
          </StateChip>
        </div>
      </SectionCard>

      <SectionCard
        title="Installationsangebot (getrennt, mit Herkunfts- und Lizenzhinweis)"
        description="Fehlendes Modellartefakt blockiert ausschließlich JFW-13 — nie Transkription oder Export; kein automatischer Netzwerkzugriff."
        testId="minutes-install-card"
      >
        <ModelMissingNotice candidate={MINUTES_MODEL} onOfferInstall={() => setInstallOpen(true)} />
      </SectionCard>

      <SectionCard
        title="Exportdialog mit Re-Identifizierungshinweis"
        description="Vor jeder Bestätigung sichtbar, solange identifizierende Restinformationen verbleiben; nie eine geprüfte Anonymitätsbehauptung."
        actions={
          <JfwButton variant="primary" testId="open-minutes-export" onClick={() => setExportOpen(true)}>
            Exportdialog öffnen …
          </JfwButton>
        }
        testId="minutes-export-card"
      >
        <dl>
          <Row label="Register">getrenntes Pseudonymregister, ausdrücklich löschbar; Zuordnung nie in Logs/Metriken/Crash-Dumps/Standardexporten</Row>
          <Row label="AC-74-Exportgate">weitere Ausgaben an eine gelöschte Registerrevision werden abgelehnt</Row>
          <Row label="Byte-Regel">identische Eingangs- UND Registerrevision → byteidentisch; Nicht-Reproduzierbares wird gesonderte Ergebnisrevision, nie zweite Fassung</Row>
        </dl>
      </SectionCard>

      {/* Re-Identifizierungshinweis im Exportdialog */}
      <Dialog open={exportOpen} onOpenChange={setExportOpen}>
        <DialogContent data-testid="minutes-export-dialog">
          <DialogHeader>
            <DialogTitle>Protokoll exportieren</DialogTitle>
            <DialogDescription>
              Bestätigte Fassung gilt für alle drei Abschnitte (Kopfdaten/Aufgaben,
              Kurz/Lang-Zusammenfassung, vollständiges anonymisiertes Transkript in Reihenfolge).
            </DialogDescription>
          </DialogHeader>

          <div
            className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3"
            data-testid="reident-hint"
          >
            <StateChip tone="warn" icon="⚠">
              Re-Identifizierungshinweis
            </StateChip>
            <p className="text-sm mt-1">
              Es verbleiben identifizierende Restinformationen (Unikate im Gespräch oder vom
              Nutzer nicht ersetzte Angaben, markiert als „nicht ersetzbar“). Trotz
              Pseudonymisierung kann eine Person eindeutig zugeordnet werden — die App
              behauptet keine geprüfte Anonymität. Hinweise: reident_hinweise des Dokuments
              (inhaltsfrei begründet).
            </p>
          </div>

          {registerDeleted && (
            <div
              className="rounded-lg border border-border bg-muted/40 p-3"
              data-testid="register-deleted-notice"
            >
              <p className="text-sm">
                Zuordnungsregister gelöscht: das Protokoll behält nur die Pseudonyme, die
                Rückführung ist unmöglich, die App behauptet keine rekonstruierbare
                Namensherkunft mehr. Dieser offene Exportdialog zeigt keine Zuordnung mehr;
                weitere Ausgaben an die gelöschte Registerrevision werden abgelehnt.
              </p>
            </div>
          )}

          <dl>
            <Row label="Metadaten/Links/Pfade">entfernt bzw. als `entfernt` gekennzeichnet</Row>
            <Row label="Belegpflicht">jede Aufgabe trägt mindestens eine Belegreferenz; Unbelegtes wird nicht ausgegeben</Row>
          </dl>

          <DialogFooter>
            <JfwButton variant="ghost" testId="register-delete" onClick={() => setRegisterDeleted(true)}>
              Zuordnungsregister löschen
            </JfwButton>
            <JfwButton variant="ghost" testId="minutes-export-cancel" onClick={() => setExportOpen(false)}>
              Abbrechen
            </JfwButton>
            <JfwButton
              variant="primary"
              testId="minutes-export-confirm"
              onClick={() => setExportOpen(false)}
            >
              Trotz Hinweis exportieren
            </JfwButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ModelInstallOffer
        open={installOpen}
        onOpenChange={setInstallOpen}
        candidate={MINUTES_MODEL}
      />
    </div>
  );
}
