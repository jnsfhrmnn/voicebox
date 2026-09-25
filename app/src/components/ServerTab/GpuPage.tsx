import { useQuery } from '@tanstack/react-query';
import { AlertCircle, Cpu, Loader2, ShieldCheck, Wrench, Trash2, RefreshCw, Keyboard } from 'lucide-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { invoke } from '@tauri-apps/api/core';
import type { HealthResponse } from '@/lib/api/types';
import { apiClient } from '@/lib/api/client';
import { useServerHealth } from '@/lib/hooks/useServer';
import { Button } from '@/components/ui/button';
import { ChordPicker } from '@/components/ChordPicker/ChordPicker';
import { cn } from '@/lib/utils/cn';
import { displayLabelForKey, modifierSideHint, sortChordKeys } from '@/lib/utils/keyCodes';
import { useSupervisor } from '@/features/backend/useSupervisor';
import { chordToAccelerator } from '@/features/backend/useBackendSwitchHotkey';
import { useBackendSwitchHotkeyStore } from '@/features/backend/useBackendSwitchHotkeyStore';

/**
 * jf-whisper-Profil: GPU-/Backend-Verwaltung (JFW-12 Block f, Spec A).
 *
 * Fünf Sektionen nach der User-Facing-Struktur:
 *   1. Betriebsmodus      — angefordert / tatsächlich aktiv / Modellbereitschaft + Generation / Phase
 *   2. Laufende Arbeit    — aktive und wartende Aufträge (Tasks-API)
 *   3. CUDA-Addon         — Phase, Download/Verifikation/Installation, Updateprüfung, Reparieren | Entfernen
 *   4. Wechselprogress    — geordnete Phasen: Admission → Drain → Ziel prüfen → Prozessbaum → VRAM ×2
 *   5. Letzte Switch-Evidence — inhaltsfrei (Operation, Richtung, Generationen, Ergebnis)
 *
 * Der Webview spricht den Sidecar nie für Backend-Zustand an — alles läuft über
 * die typisierten Supervisor-Commands/Events (D). Tasks sind ein separater
 * Bereich und nutzen die Tasks-API wie der Rest des Frontends.
 */

function AppleLogo({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M18.71 19.5c-.83 1.24-1.71 2.45-3.05 2.47-1.34.03-1.77-.79-3.29-.79-1.53 0-2 .77-3.27.82-1.31.05-2.3-1.32-3.14-2.53C4.25 17 2.94 12.45 4.7 9.39c.87-1.52 2.43-2.48 4.12-2.51 1.28-.02 2.5.87 3.29.87.78 0 2.26-1.07 3.8-.91.65.03 2.47.26 3.64 1.98-.09.06-2.17 1.28-2.15 3.81.03 3.02 2.65 4.03 2.68 4.04-.03.07-.42 1.44-1.38 2.83M13 3.5c.73-.83 1.94-1.46 2.94-1.5.13 1.17-.34 2.35-1.04 3.19-.69.85-1.83 1.51-2.95 1.42-.15-1.15.41-2.35 1.05-3.11z" />
    </svg>
  );
}

function GpuIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="4" y="6" width="16" height="12" rx="2" />
      <path d="M2 10h2M2 14h2M20 10h2M20 14h2" />
      <path d="M9 10h6M9 14h4" />
    </svg>
  );
}

function GpuInfoCard({ health }: { health: HealthResponse }) {
  const { t } = useTranslation();
  const hasGpu = health.gpu_available && health.gpu_type;

  const gpuName = hasGpu
    ? health.gpu_type!.replace(/^(CUDA|ROCm|MPS|Metal|XPU|DirectML)\s*\((.+)\)$/, '$2') ||
      health.gpu_type!
    : null;
  const gpuBackend = hasGpu ? health.gpu_type!.replace(/\s*\(.*\)$/, '') : null;
  const isApple = gpuBackend === 'MPS' || gpuBackend === 'Metal';

  return (
    <div className="rounded-lg border border-border/60 p-4">
      <div className="flex items-center gap-3">
        {hasGpu ? (
          isApple ? (
            <AppleLogo className="h-5 w-5 shrink-0 text-muted-foreground" />
          ) : (
            <GpuIcon className="h-5 w-5 shrink-0 text-accent" />
          )
        ) : (
          <Cpu className="h-5 w-5 shrink-0 text-muted-foreground" />
        )}
        <div className="flex-1 min-w-0 space-y-0.5">
          <div className="text-sm font-medium">{hasGpu ? gpuName : t('settings.gpu.cpuOnly')}</div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            {hasGpu ? (
              <>
                <span>{gpuBackend}</span>
                {health.vram_used_mb != null && health.vram_used_mb > 0 && (
                  <>
                    <span className="text-border">|</span>
                    <span>{t('settings.gpu.vramUsed', { mb: health.vram_used_mb.toFixed(0) })}</span>
                  </>
                )}
              </>
            ) : (
              <span>{t('settings.gpu.noAcceleration')}</span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/** Kleine Sektions-Überschrift mit Icon. */
function Section({ title, icon, children }: { title: string; icon?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="space-y-3">
      <h4 className="flex items-center gap-2 text-sm font-medium">
        {icon}
        {title}
      </h4>
      <div className="rounded-lg border border-border/60 p-4 space-y-3">{children}</div>
    </section>
  );
}

/** Status-Pill: Farbe nach Zustand. */
function Pill({ tone, children }: { tone: 'ok' | 'warn' | 'err' | 'idle'; children: React.ReactNode }) {
  const tones: Record<string, string> = {
    ok: 'border-emerald-500/40 text-emerald-600 dark:text-emerald-400',
    warn: 'border-amber-500/40 text-amber-600 dark:text-amber-400',
    err: 'border-red-500/40 text-red-600 dark:text-red-400',
    idle: 'border-border/60 text-muted-foreground',
  };
  return (
    <span className={cn('inline-flex items-center rounded-full border px-2.5 py-0.5 text-[11px] font-medium', tones[tone])}>
      {children}
    </span>
  );
}

/**
 * Deutsche Beschriftung der Supervisor-Grund-Tokens (feste Rust-Vokabel aus
 * `boot_failed(...)` in main.rs). Unbekannte Tokens bleiben sichtbar roh
 * (Fallback) — die Liste ist bewusst kein abgeschlossener Klassifikator.
 */
const REASON_LABEL_KEYS: Record<string, string> = {
  spawn_fehler: 'settings.gpu.backend.error.reasons.spawnFehler',
  boot_timeout: 'settings.gpu.backend.error.reasons.bootTimeout',
};

/**
 * Betriebsklasse der Runtime-Phase (Debug-String des Supervisor-Automaten,
 * z. B. `CpuReady(3)`, `PreparingCuda(...)`, `NoBackendReady(...)`).
 *
 * - `ready`      — Betrieb erlaubt, Aktionen sind scharf.
 * - `transition` — ein Vorgang läuft (Boot/Wechsel/Stop), Aktionen bleiben aus.
 * - `down`       — kein Backend bereit; nur Recovery/Add-on-Installation.
 * - `unknown`    — Snapshot noch nicht geladen (oder nicht verfügbar).
 */
function classifyRuntimePhase(phase: string | null): 'ready' | 'transition' | 'down' | 'unknown' {
  if (!phase) return 'unknown';
  const p = phase.toLowerCase();
  // `NoBackendReady(...)` enthält selbst „ready“ — deshalb zuerst prüfen.
  if (p.includes('nobackend')) return 'down';
  if (p.includes('ready')) return 'ready';
  if (/booting|draining|preparing|stopping|verifying/.test(p)) return 'transition';
  return 'unknown';
}

/** Geordnete Wechselprogress-Leiste: markiert die aktive Phase. */
function SwitchProgress({ phase, operationKind }: { phase: string; operationKind: string | null }) {
  const { t } = useTranslation();
  // Fünf geordnete Schritte (Spec A „Wechselprogress"). Die aktive Position wird
  // aus der Runtime-Phase abgeleitet — kein erfundener Fortschritt.
  const steps = [
    { key: 'admission', label: t('settings.gpu.backend.progress.admission') },
    { key: 'drain', label: t('settings.gpu.backend.progress.drain') },
    { key: 'verifyTarget', label: t('settings.gpu.backend.progress.verifyTarget') },
    { key: 'processTree', label: t('settings.gpu.backend.progress.processTree') },
    { key: 'vram', label: t('settings.gpu.backend.progress.vram') },
  ];

  let activeIndex = -1;
  if (operationKind) {
    const p = phase.toLowerCase();
    if (/draining/.test(p)) activeIndex = 1;
    else if (/preparing|verifying/.test(p)) activeIndex = 2;
    else if (/stopping/.test(p)) activeIndex = 3;
    else if (p.includes('ready')) activeIndex = -1; // abgeschlossen
    else activeIndex = 0; // Admission/Start
  }

  return (
    <ol className="space-y-2">
      {steps.map((s, i) => {
        const done = operationKind ? i < activeIndex : false;
        const active = i === activeIndex;
        return (
          <li key={s.key} className="flex items-center gap-3 text-xs">
            <span
              className={cn(
                'flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-[10px]',
                active && 'border-accent bg-accent/10 text-accent',
                done && 'border-emerald-500/40 text-emerald-600 dark:text-emerald-400',
                !active && !done && 'border-border/60 text-muted-foreground/50',
              )}
            >
              {i + 1}
            </span>
            <span className={cn(active ? 'text-foreground' : done ? 'text-muted-foreground' : 'text-muted-foreground/50')}>
              {s.label}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/** JFW-12 P3+: Zeile für den globalen CPU/GPU-Switch-Hotkey. */
function GlobalHotkeyRow() {
  const { t } = useTranslation();
  const chord = useBackendSwitchHotkeyStore((s) => s.chord);
  const setChord = useBackendSwitchHotkeyStore((s) => s.setChord);
  const registration = useBackendSwitchHotkeyStore((s) => s.registration);
  const [pickerOpen, setPickerOpen] = useState(false);

  const keys = sortChordKeys(chord);
  const accelerator = chordToAccelerator(keys);

  return (
    <div className="flex flex-wrap items-center gap-2 pt-1">
      <span className="text-xs text-muted-foreground">{t('settings.gpu.backend.hotkey.label')}</span>
      {accelerator ? (
        <span className="flex items-center gap-1">
          {keys.map((k) => (
            <HotkeyKey name={k} key={k} />
          ))}
        </span>
      ) : (
        <span className="text-xs text-destructive">{t('settings.gpu.backend.hotkey.invalid')}</span>
      )}
      {/* USCRX-2026-16037: sichtbarer Registrierungsstand (Spec-JFW-6:
          „keine stille Ersatzbelegung“) — dieselbe Status-Sprache wie die
          JFW-6-Vertragsfläche (RecordingRunPanel). */}
      <span
        role="status"
        aria-live="polite"
        className={cn(
          'text-xs',
          registration.state === 'failed' ? 'text-destructive' : 'text-muted-foreground',
        )}
      >
        {registration.state === 'registered'
          ? `✓ ${registration.accelerator} registriert`
          : registration.state === 'failed'
            ? `⊘ nicht registriert — ${registration.error}`
            : registration.state === 'pending'
              ? '○ wird registriert …'
              : '○ noch nicht registriert'}
      </span>
      <Button size="sm" variant="outline" onClick={() => setPickerOpen(true)}>
        <Keyboard className="mr-1 h-3.5 w-3.5" />
        {t('settings.gpu.backend.hotkey.change')}
      </Button>
      <ChordPicker
        open={pickerOpen}
        title={t('settings.gpu.backend.hotkey.pickerTitle')}
        description={t('settings.gpu.backend.hotkey.pickerDescription')}
        initialKeys={keys}
        onSave={(next) => {
          setChord(next);
          setPickerOpen(false);
        }}
        onCancel={() => setPickerOpen(false)}
      />
    </div>
  );
}

function HotkeyKey({ name }: { name: string }) {
  const side = modifierSideHint(name);
  return (
    <span
      className={cn(
        'relative inline-flex items-center justify-center h-7 min-w-[1.75rem] px-1.5',
        'rounded-md border border-border bg-background font-mono text-xs font-medium shadow-sm',
      )}
    >
      {displayLabelForKey(name)}
      {side ? (
        <span className="absolute -top-1 -right-1 h-3 min-w-[0.75rem] px-0.5 rounded-sm bg-accent text-[8px] font-bold leading-none flex items-center justify-center text-accent-foreground">
          {side}
        </span>
      ) : null}
    </span>
  );
}

export function GpuPage() {
  const { t } = useTranslation();
  const { data: health } = useServerHealth();
  const { snapshot, requestSwitch, installAddon, repairAddon, removeAddon } = useSupervisor();

  // Laufende Arbeit (Tasks-API) — separater Bereich wie CapturesTab.
  const tasksQuery = useQuery({
    queryKey: ['gpu', 'active-tasks'],
    queryFn: () => apiClient.getActiveTasks(),
    refetchInterval: 5000,
    retry: 0,
  });

  const generations = tasksQuery.data?.generations ?? [];
  const downloads = tasksQuery.data?.downloads ?? [];
  const activeJobs = generations.length;
  const waitingDownloads = downloads.length;

  // Betriebsmodus-Ableitung aus dem Snapshot.
  const phase = snapshot?.runtime_phase ?? null;
  const runtimeReason = snapshot?.runtime_reason ?? null;
  const activeVariant = snapshot?.active_variant ?? null;
  const admissionOpen = snapshot?.admission_open ?? false;
  const artifactPhase = snapshot?.artifact_phase ?? 'not_installed';
  const artifactError = snapshot?.artifact_error ?? null;

  // CUDA-Addon: ist eine Lifecycle-Operation aktiv?
  const addonBusy = ['downloading', 'verifying', 'staged'].includes(artifactPhase);
  const canSwitchToCuda = artifactPhase === 'installed' && activeVariant !== 'cuda';
  const canSwitchToCpu = activeVariant === 'cuda';

  // Echte Schaltflaechen-Zustaende: nur scharf, wenn runtime_phase den Betrieb
  // erlaubt. Sonst disabled MIT sichtbarem Grund statt toter Knoepfe.
  const runtimeClass = classifyRuntimePhase(phase);
  const switchAllowed = runtimeClass === 'ready';
  // Add-on-Lifecycle bleibt auch ohne bereites Backend zugaenglich (Recovery):
  // nur laufende Servervorgange und ein noch unbekannter Status sperren.
  const addonOpsAllowed = runtimeClass === 'ready' || runtimeClass === 'down';
  const blockedReasonKey =
    runtimeClass === 'transition'
      ? 'settings.gpu.backend.availability.transition'
      : runtimeClass === 'down'
        ? 'settings.gpu.backend.availability.notReady'
        : runtimeClass === 'unknown'
          ? 'settings.gpu.backend.availability.unknown'
          : null;

  // Phasenabhängige Zustandsmeldung für „Laufende Arbeit“ statt Blindtext.
  // `workBlockKey` ERSETZT die Liste (nicht bereit/Start), `workNoteKey` ist
  // ein Zusatz über der Liste (laufender Vorgang — „kann unvollständig
  // sein“ heißt: Liste bleibt sichtbar).
  const workBlockKey =
    runtimeClass === 'down'
      ? 'settings.gpu.backend.work.serverNotReady'
      : phase && /booting/i.test(phase)
        ? 'settings.gpu.backend.work.serverStarting'
        : null;
  const workNoteKey =
    !workBlockKey && runtimeClass === 'transition'
      ? 'settings.gpu.backend.work.serverBusy'
      : null;

  // Recovery-Pfad: Server ueber das Tauri-Kommando `restart_server` neu starten
  // (application/tauri/src-tauri/src/main.rs). Fehler landen im Fehlerblock.
  const [restarting, setRestarting] = useState(false);
  const [restartError, setRestartError] = useState<string | null>(null);
  const restartServer = async () => {
    setRestarting(true);
    setRestartError(null);
    try {
      await invoke('restart_server');
    } catch (e) {
      setRestartError(typeof e === 'string' ? e : e instanceof Error ? e.message : String(e));
    } finally {
      setRestarting(false);
    }
  };

  return (
    <div className="space-y-8 max-w-2xl">
      {/* Geräteinfo (Health-basiert, unverändert) */}
      {health && <GpuInfoCard health={health} />}

      {/* Sichtbarer Fehlerzustand (USCRX-2026-16039 Punkt 3): Fehlertexte
          aus dem Supervisor-Snapshot — Text, nicht nur Farbe. */}
      {(runtimeReason || artifactError || restartError) && (
        <section
          role="alert"
          className="rounded-lg border border-red-500/40 bg-red-500/5 p-4 space-y-2"
        >
          <h4 className="flex items-center gap-2 text-sm font-medium text-red-600 dark:text-red-400">
            <AlertCircle className="h-4 w-4 shrink-0" />
            {t('settings.gpu.backend.error.title')}
          </h4>
          <ul className="space-y-1.5 text-xs">
            {runtimeReason && (
              <li className="flex flex-wrap items-baseline gap-x-2">
                <span className="font-medium text-red-600 dark:text-red-400">
                  {t('settings.gpu.backend.error.runtimeLabel')}
                </span>
                <span className="break-words text-foreground/90">
                  {REASON_LABEL_KEYS[runtimeReason]
                    ? t(REASON_LABEL_KEYS[runtimeReason])
                    : runtimeReason}
                </span>
              </li>
            )}
            {artifactError && (
              <li className="flex flex-wrap items-baseline gap-x-2">
                <span className="font-medium text-red-600 dark:text-red-400">
                  {t('settings.gpu.backend.error.artifactLabel')}
                </span>
                <span className="break-words text-foreground/90">{artifactError}</span>
              </li>
            )}
            {restartError && (
              <li className="flex flex-wrap items-baseline gap-x-2">
                <span className="font-medium text-red-600 dark:text-red-400">
                  {t('settings.gpu.backend.error.restartLabel')}
                </span>
                <span className="break-words text-foreground/90">{restartError}</span>
              </li>
            )}
          </ul>
        </section>
      )}

      {/* 1. Betriebsmodus */}
      <Section title={t('settings.gpu.backend.mode.title')} icon={<Cpu className="h-4 w-4 text-muted-foreground" />}>
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm">
          <div>
            <div className="text-xs text-muted-foreground">{t('settings.gpu.backend.mode.requested')}</div>
            <div className="font-medium">{activeVariant ?? t('settings.gpu.backend.mode.none')}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">{t('settings.gpu.backend.mode.active')}</div>
            <div className="flex items-center gap-2">
              <span className="font-medium">{activeVariant ?? t('settings.gpu.backend.mode.none')}</span>
              {admissionOpen && (
                <Pill tone="ok">
                  <ShieldCheck className="mr-1 h-3 w-3" />
                  {t('settings.gpu.backend.mode.admissionOpen')}
                </Pill>
              )}
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">{t('settings.gpu.backend.mode.generation')}</div>
            <div className="font-mono text-xs">{snapshot?.generation ?? '—'}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">{t('settings.gpu.backend.mode.phase')}</div>
            <div className="text-xs font-medium">{phase ?? t('settings.gpu.backend.mode.unknown')}</div>
          </div>
        </div>

        {/* Switch-Aktionen — nur scharf, wenn runtime_phase den Betrieb erlaubt. */}
        <div className="flex flex-wrap gap-2 pt-1">
          <Button
            size="sm"
            variant={canSwitchToCuda && switchAllowed ? 'default' : 'outline'}
            disabled={!canSwitchToCuda || !switchAllowed}
            onClick={() => void requestSwitch('cuda')}
          >
            {t('settings.gpu.backend.switch.toCuda')}
          </Button>
          <Button
            size="sm"
            variant={canSwitchToCpu && switchAllowed ? 'default' : 'outline'}
            disabled={!canSwitchToCpu || !switchAllowed}
            onClick={() => void requestSwitch('cpu')}
          >
            {t('settings.gpu.backend.switch.toCpu')}
          </Button>
        </div>
        {!switchAllowed && blockedReasonKey && (
          <p className="text-xs text-muted-foreground" role="status">
            ⊘ {t(blockedReasonKey)}
          </p>
        )}

        {/* Recovery-Pfad: Server neu starten (echtes Tauri-Kommando
            `restart_server`; Fehler erscheinen im Fehlerblock oben). */}
        <div className="flex flex-wrap items-center gap-3 pt-1">
          <Button size="sm" variant="outline" disabled={restarting} onClick={() => void restartServer()}>
            <RefreshCw className={cn('mr-1 h-3.5 w-3.5', restarting && 'animate-spin')} />
            {restarting
              ? t('settings.gpu.backend.recovery.restarting')
              : t('settings.gpu.backend.recovery.restart')}
          </Button>
          <p className="text-xs text-muted-foreground/60">{t('settings.gpu.backend.recovery.restartHint')}</p>
        </div>

        {/* JFW-12 P3+: Globaler Switch-Hotkey — frei in der UI belegbar. */}
        <GlobalHotkeyRow />
      </Section>

      {/* 2. Laufende Arbeit */}
      <Section title={t('settings.gpu.backend.work.title')}>
        {workNoteKey && (
          <p className="text-xs text-muted-foreground/60" role="status">
            {t(workNoteKey)}
          </p>
        )}
        {workBlockKey ? (
          <p className="text-xs text-muted-foreground/60">{t(workBlockKey)}</p>
        ) : tasksQuery.isError ? (
          <p className="text-xs text-muted-foreground/60">{t('settings.gpu.backend.work.loadFailed')}</p>
        ) : tasksQuery.isLoading ? (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            {t('settings.gpu.backend.work.loadingTasks')}
          </div>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm">
              <div>
                <span className="text-muted-foreground">{t('settings.gpu.backend.work.active')}: </span>
                <span className="font-medium">{activeJobs}</span>
              </div>
              <div>
                <span className="text-muted-foreground">{t('settings.gpu.backend.work.waiting')}: </span>
                <span className="font-medium">{waitingDownloads}</span>
              </div>
            </div>
            {activeJobs === 0 && waitingDownloads === 0 ? (
              <p className="text-xs text-muted-foreground/60">{t('settings.gpu.backend.work.none')}</p>
            ) : (
              <ul className="space-y-1">
                {generations.map((g) => (
                  <li key={g.task_id} className="flex items-center gap-2 text-xs">
                    <Loader2 className="h-3 w-3 animate-spin text-accent" />
                    <span className="truncate font-mono">{g.profile_id}</span>
                  </li>
                ))}
                {downloads.map((d) => (
                  <li key={d.model_name} className="flex items-center gap-2 text-xs">
                    <Loader2 className="h-3 w-3 animate-spin text-accent" />
                    <span className="truncate">{d.model_name}</span>
                    {typeof d.progress === 'number' && (
                      <span className="ml-auto text-muted-foreground/60">{Math.round(d.progress)}%</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </Section>

      {/* 3. CUDA-Addon */}
      <Section title={t('settings.gpu.backend.addon.title')} icon={<Wrench className="h-4 w-4 text-muted-foreground" />}>
        <div className="flex items-center justify-between">
          <Pill tone={artifactPhase === 'installed' ? 'ok' : artifactError ? 'err' : addonBusy ? 'warn' : 'idle'}>
            {t(`settings.gpu.backend.addon.phase.${artifactPhase}`)}
          </Pill>
          {addonBusy && (
            <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              {t('settings.gpu.backend.addon.busy')}
            </span>
          )}
        </div>

        {/* Addon-Fehler stehen im eigenen Fehlerblock oben (mit Label statt
            nur Farbe) — hier keine Doppelanzeige. */}

        <div className="flex flex-wrap gap-2 pt-1">
          {(artifactPhase === 'not_installed' || artifactPhase === 'repair_required') && (
            <Button size="sm" variant={artifactPhase === 'repair_required' ? 'outline' : 'default'} disabled={addonBusy || !addonOpsAllowed} onClick={() => void installAddon()}>
              {t('settings.gpu.backend.addon.install')}
            </Button>
          )}
          {artifactPhase === 'installed' && (
            <Button size="sm" variant="outline" disabled={addonBusy || !addonOpsAllowed} onClick={() => void repairAddon()}>
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
              {t('settings.gpu.backend.addon.checkUpdate')}
            </Button>
          )}
          {artifactPhase === 'repair_required' && (
            <Button size="sm" disabled={addonBusy || !addonOpsAllowed} onClick={() => void repairAddon()}>
              <Wrench className="mr-1 h-3.5 w-3.5" />
              {t('settings.gpu.backend.addon.repair')}
            </Button>
          )}
          {(artifactPhase === 'installed' || artifactPhase === 'repair_required') && (
            <Button size="sm" variant="ghost" disabled={addonBusy || !addonOpsAllowed} onClick={() => void removeAddon()}>
              <Trash2 className="mr-1 h-3.5 w-3.5" />
              {t('settings.gpu.backend.addon.remove')}
            </Button>
          )}
        </div>
        {!addonOpsAllowed && !addonBusy && blockedReasonKey && (
          <p className="text-xs text-muted-foreground" role="status">
            ⊘ {t(blockedReasonKey)}
          </p>
        )}
      </Section>

      {/* 4. Wechselprogress */}
      <Section title={t('settings.gpu.backend.progress.title')}>
        <SwitchProgress phase={phase ?? ''} operationKind={snapshot?.operation_kind ?? null} />
      </Section>

      {/* 5. Letzte Switch-Evidence (inhaltsfrei) */}
      <Section title={t('settings.gpu.backend.evidence.title')}>
        {snapshot && snapshot.operation_id ? (
          <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs">
            <dt className="text-muted-foreground">{t('settings.gpu.backend.evidence.operation')}</dt>
            <dd className="font-mono">{snapshot.operation_kind}</dd>
            <dt className="text-muted-foreground">{t('settings.gpu.backend.evidence.generation')}</dt>
            <dd className="font-mono">{snapshot.generation}</dd>
            <dt className="text-muted-foreground">{t('settings.gpu.backend.evidence.result')}</dt>
            <dd>{phase}</dd>
          </dl>
        ) : (
          <p className="text-xs text-muted-foreground/60">{t('settings.gpu.backend.evidence.none')}</p>
        )}
      </Section>

      <p className="text-xs text-muted-foreground/60 leading-relaxed">{t('settings.gpu.footer')}</p>
    </div>
  );
}
