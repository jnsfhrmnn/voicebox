/**
 * USCRX-2026-16037 — Regressionstests Switch-Hotkey (Registrierung, Kollision,
 * Loslassen). Läuft mit `bun test`.
 */
import { describe, expect, test } from 'bun:test';
import {
  chordToAccelerator,
  describeRegistrationError,
  shouldTriggerShortcut,
} from './useBackendSwitchHotkey';
import { DEFAULT_BACKEND_SWITCH_CHORD, useBackendSwitchHotkeyStore } from './useBackendSwitchHotkeyStore';

describe('Registrierung: chordToAccelerator', () => {
  test('Standard-Belegung ergibt Strg+Alt+B', () => {
    expect(chordToAccelerator(DEFAULT_BACKEND_SWITCH_CHORD)).toBe('Ctrl+Alt+B');
  });

  test('Haupttaste zuletzt, Modifikatoren in Belegungsreihenfolge', () => {
    expect(chordToAccelerator(['KeyB', 'ControlLeft', 'Alt'])).toBe('Ctrl+Alt+B');
  });

  test('nackte Taste ohne Modifikator ist verboten (Systemtasten-Schutz)', () => {
    expect(chordToAccelerator(['KeyB'])).toBeNull();
  });

  test('ohne Haupttaste abgelehnt', () => {
    expect(chordToAccelerator(['ControlLeft', 'Alt'])).toBeNull();
  });

  test('zwei Haupttasten abgelehnt', () => {
    expect(chordToAccelerator(['ControlLeft', 'KeyB', 'KeyC'])).toBeNull();
  });

  test('Funktionstasten und Space sind zulässige Haupttasten', () => {
    expect(chordToAccelerator(['ControlLeft', 'F12'])).toBe('Ctrl+F12');
    expect(chordToAccelerator(['ShiftLeft', 'Space'])).toBe('Shift+Space');
  });

  test('unbekannte Tasten führen zur Ablehnung statt stiller Ersatzbelegung', () => {
    expect(chordToAccelerator(['ControlLeft', 'MediaPlayPause'])).toBeNull();
  });
});

describe('Loslassen: shouldTriggerShortcut', () => {
  test('Pressed löst aus', () => {
    expect(shouldTriggerShortcut('Pressed')).toBe(true);
  });

  test('Released löst NICHT aus', () => {
    expect(shouldTriggerShortcut('Released')).toBe(false);
  });

  test('unbekannte Zustände lösen nicht aus (fail-closed)', () => {
    expect(shouldTriggerShortcut('LongPress')).toBe(false);
  });
});

describe('Kollision: sichtbarer Registrierungsfehler', () => {
  test('describeRegistrationError benennt Error-Nachrichten', () => {
    expect(describeRegistrationError(new Error('already registered'))).toBe('already registered');
  });

  test('describeRegistrationError überlebt Nicht-Error-Werte', () => {
    expect(describeRegistrationError('boom')).toBe('boom');
  });

  test('describeRegistrationError fällt nie auf leer zurück', () => {
    expect(describeRegistrationError(undefined).length).toBeGreaterThan(0);
  });

  test('failed-Status trägt Fehlerursache in den Store (kein stilles Loggen)', () => {
    const store = useBackendSwitchHotkeyStore.getState();
    store.setRegistration({ state: 'failed', accelerator: 'Ctrl+Alt+B', error: 'Kollision mit anderer App' });
    const reg = useBackendSwitchHotkeyStore.getState().registration;
    expect(reg.state).toBe('failed');
    if (reg.state === 'failed') {
      expect(reg.error).toBe('Kollision mit anderer App');
      expect(reg.accelerator).toBe('Ctrl+Alt+B');
    }
  });
});
