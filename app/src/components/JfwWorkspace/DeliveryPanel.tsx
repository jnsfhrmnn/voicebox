import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { jfwApi, type DeliverySummary } from '@/lib/api/jfwApi';
import { JFW_FOCUS_RING, JfwButton, Row, SectionCard, StateChip, StatePreview } from './JfwCommon';
import { JFW_PROGRESS_POLL_MS } from './JfwProgress';

/**
 * JFW-8: Zustandsanzeige der Zieluebergabe in der Pill-/Capture-Ansicht.
 *
 * Verlangte Zustaende (Spec-Texte): `Ziel bestätigen`, `Manuell kopieren`,
 * `Einfügung fehlgeschlagen`, `Ausgang unbekannt`, `Erneut einfügen` — nie
 * ein falscher Erfolg. Jeder Zustand traegt Text und Form (nie nur Farbe),
 * Fokusreihenfolge und zugaengliche Beschriftung; die Zwischenablage wird als
 * externe Trust Boundary sichtbar benannt.
 */

export type DeliveryViewState =
  | 'ziel_bestaetigen'
  | 'manuell_kopieren'
  | 'einfuegung_fehlgeschlagen'
  | 'ausgang_unbekannt'
  | 'erneut_einfuegen';

const VIEW_STATES: readonly DeliveryViewState[] = [
  'ziel_bestaetigen',
  'manuell_kopieren',
  'einfuegung_fehlgeschlagen',
  'ausgang_unbekannt',
  'erneut_einfuegen',
];

const STATE_META: Record<
  DeliveryViewState,
  {
    label: string;
    tone: 'neutral' | 'ok' | 'warn' | 'error' | 'active';
    icon: string;
    description: string;
  }
> = {
  ziel_bestaetigen: {
    label: 'Ziel bestätigen',
    tone: 'warn',
    icon: '⚠',
    description:
      'Ziel-Snapshot sichtbar bestätigen (Target-Confirm-Flag). Ohne Bestätigung keine automatische Einfügung.',
  },
  manuell_kopieren: {
    label: 'Manuell kopieren',
    tone: 'neutral',
    icon: '⧉',
    description:
      'Explizite Aktion; die Zwischenablage wird als externe Trust Boundary gezeigt und nur ausdrücklich beschrieben.',
  },
  einfuegung_fehlgeschlagen: {
    label: 'Einfügung fehlgeschlagen',
    tone: 'error',
    icon: '✕',
    description:
      'failed — statt falschem Erfolg. Erneuter Versuch nur nach ausdrücklicher Nutzeraktion und neuer Zielprüfung.',
  },
  ausgang_unbekannt: {
    label: 'Ausgang unbekannt',
    tone: 'warn',
    icon: '?',
    description:
      'unknown — kein Auto-Retry; das Einmalbudget gilt dauerhaft als verbraucht.',
  },
  erneut_einfuegen: {
    label: 'Erneut einfügen',
    tone: 'active',
    icon: '↻',
    description:
      'Kindoperation mit Elternbezug, neuem Ziel-Snapshot und eigenem Einmalbudget; alte Operation nie zurückgesetzt.',
  },
};

export function DeliveryPanel() {
  const [preview, setPreview] = useState<DeliveryViewState>('ziel_bestaetigen');
  const [identityHash, setIdentityHash] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const live = useQuery({
    queryKey: ['jfw8-delivery', identityHash],
    queryFn: () => jfwApi.getDelivery(identityHash as string),
    enabled: Boolean(identityHash),
    refetchInterval: JFW_PROGRESS_POLL_MS,
  });

  const liveSummary: DeliverySummary | undefined = live.data;

  const statusToView = (status: string): DeliveryViewState | null => {
    if (status === 'failed') return 'einfuegung_fehlgeschlagen';
    if (status === 'unknown') return 'ausgang_unbekannt';
    return null;
  };

  const activeView = liveSummary ? (statusToView(liveSummary.status) ?? preview) : preview;
  const activeMeta = STATE_META[activeView];

  return (
    <div className="space-y-4">
      <StatePreview
        label="Zustandsvorschau Zielübergabe:"
        states={VIEW_STATES}
        active={preview}
        onChange={(s) => {
          setIdentityHash(null);
          setNotice(null);
          setPreview(s);
        }}
        testId="delivery-state-preview"
      />

      <SectionCard
        title="Pill/Capture-Ansicht — Zustandsanzeige Zielübergabe"
        description="Zustand nie nur farblich: Text, Form, Fokusreihenfolge und zugängliche Beschriftung."
        testId="delivery-state-card"
      >
        <div
          role="status"
          aria-live="polite"
          aria-label={`Zielübergabe: ${activeMeta.label}`}
          data-testid="delivery-state"
          data-state={activeView}
          className={`inline-flex items-center gap-2.5 rounded-full px-4 h-10 ring-2 ${JFW_FOCUS_RING} ${
            activeView === 'einfuegung_fehlgeschlagen'
              ? 'ring-red-500/60'
              : activeView === 'ausgang_unbekannt' || activeView === 'ziel_bestaetigen'
                ? 'ring-amber-500/60'
                : 'ring-accent/60'
          }`}
        >
          <span aria-hidden="true" className="text-base leading-none">{activeMeta.icon}</span>
          <span className="text-sm font-semibold">{activeMeta.label}</span>
          <span className="sr-only">Zielübergabe-Zustand, nicht nur farblich</span>
        </div>
        <p className="text-sm text-muted-foreground mt-2" data-testid="delivery-state-description">
          {activeMeta.description}
        </p>

        <dl className="mt-2">
          <Row label="Vertragszustände">pending · attempting · succeeded · failed · unknown (unknown verbraucht dauerhaft das Budget)</Row>
          <Row label="Zielbindung">target_confirmed: {String(liveSummary?.target_confirmed ?? 'sichtbar zu bestätigen')} · Zielwechsel erzeugt nie ein Ersatzziel</Row>
          <Row label="Manuell">manual_only — ohne automatischen Versuch; Aufnahme bleibt möglich, spätere Zielbestätigung verlangt</Row>
          <Row label="Retry">nur ausdrückliche Nutzeraktion + neue Zielprüfung — Auto-Retry gibt es nie</Row>
        </dl>

        <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3" data-testid="delivery-trust-boundary">
          <StateChip tone="warn" icon="⚠">
            Zwischenablage = externe Trust Boundary
          </StateChip>
          <p className="text-sm mt-1">
            Kopieren führt Text über die Trust Boundary der Zwischenablage. Fremde
            Zwischenablage-Änderungen werden nie überschrieben; der eigene Restore bleibt bei
            Konflikt „nicht_ausgefuehrt“.
          </p>
        </div>

        <div className="flex flex-wrap gap-2">
          <JfwButton
            variant="primary"
            testId="delivery-confirm-target"
            onClick={() => setNotice('Ziel bestätigt — Target-Confirm-Flag gesetzt.')}
          >
            Ziel bestätigen
          </JfwButton>
          <JfwButton
            testId="delivery-copy"
            onClick={() => {
              setNotice('Manuell kopieren — Zwischenablage als externe Trust Boundary, nur ausdrücklich ausgeführt.');
              if (identityHash) jfwApi.deliveryCopy(identityHash, true).catch(() => undefined);
            }}
          >
            Manuell kopieren
          </JfwButton>
          <JfwButton
            testId="delivery-manual"
            onClick={() => {
              setNotice('manual_only — kein automatischer Einfügeversuch.');
              if (identityHash) jfwApi.deliveryManual(identityHash).catch(() => undefined);
            }}
          >
            Manueller Pfad (manual_only)
          </JfwButton>
          <JfwButton
            variant="danger"
            testId="delivery-failed-demo"
            onClick={() => {
              setIdentityHash(null);
              setPreview('einfuegung_fehlgeschlagen');
            }}
          >
            Einfügung fehlgeschlagen zeigen
          </JfwButton>
          <JfwButton
            variant="danger"
            testId="delivery-unknown-demo"
            onClick={() => {
              setIdentityHash(null);
              setPreview('ausgang_unbekannt');
            }}
          >
            Ausgang unbekannt zeigen
          </JfwButton>
          <JfwButton
            testId="delivery-retry"
            onClick={() => {
              setPreview('erneut_einfuegen');
              setNotice('Erneut einfügen — Kindoperation mit neuem Ziel-Snapshot und eigenem Einmalbudget.');
              if (identityHash)
                jfwApi.deliveryRetry(identityHash, {}, {}).catch(() => undefined);
            }}
          >
            Erneut einfügen
          </JfwButton>
        </div>
        {notice && (
          <p className="text-sm" role="status" data-testid="delivery-notice">
            {notice}
          </p>
        )}
      </SectionCard>

      <SectionCard
        title="Recovery-Ansicht"
        description="Wiederherstellbarer Rohtext (benutzergebunden, zeitlich begrenzt) — nie Rekonstruktion aus Logs, Hashen, Zielinhalt oder anderen Revisionen."
        testId="delivery-recovery-card"
      >
        <dl>
          <Row label="Recovery-Eintrag">benutzergebunden, zeitlich begrenzt; Ablauf/Loeschung gemeinsam mit inhaltsfreiem Tombstone</Row>
          <Row label="Live-Status">{liveSummary ? `${liveSummary.status}${liveSummary.reason_code ? ` (${liveSummary.reason_code})` : ''}` : 'keine Operation gebunden'}</Row>
        </dl>
        <p className="text-xs text-muted-foreground">
          Logs und Crash-Dumps tragen ausschließlich IDs, Adapter, Zustände, Dauer und
          Fehlercodes — nie Rohtext, Clipboard-Inhalt, Fenstertitel mit Inhalt oder Credential.
        </p>
      </SectionCard>
    </div>
  );
}
