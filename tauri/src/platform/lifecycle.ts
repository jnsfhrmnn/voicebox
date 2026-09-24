import { invoke } from '@tauri-apps/api/core';
import { emit, listen } from '@tauri-apps/api/event';
import type { PlatformLifecycle, ServerLogEntry } from '@/platform/types';

class TauriLifecycle implements PlatformLifecycle {
  onServerReady?: () => void;

  async startServer(modelsDir?: string | null): Promise<void> {
    try {
      // JFW-1: Das Kommando liefert keinen Port/keine URL — die Webview darf
      // weder Port noch Token sehen. Ready = Resolve des Promises.
      await invoke('start_server', {
        modelsDir: modelsDir ?? undefined,
      });
      console.log('Server started');
      this.onServerReady?.();
    } catch (error) {
      console.error('Failed to start server:', error);
      throw error;
    }
  }

  async stopServer(): Promise<void> {
    try {
      await invoke('stop_server');
      console.log('Server stopped');
    } catch (error) {
      console.error('Failed to stop server:', error);
      throw error;
    }
  }

  async restartServer(modelsDir?: string | null): Promise<void> {
    try {
      await invoke('restart_server', {
        modelsDir: modelsDir ?? undefined,
      });
      console.log('Server restarted');
      this.onServerReady?.();
    } catch (error) {
      console.error('Failed to restart server:', error);
      throw error;
    }
  }

  async setupWindowCloseHandler(): Promise<void> {
    try {
      // Listen for window close request from Rust. JFW-12 B8: Der Sidecar-Prozessbaum
      // wird beim App-Ende ueber das Job Object beendet (KILL_ON_JOB_CLOSE) — die
      // keep_running_on_close-Option existiert nicht mehr. Wir geben den Close nur
      // noch frei; der graceful HTTP-Stopp bleibt als sauberes Verabschieden.
      await listen<null>('window-close-requested', async () => {
        // @ts-ignore - accessing module-level variable from another module
        const serverStartedByApp = window.__voiceboxServerStartedByApp ?? false;

        console.log('[lifecycle] window-close-requested: serverStartedByApp=%s', serverStartedByApp);

        if (serverStartedByApp) {
          // Graceful HTTP-Shutdown vor dem Close (Job-Close ist die Garantie).
          try {
            await this.stopServer();
          } catch (error) {
            console.error('Failed to stop server on close:', error);
          }
        }

        // Emit event back to Rust to allow close
        await emit('window-close-allowed');
      });
    } catch (error) {
      console.error('Failed to setup window close handler:', error);
    }
  }

  subscribeToServerLogs(callback: (entry: ServerLogEntry) => void): () => void {
    let disposed = false;
    let unlisten: (() => void) | null = null;

    void listen<ServerLogEntry>('server-log', (event) => {
      callback(event.payload);
    })
      .then((fn) => {
        if (disposed) {
          fn();
          return;
        }
        unlisten = fn;
      })
      .catch((error) => {
        console.error('Failed to subscribe to server logs:', error);
      });

    return () => {
      disposed = true;
      unlisten?.();
      unlisten = null;
    };
  }
}

export const tauriLifecycle = new TauriLifecycle();
