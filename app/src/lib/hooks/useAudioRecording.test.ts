/**
 * USCRX-2026-16039 — Fehlerdurchleitung Medienzugriff (Regressionstests).
 */
import { describe, expect, test } from 'bun:test';
import { describeMediaError } from './useAudioRecording';

describe('describeMediaError', () => {
  test('NotFoundError wird handlungsleitend deutsch (Gerät fehlt)', () => {
    const err = new Error('Requested device not found');
    err.name = 'NotFoundError';
    const out = describeMediaError(err);
    expect(out).toContain('Kein Mikrofon gefunden');
    expect(out.toLowerCase()).not.toContain('requested device');
  });

  test('NotAllowedError zeigt Berechtigungsweg', () => {
    const err = new Error('Permission denied');
    err.name = 'NotAllowedError';
    expect(describeMediaError(err)).toContain('nicht erlaubt');
  });

  test('NotReadableError zeigt Belegungs-Hinweis', () => {
    const err = new Error('Device busy');
    err.name = 'NotReadableError';
    expect(describeMediaError(err)).toContain('belegt');
  });

  test('unbekannte Fehler behalten ihren Originaltext', () => {
    expect(describeMediaError(new Error('boom'))).toBe('boom');
    expect(describeMediaError(undefined).length).toBeGreaterThan(0);
  });
});
