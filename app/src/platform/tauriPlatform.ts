import type { Platform } from './types';
// JFW-Boot-Reparatur: Die Tauri-Implementierung der Platform-Schnittstelle
// existiert intakt unter application/tauri/src/platform/ (Vertrag aus den
// Upstream-Commits a6b0702/9044b98). Sie wird hier nur referenziert, damit
// der App-Einstieg (app/src/main.tsx) denselben Adapter benutzt und es genau
// EINE Implementierung gibt.
import { tauriPlatform } from '../../../tauri/src/platform';

export function createTauriPlatform(): Platform {
  return tauriPlatform;
}
