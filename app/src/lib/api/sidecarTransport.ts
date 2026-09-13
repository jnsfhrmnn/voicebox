/**
 * JFW-1: Sidecar-Transport (Spec "Prozess- und Sicherheitsgrenze").
 *
 * In Produktion (Tauri) spricht die Webview den Python-Sidecar nie direkt an
 * und kennt weder Port noch Token. Alle Aufrufe laufen ueber typisierte
 * Tauri-Kommandos, die im Rust-State die SidecarSession lesen, das
 * Caller-Fensterlabel gegen eine fachliche Allowlist pruefen und Auth +
 * Generation injizieren (tauri/src-tauri/src/sidecar.rs).
 *
 * Im Web-Dev-Modus (kein Tauri) bleibt der direkte Loopback-Zugriff — dort
 * gibt es keinen Sidecar-Prozess, den man schuetzen muesste.
 */

import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { useServerStore } from '@/stores/serverStore';

export function isTauriRuntime(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

function b64ToBytes(b64: string): Uint8Array<ArrayBuffer> {
  const bin = atob(b64);
  const bytes = new Uint8Array(new ArrayBuffer(bin.length));
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

export interface SidecarTransport {
  request<T>(method: string, path: string, body?: unknown): Promise<T>;
  upload<T>(path: string, file: File, fields?: Record<string, string>): Promise<T>;
  fetchBytes(path: string): Promise<{ blob: Blob; contentType: string }>;
  /**
   * SSE-Stream abonnieren; liefert die Generation und eine Unlisten-Funktion.
   * ``onEnd`` feuert, wenn der Rust-Stream endet (Sidecarwechsel oder Riss).
   */
  stream(
    path: string,
    onData: (data: string) => void,
    onEnd?: () => void,
  ): Promise<{ generation: number; close: () => void }>;
}

const tauriTransport: SidecarTransport = {
  async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    return invoke<T>('sidecar_request', { method, path, body: body ?? null });
  },

  async upload<T>(path: string, file: File, fields?: Record<string, string>): Promise<T> {
    const dataBase64 = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',')[1]);
      reader.onerror = () => reject(new Error('Datei konnte nicht gelesen werden'));
      reader.readAsDataURL(file);
    });
    return invoke<T>('sidecar_upload', {
      path,
      filename: file.name,
      contentType: file.type || 'application/octet-stream',
      dataBase64,
      fields: fields ?? null,
    });
  },

  async fetchBytes(path: string): Promise<{ blob: Blob; contentType: string }> {
    const payload = await invoke<{ base64: string; content_type: string }>(
      'sidecar_fetch_bytes',
      { path },
    );
    return {
      blob: new Blob([b64ToBytes(payload.base64)], { type: payload.content_type }),
      contentType: payload.content_type,
    };
  },

  async stream(
    path: string,
    onData: (data: string) => void,
    onEnd?: () => void,
  ): Promise<{ generation: number; close: () => void }> {
    const generation = await invoke<number>('sidecar_stream', { path });
    const event = `sidecar:sse:${generation}`;
    const endEvent = `sidecar:sse-end:${generation}`;
    let unlistenPromise: Promise<UnlistenFn> | null = null;
    let unlistenEndPromise: Promise<UnlistenFn> | null = null;
    listen<string>(event, (e) => onData(e.payload)).then((fn) => {
      unlistenPromise = Promise.resolve(fn);
    });
    if (onEnd) {
      listen(endEvent, () => onEnd()).then((fn) => {
        unlistenEndPromise = Promise.resolve(fn);
      });
    }
    const close = () => {
      // Generation-gebundene Events: der Rust-Stream endet mit dem Sidecar.
      void unlistenPromise?.then((fn) => fn()).catch(() => {});
      void unlistenEndPromise?.then((fn) => fn()).catch(() => {});
    };
    return { generation, close };
  },
};

/** Direkter Loopback-Zugriff — nur im Web-Dev-Modus ohne Tauri. */
const webTransport: SidecarTransport = {
  async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const base = useServerStore.getState().serverUrl;

    const response = await fetch(`${base}${path}`, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json() as Promise<T>;
  },

  async upload<T>(path: string, file: File, fields?: Record<string, string>): Promise<T> {
    const formData = new FormData();
    formData.append('file', file);
    for (const [k, v] of Object.entries(fields ?? {})) formData.append(k, v);
    const base = useServerStore.getState().serverUrl;

    const response = await fetch(`${base}${path}`, { method: 'POST', body: formData });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json() as Promise<T>;
  },

  async fetchBytes(path: string): Promise<{ blob: Blob; contentType: string }> {
    const base = useServerStore.getState().serverUrl;

    const response = await fetch(`${base}${path}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return {
      blob: await response.blob(),
      contentType: response.headers.get('content-type') ?? 'application/octet-stream',
    };
  },

  async stream(
    path: string,
    onData: (data: string) => void,
    onEnd?: () => void,
  ): Promise<{ generation: number; close: () => void }> {
    const base = useServerStore.getState().serverUrl;

    const source = new EventSource(`${base}${path}`);
    source.onmessage = (msg) => onData(msg.data);
    source.onerror = () => onEnd?.();
    return {
      generation: 0,
      close: () => source.close(),
    };
  },
};

// Produktions-Tauri: strikt ueber Rust-Kommandos (Auth/Generation).
// Dev-Modus (Web ODER `tauri dev` mit Platzhalter-Binary): direkter Loopback-Zugriff
// auf den manuell gestarteten Server — dort existiert kein Token, und die Webview
// darf in Produktion ohnehin nie einen Port sehen.
const useTauriCommands = isTauriRuntime() && import.meta.env.PROD;

export const sidecarTransport: SidecarTransport = useTauriCommands ? tauriTransport : webTransport;
