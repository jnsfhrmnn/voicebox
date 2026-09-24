import { useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { apiClient } from '@/lib/api/client';
import { JfwButton, Row, StateChip } from './JfwCommon';

/**
 * JFW-7/JFW-13: Installationsangebot fuer fehlende Modellartefakte.
 *
 * Vertrag: fehlende Artefakte loesen nie einen automatischen Netzwerkzugriff
 * aus. Die App bietet eine getrennte, ausdruecklich zu startende Installation
 * mit Herkunfts- und Lizenzhinweis an; das Fehlen blockiert ausschliesslich
 * das betroffene Feature (JFW-7 Diktat bzw. JFW-13 Protokoll).
 */

export interface ModelInstallCandidate {
  /** Modell-/Artefaktname (z. B. `openai/whisper-large-v3-turbo`). */
  name: string;
  /** Betroffenes Feature — erscheint als Blockierungshinweis. */
  feature: string;
  /** Herkunft (Modellregister: Repo-URL). */
  source: string;
  /** Immutable Revision (40-hex) aus dem Artefaktmanifest. */
  revision: string;
  /** Lizenzbezug aus dem Modellregister (fail-closed Gate: `mit`/`apache-2.0`). */
  license: string;
  /** Quelle/Groesse/Ziel des getrennten Downloads, sofern bekannt. */
  download?: { size?: string; from?: string; to?: string };
}

export function ModelInstallOffer({
  open,
  onOpenChange,
  candidate,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  candidate: ModelInstallCandidate;
}) {
  const [installState, setInstallState] = useState<'angeboten' | 'bestaetigt' | 'abgebrochen'>(
    'angeboten',
  );

  const startInstall = () => {
    // Ausdruecklicher Nutzerakt: erst danach darf der getrennte Download
    // ueber die bestehende Modellverwaltung starten.
    setInstallState('bestaetigt');
    apiClient.triggerModelDownload(candidate.name).catch(() => {
      // Ohne Backend/Netz sichtbar als Angebot stehen bleiben — kein Fallback.
      setInstallState('angeboten');
    });
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="model-install-dialog">
        <DialogHeader>
          <DialogTitle>Modell fehlt — getrennte Installation anbieten</DialogTitle>
          <DialogDescription>
            Es erfolgt kein automatischer Netzwerkzugriff. Ohne Artefakt bleibt
            ausschließlich „{candidate.feature}“ blockiert; Transkription, Export und
            andere Pfade bleiben unberührt.
          </DialogDescription>
        </DialogHeader>

        <dl className="space-y-0.5">
          <Row label="Modell / Artefakt">{candidate.name}</Row>
          <Row label="Herkunft">{candidate.source}</Row>
          <Row label="Revision (immun)">{candidate.revision}</Row>
          <Row label="Lizenzhinweis">{candidate.license}</Row>
          {candidate.download?.size && <Row label="Größe">{candidate.download.size}</Row>}
          {candidate.download?.from && <Row label="Quelle">{candidate.download.from}</Row>}
          {candidate.download?.to && <Row label="Ziel">{candidate.download.to}</Row>}
        </dl>

        <div className="flex items-center gap-2">
          <StateChip tone={installState === 'bestaetigt' ? 'ok' : 'warn'} icon="⇩">
            Installation {installState === 'bestaetigt' ? 'ausdrücklich gestartet' : 'noch nicht gestartet'}
          </StateChip>
        </div>
        <p className="text-xs text-muted-foreground">
          Herkunfts- und Lizenzhinweis sind Pflichtbestandteil des Angebots; die
          Lizenzkette wird vor der Installation geprüft (fail-closed bei unbekannter Lizenz).
        </p>

        <DialogFooter>
          <JfwButton
            variant="ghost"
            testId="install-decline"
            onClick={() => {
              setInstallState('abgebrochen');
              onOpenChange(false);
            }}
          >
            Abbrechen
          </JfwButton>
          <JfwButton
            variant="primary"
            testId="install-confirm"
            ariaLabel={`Installation von ${candidate.name} ausdrücklich starten`}
            onClick={startInstall}
          >
            Installation ausdrücklich starten
          </JfwButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Inline-Variante ohne Dialog (z. B. im Fortschrittsblock einer Warteschlange). */
export function ModelMissingNotice({
  candidate,
  onOfferInstall,
}: {
  candidate: ModelInstallCandidate;
  onOfferInstall: () => void;
}) {
  return (
    <div
      data-testid="model-missing-notice"
      className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 space-y-2"
    >
      <StateChip tone="warn" icon="⚠">
        waiting_for_local_artifact — kein automatischer Download, kein Online-Fallback
      </StateChip>
      <p className="text-sm">
        Modellartefakt „{candidate.name}“ fehlt. Blockiert ist ausschließlich
        „{candidate.feature}“.
      </p>
      <JfwButton
        variant="primary"
        testId="offer-install"
        ariaLabel="Getrennte Installation mit Herkunfts- und Lizenzhinweis anbieten"
        onClick={onOfferInstall}
      >
        Installationsangebot öffnen (Herkunft & Lizenz)
      </JfwButton>
    </div>
  );
}
