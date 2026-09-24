/**
 * JFW-12 P3+: Persistenz des globalen CPU/GPU-Switch-Hotkeys.
 *
 * Die Belegung ist frei in der UI änderbar (ChordPicker auf der GPU-Seite) und
 * wird pro Gerät im Webview-LocalStorage gehalten — nichts ist hardcoded,
 * Strg+Alt+B ist nur der Standardwert. Der Key-Vokabular entspricht dem des
 * bestehenden ChordPickers (keytap-Namen), damit Picker + Anzeige wiederverwendet
 * werden können.
 *
 * USCRX-2026-16037: zusätzlich der Registrierungsstatus (Spec-JFW-6-Vertrag
 * „keine stille Ersatzbelegung“ — auch für den Switch-Hotkey): Kollision oder
 * Nicht-Registrierbarkeit werden sichtbar statt nur in die Konsole geloggt.
 * Der Status ist Laufzeit und wird nicht persistiert.
 */

import { create } from 'zustand';
import { persist } from 'zustand/middleware';

/** Standard-Belegung: Strg+Alt+B (B = Backend). */
export const DEFAULT_BACKEND_SWITCH_CHORD = ['ControlLeft', 'Alt', 'KeyB'];

/** Registrierungsstand des globalen Hotkeys. `failed` trägt die Fehlerursache. */
export type HotkeyRegistrationStatus =
  | { state: 'idle' }
  | { state: 'pending'; accelerator: string }
  | { state: 'registered'; accelerator: string }
  | { state: 'failed'; accelerator: string | null; error: string };

interface BackendHotkeyState {
  /** Kanonische Key-Namen (keytap-Vokabular), z. B. ["ControlLeft","Alt","KeyB"]. */
  chord: string[];
  setChord: (chord: string[]) => void;
  registration: HotkeyRegistrationStatus;
  setRegistration: (registration: HotkeyRegistrationStatus) => void;
}

export const useBackendSwitchHotkeyStore = create<BackendHotkeyState>()(
  persist(
    (set) => ({
      chord: DEFAULT_BACKEND_SWITCH_CHORD,
      setChord: (chord) => set({ chord }),
      registration: { state: 'idle' },
      setRegistration: (registration) => set({ registration }),
    }),
    {
      name: 'voicebox-backend-switch-hotkey',
      // Nur die Belegung ist persistent — der Registrierungsstatus ist Laufzeit.
      partialize: (s) => ({ chord: s.chord }),
    },
  ),
);
