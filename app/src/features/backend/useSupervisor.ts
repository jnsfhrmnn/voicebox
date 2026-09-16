/**
 * JFW-12 B2/D: React-Hook fuer den BackendSupervisor.
 *
 * Liest den initialen Snapshot ueber `supervisor_snapshot` und abonniert danach
 * typisierte Aenderungen ueber das Event `backend:supervisor`. Der Webview
 * spricht den Sidecar nie direkt an (D: Surface).
 */

import { useCallback, useEffect, useState } from 'react';
import type { AdmissionOutcome, BackendVariant, SupervisorSnapshot } from './supervisor';
import { admitJob, getSupervisorSnapshot, installCudaAddon, onSupervisorChange, removeCudaAddon, repairCudaAddon, requestBackendSwitch } from './supervisor';

export interface UseSupervisorResult {
  snapshot: SupervisorSnapshot | null;
  /** Admission-Entscheidung fuer einen neuen Job (B5 Schritt 1). */
  admit: (requestedGeneration?: number) => Promise<AdmissionOutcome>;
  /** Nutzerinitiiertes Backend-Switch-Request (B9); CUDA fail-closed bis Block (d). */
  requestSwitch: (target: BackendVariant) => Promise<void>;
  /** CUDA-Addon installieren (Block d, B9 — nur nutzerinitiiert). */
  installAddon: () => Promise<void>;
  /** Defektes Addon reparieren (Block d, B9). */
  repairAddon: () => Promise<void>;
  /** Installiertes Addon entfernen (Block d, B9). */
  removeAddon: () => Promise<void>;
}

export function useSupervisor(): UseSupervisorResult {
  const [snapshot, setSnapshot] = useState<SupervisorSnapshot | null>(null);

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;

    getSupervisorSnapshot()
      .then((snap) => {
        if (!disposed) setSnapshot(snap);
      })
      .catch(() => {
        /* nicht in Tauri (Web-Dev) — Snapshot bleibt null */
      });

    onSupervisorChange((snap) => {
      if (!disposed) setSnapshot(snap);
    })
      .then((fn) => {
        unlisten = fn;
      })
      .catch(() => {
        /* Event-Kanal nicht verfuegbar (Web-Dev) */
      });

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  const admit = useCallback((requestedGeneration?: number) => admitJob(requestedGeneration), []);
  const requestSwitch = useCallback((target: BackendVariant) => requestBackendSwitch(target), []);
  const installAddon = useCallback(() => installCudaAddon(), []);
  const repairAddon = useCallback(() => repairCudaAddon(), []);
  const removeAddon = useCallback(() => removeCudaAddon(), []);

  return { snapshot, admit, requestSwitch, installAddon, repairAddon, removeAddon };
}
