/**
 * JFW-12 P3+: Globaler CPU/GPU-Switch-Hotkey.
 *
 * Registriert die frei belegbare Tastenkombination aus dem Store als OS-weiten
 * Shortcut (Windows: RegisterHotKey, kein Admin nötig) über das Tauri
 * global-shortcut-Plugin. Beim Drücken wird zum ANDEREN Backend gewechselt —
 * die Richtung leitet sich aus dem aktuellen Supervisor-Snapshot ab. Die
 * bestehende fail-closed-Logik bleibt wirksam (CUDA nicht installiert → Wechsel
 * wird abgelehnt, nichts kaputtes passiert).
 *
 * Wird NUR im Hauptfenster aufgerufen (einmalig) — die Diktat-Webview registriert
 * keinen eigenen Shortcut.
 */

import { useEffect, useRef } from 'react';
import { register, unregisterAll, type ShortcutEvent } from '@tauri-apps/plugin-global-shortcut';
import type { BackendVariant } from './supervisor';
import { getSupervisorSnapshot, onSupervisorChange, requestBackendSwitch } from './supervisor';
import { useBackendSwitchHotkeyStore } from './useBackendSwitchHotkeyStore';

/**
 * USCRX-2026-16037: Auslöse-Regel des Shortcut-Handlers — nur `Pressed` schaltet,
 * das Loslassen (`Released`) hat keine Wirkung (Regressionstest-Aspekt „Loslassen“).
 */
export function shouldTriggerShortcut(state: string): boolean {
  return state === 'Pressed';
}

/**
 * USCRX-2026-16037: Fehlerursache für die sichtbare Registrierungsmeldung
 * (Regressionstest-Aspekt „Kollision“ — der Fehler wird benannt statt geschluckt).
 */
export function describeRegistrationError(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  const text = String(err ?? '');
  return text || 'unbekannter Registrierungsfehler';
}

/** keytap-Keyname → global-hotcut-Accelerator-Token (Modifikatoren + Haupttaste). */
function acceleratorToken(key: string): string | null {
  switch (key) {
    case 'ControlLeft':
    case 'ControlRight':
      return 'Ctrl';
    case 'Alt':
    case 'AltGr':
      return 'Alt';
    case 'ShiftLeft':
    case 'ShiftRight':
      return 'Shift';
    case 'MetaLeft':
    case 'MetaRight':
      return 'Super';
    default:
      break;
  }
  if (/^Key[A-Z]$/.test(key)) return key.slice(3);
  if (/^Digit[0-9]$/.test(key)) return key.slice(5);
  if (/^F([1-9]|1[0-2])$/.test(key)) return key;
  if (key === 'Space') return 'Space';
  return null;
}

/** Kanonische Key-Namen → Accelerator-String ("Ctrl+Alt+B"). Haupttaste zuletzt.
 *  Liefert `null`, wenn die Belegung unbrauchbar ist (keine/zu viele Haupttasten,
 *  oder KEIN Modifikator — eine nackte Taste als globalen Shortcut zu registrieren
 *  würde System-/App-Tasten stehlen und ist daher verboten). */
export function chordToAccelerator(keys: string[]): string | null {
  const mods = keys.filter((k) => ['ControlLeft', 'ControlRight', 'Alt', 'AltGr', 'ShiftLeft', 'ShiftRight', 'MetaLeft', 'MetaRight'].includes(k));
  const mains = keys.filter((k) => !mods.includes(k));
  if (mains.length !== 1 || mods.length < 1) return null; // genau eine Haupttaste + ≥1 Modifikator
  const modTokens = mods.map(acceleratorToken).filter(Boolean);
  const mainToken = acceleratorToken(mains[0]);
  if (!mainToken) return null;
  return [...modTokens, mainToken].join('+');
}

export function useBackendSwitchHotkey() {
  const chord = useBackendSwitchHotkeyStore((s) => s.chord);
  // Aktueller Modus in einem Ref — der Shortcut-Handler liest den frischen Wert,
  // ohne dass die Registrierung bei jedem Snapshot neu laufen muss.
  const activeRef = useRef<BackendVariant | null>(null);

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;

    getSupervisorSnapshot()
      .then((snap) => {
        if (!disposed) activeRef.current = snap.active_variant;
      })
      .catch(() => {});
    onSupervisorChange((snap) => {
      if (!disposed) activeRef.current = snap.active_variant;
    })
      .then((fn) => {
        unlisten = fn;
      })
      .catch(() => {});

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    const setRegistration = useBackendSwitchHotkeyStore.getState().setRegistration;
    const accelerator = chordToAccelerator(chord);
    if (!accelerator) {
      console.warn('[backend-hotkey] ungültige Belegung, nicht registriert:', chord);
      setRegistration({
        state: 'failed',
        accelerator: null,
        error: 'Belegung ungültig — genau eine Haupttaste plus mindestens ein Modifikator.',
      });
      return;
    }

    let cancelled = false;
    setRegistration({ state: 'pending', accelerator });
    register(accelerator, (event: ShortcutEvent) => {
      if (cancelled || !shouldTriggerShortcut(event.state)) return;
      const current = activeRef.current;
      // Auf CUDA → CPU (immer erlaubt). Auf CPU/keine Info → CUDA (fail-closed
      // im Supervisor, falls Addon fehlt).
      const target: BackendVariant = current === 'cuda' ? 'cpu' : 'cuda';
      requestBackendSwitch(target).catch((err: unknown) => {
        console.warn('[backend-hotkey] Switch abgelehnt:', err);
      });
    })
      .then(() => {
        if (cancelled) {
          void unregisterAll();
          return;
        }
        setRegistration({ state: 'registered', accelerator });
      })
      .catch((err: unknown) => {
        // Belegung kollidiert mit einer anderen App oder ist nicht registrierbar —
        // Spec-JFW-6: das wird SICHTBAR gemeldet, nicht nur geloggt (USCRX-2026-16037).
        const error = describeRegistrationError(err);
        console.warn('[backend-hotkey] Registrierung fehlgeschlagen:', err);
        if (!cancelled) setRegistration({ state: 'failed', accelerator, error });
      });

    return () => {
      cancelled = true;
      void unregisterAll();
    };
  }, [chord]);
}
