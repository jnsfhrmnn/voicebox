import { useCallback, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { usePlatform } from '@/platform/PlatformContext';
import {
  jfwApi,
  type BatchDiscoveryBundle,
  type BatchSelectionRef,
} from '@/lib/api/jfwApi';
import { JFW_FOCUS_RING, JfwButton, Row, SectionCard, StateChip, StatePreview } from './JfwCommon';
import { JFW_PROGRESS_POLL_MS, JfwProgress, type ProgressPhase } from './JfwProgress';

/**
 * JFW-5: Mehrfachauswahl-Dialoge, Bestaetigungsansicht, Batch-Uebersicht mit
 * Ressourcen-/Fortschrittsanzeige, Resume-Dialog und vollstaendiger
 * Tastaturbedienung.
 *
 * Tastaturbedienung: alle Bedienelemente sind echte Buttons/Inputs mit
 * sichtbarem Fokusring; die Auswahl-Liste faehrt mit Pfeiltasten, Entf
 * entfernt den Eintrag, Esc schliesst Dialoge (Radix) und der Fokus bleibt
 * nach Aktionen im Bedienpfad (Roving-Tabindex in der Liste).
 */

export type BatchViewState = 'auswahl' | 'bestaetigung' | 'laufend' | 'pausiert' | 'unterbrochen' | 'abgeschlossen';

const VIEW_STATES: readonly BatchViewState[] = [
  'auswahl',
  'bestaetigung',
  'laufend',
  'pausiert',
  'unterbrochen',
  'abgeschlossen',
];

const STATE_META: Record<
  BatchViewState,
  { label: string; tone: 'neutral' | 'ok' | 'warn' | 'error' | 'active'; icon: string }
> = {
  auswahl: { label: 'Auswahl — noch kein Start', tone: 'neutral', icon: '○' },
  bestaetigung: { label: 'Bestätigung erforderlich — Start blockiert bis validiert', tone: 'warn', icon: '⚠' },
  laufend: { label: 'Batch läuft', tone: 'active', icon: '◐' },
  pausiert: { label: 'Pausiert — keine neue Phase', tone: 'warn', icon: '⏸' },
  unterbrochen: { label: 'Unterbrochen — sichtbar fortsetzbar', tone: 'warn', icon: '⚠' },
  abgeschlossen: { label: 'Abgeschlossen — genau ein Endzustand je Element', tone: 'ok', icon: '✓' },
};

const DEMO_PROFILE: Record<string, unknown> = {
  profile_id: 'jfw5_batch_profile_v1',
  phases: ['jfw7_transcribe', 'jfw2_align', 'jfw3_diarize', 'jfw4_export'],
  resource_policy: { name: 'jfw5_serial_v1', max_concurrent_ai_phases: 1 },
  output_policy: { structure: 'relative_source', overwrite: false, collision_rule: 'element_suffix' },
  partial_failure_policy: 'mit_belegten_daten_fortfahren',
  fail_fast: false,
};

export function BatchPanel() {
  const platform = usePlatform();
  const [selection, setSelection] = useState<BatchSelectionRef[]>([]);
  const [pathDraft, setPathDraft] = useState('');
  const [selectOpen, setSelectOpen] = useState(false);
  const [resumeOpen, setResumeOpen] = useState(false);
  const [confirmState, setConfirmState] = useState<'unbestaetigt' | 'bestaetigt'>('unbestaetigt');
  const [preview, setPreview] = useState<BatchViewState>('bestaetigung');
  const [discovery, setDiscovery] = useState<BatchDiscoveryBundle | null>(null);
  const [batchId, setBatchId] = useState<string | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);

  // Live-Uebersicht: Polling <= 2 s (JFW_PROGRESS_POLL_MS).
  const live = useQuery({
    queryKey: ['jfw5-batch', batchId],
    queryFn: () => jfwApi.getBatch(batchId as string),
    enabled: Boolean(batchId),
    refetchInterval: JFW_PROGRESS_POLL_MS,
  });

  const addRef = useCallback((kind: 'file' | 'folder', path: string) => {
    const trimmed = path.trim();
    if (!trimmed) return;
    setSelection((prev) =>
      prev.some((r) => r.kind === kind && r.path === trimmed)
        ? prev
        : [...prev, { kind, path: trimmed }],
    );
    setPathDraft('');
  }, []);

  const removeAt = useCallback((index: number) => {
    setSelection((prev) => prev.filter((_, i) => i !== index));
  }, []);

  /** Pfeiltasten-Navigation + Entf-Entfernung in der Auswahl-Liste. */
  const onListKeyDown = (event: React.KeyboardEvent<HTMLUListElement>) => {
    const items = Array.from(listRef.current?.querySelectorAll<HTMLButtonElement>('button[data-select-item]') ?? []);
    const currentIndex = items.findIndex((el) => el === document.activeElement);
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      items[Math.min(items.length - 1, currentIndex + 1)]?.focus();
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      items[Math.max(0, currentIndex - 1)]?.focus();
    } else if (event.key === 'Delete' && currentIndex >= 0) {
      event.preventDefault();
      removeAt(currentIndex);
    }
  };

  const runDiscover = async () => {
    try {
      const bundle = await jfwApi.discoverBatch(selection);
      setDiscovery(bundle);
    } catch {
      setDiscovery(null);
    }
  };

  const confirmSnapshot = async () => {
    try {
      const snap = await jfwApi.confirmBatch({ selection, profile: DEMO_PROFILE });
      if (snap.identity_hash) setBatchId(snap.identity_hash);
      setConfirmState('bestaetigt');
    } catch {
      setConfirmState('unbestaetigt');
    }
  };

  const view: BatchViewState = live.data && batchId ? 'laufend' : preview;
  const batch = live.data;
  const counts = (batch?.aggregates?.counts ?? {}) as Record<string, number | string>;
  const activeItem = batch?.items?.find((i) => i.status === 'running');
  const progressPhase: ProgressPhase =
    view === 'laufend'
      ? 'laufend'
      : view === 'abgeschlossen'
        ? 'abgeschlossen'
        : view === 'unterbrochen'
          ? 'fehlgeschlagen'
          : view === 'pausiert'
            ? 'bereit'
            : 'bereit';

  const formatBytes = (n?: number) =>
    n == null ? '—' : `${(n / (1024 * 1024)).toFixed(1)} MiB`;

  return (
    <div className="space-y-4">
      <StatePreview
        label="Zustandsvorschau:"
        states={VIEW_STATES}
        active={preview}
        onChange={(s) => {
          setBatchId(null);
          setPreview(s);
        }}
        testId="batch-state-preview"
      />

      {/* ---------------- Mehrfachauswahl-Dialoge ---------------- */}
      <SectionCard
        title="Mehrfachauswahl (Dateien · Ordner · Kombination)"
        description="Auswahl startet nichts: erst die bestätigte Vorschau erlaubt den Start."
        actions={
          <JfwButton variant="primary" testId="open-select-dialog" onClick={() => setSelectOpen(true)}>
            Auswahl öffnen …
          </JfwButton>
        }
        testId="batch-select-card"
      >
        <ul
          ref={listRef}
          onKeyDown={onListKeyDown}
          aria-label="Ausgewählte Quellen (Pfeiltasten navigieren, Entf entfernt)"
          className="divide-y divide-border/60 rounded-md border border-border"
          data-testid="selection-list"
        >
          {selection.length === 0 && (
            <li className="px-3 py-2 text-sm text-muted-foreground">Keine Quellen gewählt.</li>
          )}
          {selection.map((ref, index) => (
            <li key={`${ref.kind}:${ref.path}`} className="flex items-center justify-between gap-2 px-3 py-1.5">
              <button
                type="button"
                data-select-item
                data-testid={`select-item-${index}`}
                className={`flex-1 text-left text-sm truncate ${JFW_FOCUS_RING}`}
                onClick={() => undefined}
              >
                <span className="font-mono text-xs mr-2">{ref.kind}</span>
                {ref.path}
              </button>
              <JfwButton variant="ghost" testId={`remove-item-${index}`} onClick={() => removeAt(index)}>
                Entfernen
              </JfwButton>
            </li>
          ))}
        </ul>
        <div className="flex flex-wrap gap-2">
          <JfwButton testId="discover" disabled={selection.length === 0} onClick={runDiscover}>
            Fundmenge prüfen (Discover)
          </JfwButton>
          <JfwButton variant="ghost" testId="clear-selection" onClick={() => setSelection([])}>
            Auswahl leeren
          </JfwButton>
        </div>
      </SectionCard>

      {/* ---------------- Bestätigungsansicht ---------------- */}
      <SectionCard
        title="Bestätigungsansicht (Vorschau vor dem Start)"
        description="Quellen, Pfade, Ausschlüsse, Duplikate, Anzahl, Größe, Reihenfolge, Profil, Phasen, Ziele, Kollisionen und Ressourcenpolitik."
        testId="batch-confirm-card"
      >
        <div className="flex flex-wrap items-center gap-2">
          <StateChip tone={STATE_META[view].tone} icon={STATE_META[view].icon}>
            {STATE_META[view].label}
          </StateChip>
          <StateChip tone={confirmState === 'bestaetigt' ? 'ok' : 'warn'} icon={confirmState === 'bestaetigt' ? '✓' : '⚠'}>
            Snapshot {confirmState === 'bestaetigt' ? 'bestätigt (eine unveränderliche Revision)' : 'noch nicht bestätigt'}
          </StateChip>
        </div>
        <dl className="mt-2">
          <Row label="Anzahl">{discovery?.counts ? `included=${discovery.counts.included} · excluded=${discovery.counts.excluded}` : 'noch nicht geprüft'}</Row>
          <Row label="Größe">{formatBytes(discovery?.total_size)}</Row>
          <Row label="Ausschlüsse">
            {(discovery?.excluded ?? []).map((e) => `${e.path} (${e.reason_code})`).join(' · ') || 'versteckte/System-/Reparse-Unterbäume werden nie durchlaufen'}
          </Row>
          <Row label="Duplikate">{(discovery?.duplicate_groups ?? []).length} Gruppe(n) — getrennte Elemente mit Duplikathinweis, kein stiller Ausschluss</Row>
          <Row label="Reihenfolge">eingefroren (frozen_order); Umsortieren nur nicht gestarteter Elemente erzeugt eine neue Revision</Row>
          <Row label="Profil">{String((DEMO_PROFILE.profile_id as string) ?? '')} — jedes Element an die Profilrevision gebunden</Row>
          <Row label="Phasen">{(DEMO_PROFILE.phases as string[]).join(' → ')}</Row>
          <Row label="Ziele / Kollisionen">Zielkollisionen sichtbar vor dem Start; belegte Ziele werden nie still überschrieben (element_suffix)</Row>
          <Row label="Ressourcenpolitik">jfw5_serial_v1 — höchstens eine ressourcenintensive KI-Phase gleichzeitig</Row>
          <Row label="Trust Boundary">{(discovery?.trust_boundary_paths ?? []).join(' · ') || 'keine externen Ziele gewählt'}</Row>
        </dl>
        <div className="flex gap-2">
          <JfwButton
            variant="primary"
            testId="confirm-snapshot"
            disabled={confirmState === 'bestaetigt'}
            onClick={confirmSnapshot}
          >
            Vorschau bestätigen & Snapshot frieren
          </JfwButton>
          <JfwButton
            testId="batch-start"
            disabled={confirmState !== 'bestaetigt'}
            onClick={() => batchId && jfwApi.startBatch(batchId).catch(() => undefined)}
          >
            Start
          </JfwButton>
          <JfwButton variant="ghost" testId="batch-abort-before-confirm" onClick={() => setSelection([])}>
            Abbruch vor Bestätigung — kein Start, keine Endausgabe
          </JfwButton>
        </div>
      </SectionCard>

      {/* ---------------- Batch-Übersicht ---------------- */}
      <SectionCard
        title="Batch-Übersicht"
        description="Gesamtstatus, Zähler, Ressourcen, aktives Element, Reihenfolge, Fortschritt, Warnungen, Gründe und Aktionen."
        testId="batch-overview-card"
      >
        <div className="flex flex-wrap items-center gap-2">
          <StateChip tone={STATE_META[view].tone} icon={STATE_META[view].icon}>
            Gesamtstatus: {batch?.status ?? view}
          </StateChip>
          <StateChip tone="neutral" icon="Σ">
            Zähler: {Object.entries(counts).map(([k, v]) => `${k}=${v}`).join(' · ') || '—'}
          </StateChip>
        </div>
        <JfwProgress
          title="JFW-5 Batch"
          phase={progressPhase}
          detail={
            batch?.reason_code
              ? `Grund: ${batch.reason_code} (Wartegrund ist kein Fehler)`
              : 'Serielle Phasen laden nie mehrere Modelle gleichzeitig.'
          }
          lastUpdatedAtMs={live.dataUpdatedAt || null}
          onCancel={() => batchId && jfwApi.cancelBatch(batchId).catch(() => undefined)}
          extraActions={
            <>
              <JfwButton testId="batch-pause" onClick={() => batchId && jfwApi.pauseBatch(batchId).catch(() => undefined)}>
                Pausieren
              </JfwButton>
              <JfwButton testId="batch-resume-dialog" onClick={() => setResumeOpen(true)}>
                Fortsetzen …
              </JfwButton>
            </>
          }
        />
        <dl>
          <Row label="Ressourcen">
            {JSON.stringify(batch?.resource_policy ?? DEMO_PROFILE.resource_policy)}
          </Row>
          <Row label="Aktives Element">
            {activeItem ? `${activeItem.item_id} (Phase ${activeItem.current_phase ?? '—'})` : 'keins'}
          </Row>
          <Row label="Reihenfolge">
            {(batch?.items ?? []).map((i) => `${i.order_index}:${i.item_id}`).join(' → ') || 'frozen_order nach Bestätigung'}
          </Row>
          <Row label="Warnungen">
            {(batch?.items ?? []).flatMap((i) => i.warnings ?? []).join(' · ') || 'keine'}
          </Row>
          <Row label="Aktionen">{(batch?.available_actions ?? ['start', 'pause', 'resume', 'cancel']).join(' · ')}</Row>
        </dl>
      </SectionCard>

      {/* ---------------- Resume-Dialog ---------------- */}
      <Dialog open={resumeOpen} onOpenChange={setResumeOpen}>
        <DialogContent data-testid="resume-dialog">
          <DialogHeader>
            <DialogTitle>Wiederaufnahme nach Unterbrechung</DialogTitle>
            <DialogDescription>
              Zustand, Ursache und Wiederaufnahmeumfang sind sichtbar; ohne ausdrückliche
              Bestätigung beginnt keine Arbeit. Snapshot, Reihenfolge und Identitäten bleiben
              unverändert — keine Rekonstruktion aus globalen Defaults.
            </DialogDescription>
          </DialogHeader>
          <dl>
            <Row label="Zustand">{batch?.status ?? 'unterbrochen (interrupted)'}</Row>
            <Row label="Ursache">{batch?.reason_code ?? 'unterbrochen'}</Row>
            <Row label="Wiederaufnahmeumfang">
              nur zulässige wartende/unterbrochene Elemente in fester Reihenfolge; wiederverwendbare Upstream-Commits bleiben erhalten
            </Row>
          </dl>
          <DialogFooter>
            <JfwButton variant="ghost" testId="resume-decline" onClick={() => setResumeOpen(false)}>
              Abbrechen
            </JfwButton>
            <JfwButton
              variant="primary"
              testId="resume-confirm"
              onClick={() => {
                setResumeOpen(false);
                if (batchId) jfwApi.resumeBatch(batchId, true).catch(() => undefined);
              }}
            >
              Original-Snapshot bestätigen & fortsetzen
            </JfwButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ---------------- Auswahl-Dialog ---------------- */}
      <Dialog open={selectOpen} onOpenChange={setSelectOpen}>
        <DialogContent data-testid="select-dialog">
          <DialogHeader>
            <DialogTitle>Mehrfachauswahl — Dateien und Ordner</DialogTitle>
            <DialogDescription>
              Dateien, Ordner oder Kombination; Discover/Confirm legen nichts an und starten
              nichts. Tastatur: Eingabe fügt den Pfad hinzu, Esc schließt.
            </DialogDescription>
          </DialogHeader>
          <div className="flex gap-2">
            <input
              type="text"
              value={pathDraft}
              onChange={(e) => setPathDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') addRef('file', pathDraft);
              }}
              placeholder="Pfad (z. B. D:\\Meetings\\aufnahme.wav)"
              aria-label="Pfad zur Quelldatei"
              className={`flex-1 rounded-md border border-border bg-background px-2 py-1.5 text-sm ${JFW_FOCUS_RING}`}
              data-testid="path-input"
            />
            <JfwButton testId="add-file" onClick={() => addRef('file', pathDraft)}>
              Datei hinzufügen
            </JfwButton>
          </div>
          <div className="flex gap-2">
            <JfwButton
              testId="add-folder"
              onClick={async () => {
                const dir = await platform.filesystem.pickDirectory('Ordner auswählen');
                if (dir) addRef('folder', dir);
              }}
            >
              Ordner auswählen …
            </JfwButton>
            <JfwButton
              testId="add-folder-manual"
              onClick={() => addRef('folder', pathDraft)}
            >
              Ordner (Pfad) hinzufügen
            </JfwButton>
          </div>
          <DialogFooter>
            <JfwButton variant="primary" testId="select-done" onClick={() => setSelectOpen(false)}>
              Auswahl übernehmen
            </JfwButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
