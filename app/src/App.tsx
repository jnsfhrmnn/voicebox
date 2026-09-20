import { RouterProvider } from '@tanstack/react-router';
import { useEffect, useRef, useState } from 'react';
import voiceboxLogo from '@/assets/voicebox-logo.png';
import { DictateWindow } from '@/components/DictateWindow/DictateWindow';
import ShinyText from '@/components/ShinyText';
import { TitleBarDragRegion } from '@/components/TitleBarDragRegion';
import { useAutoUpdater } from '@/hooks/useAutoUpdater';
import { useThemeSync } from '@/hooks/useThemeSync';
import { useChordSync } from '@/lib/hooks/useChordSync';
import { TOP_SAFE_AREA_PADDING } from '@/lib/constants/ui';
import { cn } from '@/lib/utils/cn';
import { usePlatform } from '@/platform/PlatformContext';
import { router } from '@/router';
import { useBackendModeMarker } from '@/features/backend/useBackendModeMarker';
import { useBackendSwitchHotkey } from '@/features/backend/useBackendSwitchHotkey';
import { useLogStore } from '@/stores/logStore';
import {
  getDefaultServerUrl,
  isLoopbackVoiceboxServerUrl,
  useServerStore,
} from '@/stores/serverStore';

function isDictateView(): boolean {
  if (typeof window === 'undefined') return false;
  return new URLSearchParams(window.location.search).get('view') === 'dictate';
}

const LOADING_MESSAGES = [
  'Starting transcription sidecar...',
  'Loading Whisper model...',
  'Warming up tensors...',
  'Preparing audio pipelines...',
  'Syncing audio buffers...',
  'Establishing model connections...',
  'Validating capture storage...',
];

function App() {
  useThemeSync();

  // The dictate window runs in a separate Tauri webview that must skip
  // server bootstrap (the main window owns that lifecycle) and render only
  // the floating recording surface. Split into a sibling component so the
  // main app's hooks are not called on the dictate path.
  if (isDictateView()) {
    return <DictateWindow />;
  }
  return <MainApp />;
}

function MainApp() {
  const platform = usePlatform();
  const [serverReady, setServerReady] = useState(false);
  const [startupError, setStartupError] = useState<string | null>(null);
  const [loadingMessageIndex, setLoadingMessageIndex] = useState(0);
  const serverStartingRef = useRef(false);

  // Automatically check for app updates on startup and show toast notifications
  useAutoUpdater({ checkOnMount: true, showToast: true });

  // JFW-12 P3+: Backend-Farbsignal (CUDA = rot) + globaler Switch-Hotkey.
  // Der Marker gilt für beide Webviews; der Hotkey wird nur im Hauptfenster
  // registriert (einmalig).
  useBackendModeMarker();
  useBackendSwitchHotkey();

  // Replay the saved chord into the Rust hotkey listener every time
  // capture_settings resolves or the user edits the chord.
  useChordSync();

  // Setup lifecycle callbacks
  useEffect(() => {
    platform.lifecycle.onServerReady = () => {
      setServerReady(true);
    };
    // Empty dependency array - platform is stable from context, only run once
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [platform.lifecycle]);

  // Subscribe to server logs
  useEffect(() => {
    const unsubscribe = platform.lifecycle.subscribeToServerLogs((entry) => {
      useLogStore.getState().addEntry(entry);
    });
    return unsubscribe;
  }, [platform.lifecycle]);

  // Setup window close handler and auto-start server when running in Tauri (production only)
  useEffect(() => {
    if (!platform.metadata.isTauri) {
      const serverUrl = getDefaultServerUrl();
      const currentServerUrl = useServerStore.getState().serverUrl;
      if (currentServerUrl !== serverUrl && isLoopbackVoiceboxServerUrl(currentServerUrl)) {
        useServerStore.getState().setServerUrl(serverUrl);
      }
      setServerReady(true); // Web assumes server is running
      return;
    }

    // Setup window close handler to check setting and stop server if needed
    // This works in both dev and prod, but will only stop server if it was started by the app
    platform.lifecycle.setupWindowCloseHandler().catch((error) => {
      console.error('Failed to setup window close handler:', error);
    });

    // Only auto-start server in production mode
    // In dev mode, user runs server separately
    if (!import.meta.env?.PROD) {
      console.log('Dev mode: Skipping auto-start of server (run it separately)');
      setServerReady(true); // Mark as ready so UI doesn't show loading screen
      // Mark that server was not started by app (so we don't try to stop it on close)
      window.__voiceboxServerStartedByApp = false;
      return;
    }

    // Auto-start server in production
    if (serverStartingRef.current) {
      return;
    }

    serverStartingRef.current = true;
    const customModelsDir = useServerStore.getState().customModelsDir;
    console.log('Production mode: Starting bundled server...');

    // JFW-1: startServer liefert keinen Port/keine URL — die Webview kennt
    // weder Port noch Token. Ready ist das Resolve des Promises selbst.
    platform.lifecycle
      .startServer(customModelsDir)
      .then(() => {
        setServerReady(true);
        // Mark that we started the server (so we know to stop it on close)
        window.__voiceboxServerStartedByApp = true;
      })
      .catch((error) => {
        console.error('Failed to auto-start server:', error);
        serverStartingRef.current = false;
        window.__voiceboxServerStartedByApp = false;

        // JFW-1 (fail-closed): Ein fremder Prozess wird nicht anhand eines
        // Health-Payloads wiederverwendet — jeder Startfehler wird direkt
        // angezeigt, statt einen externen Sidecar zu adoptieren.
        const msg = error instanceof Error ? error.message : String(error);
        setStartupError(msg);
      });

    // Cleanup: stop server on actual unmount (not StrictMode remount)
    // Note: Window close is handled separately in Tauri Rust code
    return () => {
      // Window close event handles server shutdown based on setting
      serverStartingRef.current = false;
    };
    // Empty dependency array - platform is stable from context, only run once
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [platform.metadata.isTauri, platform.lifecycle]);

  // Cycle through loading messages every 3 seconds
  useEffect(() => {
    if (!platform.metadata.isTauri || serverReady) {
      return;
    }

    const interval = setInterval(() => {
      setLoadingMessageIndex((prev) => (prev + 1) % LOADING_MESSAGES.length);
    }, 3000);

    return () => clearInterval(interval);
  }, [serverReady, platform.metadata.isTauri]);

  // Show loading screen while server is starting in Tauri
  if (platform.metadata.isTauri && !serverReady) {
    return (
      <div
        className={cn(
          'min-h-screen bg-background flex items-center justify-center',
          TOP_SAFE_AREA_PADDING,
        )}
      >
        <TitleBarDragRegion />
        <div className="text-center space-y-6">
          <div className="flex justify-center relative">
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="w-48 h-48 rounded-full bg-accent/20 blur-3xl" />
            </div>
            <img
              src={voiceboxLogo}
              alt="Voicebox"
              className="w-48 h-48 object-contain animate-fade-in-scale relative z-10"
            />
          </div>
          {startupError ? (
            <div className="animate-fade-in-delayed max-w-md mx-auto space-y-3">
              <p className="text-lg font-medium text-destructive">Server startup failed</p>
              <p className="text-sm text-muted-foreground">{startupError}</p>
              <button
                type="button"
                className="mt-2 px-4 py-2 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
                onClick={() => {
                  setStartupError(null);
                  serverStartingRef.current = false;
                  // Trigger a re-mount of the effect by toggling state
                  window.location.reload();
                }}
              >
                Retry
              </button>
            </div>
          ) : (
            <div className="animate-fade-in-delayed">
              <ShinyText
                text={LOADING_MESSAGES[loadingMessageIndex]}
                className="text-lg font-medium text-muted-foreground"
                speed={2}
                color="hsl(var(--muted-foreground))"
                shineColor="hsl(var(--foreground))"
              />
            </div>
          )}
        </div>
      </div>
    );
  }

  return <RouterProvider router={router} />;
}

export default App;
