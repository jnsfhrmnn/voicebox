/**
 * JFW-12 B2/D (Surface): Typisierte Supervisor-Commands und Events.
 *
 * Der Webview spricht den Sidecar nie direkt an — alle Backend-Zustandsfragen
 * laufen ueber diese Tauri-Commands, Zustandsaenderungen kommen als typisiertes
 * Event `backend:supervisor`. Keine direkte Sidecar-URL, kein EventSource (D).
 */

import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';

/** Produktive Backend-Variante. */
export type BackendVariant = 'cpu' | 'cuda';

/** Typisierter Snapshot des BackendSupervisors (C: nur RAM, kein Webview-Zugriff). */
export interface SupervisorSnapshot {
  app_epoch: string;
  generation: number;
  runtime_phase: string;
  active_variant: BackendVariant | null;
  admission_open: boolean;
  artifact_phase: 'not_installed' | 'downloading' | 'verifying' | 'staged' | 'installed' | 'repair_required' | 'removing';
  operation_id: string | null;
  operation_kind: 'switch_to_cuda' | 'switch_to_cpu' | 'install_addon' | 'repair_addon' | 'remove_addon' | null;
}

/** Admission-Entscheidung fuer einen neuen Job (B5 Schritt 1). */
export interface AdmissionOutcome {
  admitted: boolean;
  generation: number | null;
  backend_variant: BackendVariant | null;
  reason: string;
}

const SUPERVISOR_EVENT = 'backend:supervisor';

/** Aktueller typisierter Supervisor-Snapshot. */
export function getSupervisorSnapshot(): Promise<SupervisorSnapshot> {
  return invoke<SupervisorSnapshot>('supervisor_snapshot');
}

/** Admission-Entscheidung; `requestedGeneration` optional (Stale-Fence, B7). */
export function admitJob(requestedGeneration?: number): Promise<AdmissionOutcome> {
  return invoke<AdmissionOutcome>('supervisor_admit', { requested_generation: requestedGeneration ?? null });
}

/** Nutzerinitiiertes Backend-Switch-Request von der GPU-Seite (B9). */
export function requestBackendSwitch(target: BackendVariant): Promise<void> {
  return invoke('request_backend_switch', { target });
}

/** Abonniert typisierte Supervisor-Zustandsaenderungen; liefert Unlisten. */
export function onSupervisorChange(callback: (snapshot: SupervisorSnapshot) => void): Promise<UnlistenFn> {
  return listen<SupervisorSnapshot>(SUPERVISOR_EVENT, (event) => callback(event.payload));
}
