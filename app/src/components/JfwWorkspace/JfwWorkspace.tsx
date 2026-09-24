import { useState } from 'react';
import { cn } from '@/lib/utils/cn';
import { TOP_SAFE_AREA_PADDING } from '@/lib/constants/ui';
import { JFW_FOCUS_RING } from './JfwCommon';
import { RecordingRunPanel } from './RecordingRunPanel';
import { DictationAlignmentPanel } from './DictationAlignmentPanel';
import { DiarizationQualityPanel } from './DiarizationQualityPanel';
import { ExportPanel } from './ExportPanel';
import { MinutesPanel } from './MinutesPanel';
import { BatchPanel } from './BatchPanel';
import { DeliveryPanel } from './DeliveryPanel';

/**
 * Arbeitsraum „Vorgänge" — die Bedienoberflaechen des JFW-Folge-Blocks.
 *
 * Jeder Screen bildet die in den Specs verlangten Zustaende und Texte ab
 * (Aufnahme-Pill/RunSession, Fortschrittsanzeige ≤ 2 s, Installationsangebot,
 * Qualitaetsansicht mit Sprecheranzahl-Vorgabe, Bestaetigungs-/Freigabe-/
 * Abbruchdialoge mit Trust-Boundary- und Loeschhinweis, Batch-Uebersicht mit
 * Resume-Dialog, Zieluebergabe-Zustaende, Re-Identifizierungshinweis).
 * Tastaturbedienung: die Screen-Auswahl ist eine Tastaturliste (Pfeiltasten,
 * Enter/Leertaste), alle Bedienelemente sind fokussierbar.
 */

type ScreenId =
  | 'aufnahme'
  | 'diktat'
  | 'qualitaet'
  | 'export'
  | 'protokoll'
  | 'batch'
  | 'ziel';

const SCREENS: readonly { id: ScreenId; title: string; subtitle: string }[] = [
  { id: 'aufnahme', title: 'Aufnahme & Pill', subtitle: 'JFW-6: RunSession-Pill, Sound-Cues, Shift+Shift' },
  { id: 'diktat', title: 'Diktat & Alignment', subtitle: 'JFW-7/JFW-2: Fortschritt ≤ 2 s, Installationsangebot' },
  { id: 'qualitaet', title: 'Qualität & Sprecher', subtitle: 'JFW-3: Sprecheranzahl-Vorgabe, Qualitätsansicht' },
  { id: 'export', title: 'Export', subtitle: 'JFW-4: Bestätigung, Freigabe/Abbruch, Trust Boundary' },
  { id: 'protokoll', title: 'Protokoll', subtitle: 'JFW-13: Re-Identifizierungshinweis, fortsetzbar' },
  { id: 'batch', title: 'Batch', subtitle: 'JFW-5: Auswahl, Übersicht, Resume-Dialog, Tastatur' },
  { id: 'ziel', title: 'Zielübergabe', subtitle: 'JFW-8: Ziel bestätigen · Manuell kopieren · …' },
];

export function JfwWorkspace() {
  const [screen, setScreen] = useState<ScreenId>('aufnahme');

  const onNavKeyDown = (event: React.KeyboardEvent<HTMLElement>) => {
    const index = SCREENS.findIndex((s) => s.id === screen);
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setScreen(SCREENS[(index + 1) % SCREENS.length].id);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setScreen(SCREENS[(index - 1 + SCREENS.length) % SCREENS.length].id);
    }
  };

  return (
    <div className={cn('flex-1 min-h-0 overflow-hidden flex gap-6 py-6', TOP_SAFE_AREA_PADDING)}>
      <nav
        aria-label="Vorgänge (JFW-Folge-Block)"
        onKeyDown={onNavKeyDown}
        className="w-56 shrink-0 space-y-1"
        data-testid="jfw-nav"
      >
        {SCREENS.map((s) => (
          <button
            key={s.id}
            type="button"
            aria-current={screen === s.id ? 'page' : undefined}
            tabIndex={screen === s.id ? 0 : -1}
            data-testid={`nav-${s.id}`}
            onClick={() => setScreen(s.id)}
            className={cn(
              'block w-full rounded-lg px-3 py-2 text-left transition-colors',
              JFW_FOCUS_RING,
              screen === s.id
                ? 'bg-accent/10 text-foreground font-medium ring-1 ring-accent/40'
                : 'text-muted-foreground hover:bg-muted',
            )}
          >
            <span className="block text-sm">{s.title}</span>
            <span className="block text-xs text-muted-foreground/80">{s.subtitle}</span>
          </button>
        ))}
      </nav>

      <div className="flex-1 min-w-0 overflow-y-auto pr-2" data-testid={`screen-${screen}`}>
        {screen === 'aufnahme' && <RecordingRunPanel />}
        {screen === 'diktat' && <DictationAlignmentPanel />}
        {screen === 'qualitaet' && <DiarizationQualityPanel />}
        {screen === 'export' && <ExportPanel />}
        {screen === 'protokoll' && <MinutesPanel />}
        {screen === 'batch' && <BatchPanel />}
        {screen === 'ziel' && <DeliveryPanel />}
      </div>
    </div>
  );
}
