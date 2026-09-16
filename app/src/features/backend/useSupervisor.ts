/**
 * JFW-12 B2/D: React-Hook fuer den BackendSupervisor.
 *
 * Liest den initialen Snapshot ueber `supervisor_snapshot` und abonniert danach
 * typisierte Aenderungen ueber das Event `backend:supervisor`. Der Webview
 * spricht den Sidecar nie direkt an (D: Surface).
 */

import { useCallback, useEffect, useState } from 'react';
import type { AdmissionOutcome, BackendVariant, SupervisorSnapshot } from './supervisor';
import { admitJob, getSupervisorSnapshot, onSupervisorChange, requestBackendSwitch } from './supervisor';

export interface UseSupervisorResult {
  snapshot: SupervisorSnapshot | null;
  /** Admission-Entscheidung fuer einen neuen Job (B5 Schritt 1). */
  admit: (requestedGeneration?: number) => Promise<AdmissionOutcome>;
  /** Nutzerinitiiertes Backend-Switch-Request (B9); CUDA fail-closed bis Block (d). */
  requestSwitch: (target: BackendVariant) => Promise<void>;
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

  return { snapshot, admit, requestSwitch };
}
