/**
 * JFW-12 P3+: Persistenz des globalen CPU/GPU-Switch-Hotkeys.
 *
 * Die Belegung ist frei in der UI änderbar (ChordPicker auf der GPU-Seite) und
 * wird pro Gerät im Webview-LocalStorage gehalten — nichts ist hardcoded,
 * Strg+Alt+B ist nur der Standardwert. Der Key-Vokabular entspricht dem des
 * bestehenden ChordPickers (keytap-Namen), damit Picker + Anzeige wiederverwendet
 * werden können.
 */

import { create } from 'zustand';
import { persist } from 'zustand/middleware';

/** Standard-Belegung: Strg+Alt+B (B = Backend). */
export const DEFAULT_BACKEND_SWITCH_CHORD = ['ControlLeft', 'Alt', 'KeyB'];

interface BackendHotkeyState {
  /** Kanonische Key-Namen (keytap-Vokabular), z. B. ["ControlLeft","Alt","KeyB"]. */
  chord: string[];
  setChord: (chord: string[]) => void;
}

export const useBackendSwitchHotkeyStore = create<BackendHotkeyState>()(
  persist(
    (set) => ({
      chord: DEFAULT_BACKEND_SWITCH_CHORD,
      setChord: (chord) => set({ chord }),
    }),
    { name: 'voicebox-backend-switch-hotkey' },
  ),
);
