/**
 * JFW-12 P3+: Backend-Farbsignal.
 *
 * Setzt `data-backend="cuda"` am Dokument-Root, sobald der Supervisor CUDA als
 * aktive Variante meldet (sonst: Attribut entfernt = CPU). Die CSS-Regeln in
 * index.css hängen die Akzentfarbe an diesen Marker — dadurch wechselt das
 * gesamte Interface (Pill, Buttons, Highlights) von gelb-orange auf Rot, wenn
 * der GPU-Modus aktiv ist.
 *
 * Wird im App-Root aufgerufen und gilt damit für BEIDE Webviews: Hauptfenster
 * UND Diktat-Overlay (die Pill). Der Snapshot-Lauf ist ein reiner Read gegen
 * den Supervisor-State — kein Sidecar-Zugriff, keine neuen Berechtigungen.
 */

import { useEffect } from 'react';
import type { BackendVariant } from './supervisor';
import { getSupervisorSnapshot, onSupervisorChange } from './supervisor';

function applyMarker(variant: BackendVariant | null) {
  const root = document.documentElement;
  if (variant === 'cuda') {
    root.setAttribute('data-backend', 'cuda');
  } else {
    root.removeAttribute('data-backend');
  }
}

export function useBackendModeMarker() {
  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;

    getSupervisorSnapshot()
      .then((snap) => {
        if (!disposed) applyMarker(snap.active_variant);
      })
      .catch(() => {
        /* nicht in Tauri (Web-Dev) — Marker bleibt entfernt */
      });

    onSupervisorChange((snap) => {
      if (!disposed) applyMarker(snap.active_variant);
    })
      .then((fn) => {
        unlisten = fn;
      })
      .catch(() => {
        /* Event-Kanal nicht verfügbar (Web-Dev) */
      });

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);
}
