import { CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { useCallback, useEffect, useRef } from 'react';
import { Progress } from '@/components/ui/progress';
import { useToast } from '@/components/ui/use-toast';
import type { ModelProgress } from '@/lib/api/types';
import { sidecarTransport } from '@/lib/api/sidecarTransport';

interface UseModelDownloadToastOptions {
  modelName: string;
  displayName: string;
  enabled?: boolean;
  onComplete?: () => void;
  onError?: (error: string) => void;
}

/**
 * Zeigt ein Toast mit Modell-Download-Fortschritt.
 *
 * JFW-1: Der Fortschritt kommt ueber den Sidecar-Transport (Auth + Generation
 * injiziert), nie ueber einen rohen EventSource gegen eine Server-URL — die
 * Webview kennt weder Port noch Token.
 */
export function useModelDownloadToast({
  modelName,
  displayName,
  enabled = false,
  onComplete,
  onError,
}: UseModelDownloadToastOptions) {
  const { toast } = useToast();
  const toastIdRef = useRef<string | null>(null);
  // biome-ignore lint: Using any for toast update ref to handle complex toast types
  const toastUpdateRef = useRef<any>(null);
  const closeStreamRef = useRef<(() => void) | null>(null);

  const formatBytes = useCallback((bytes: number): string => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${(bytes / k ** i).toFixed(1)} ${sizes[i]}`;
  }, []);

  useEffect(() => {
    if (!enabled || !modelName) {
      return;
    }

    // Create initial toast
    const toastResult = toast({
      title: displayName,
      description: (
        <div className="flex items-center gap-2">
          <Loader2 className="h-4 w-4 animate-spin" />
          <span>Connecting to download...</span>
        </div>
      ),
      duration: Infinity, // Don't auto-dismiss, we'll handle it manually
    });
    toastIdRef.current = toastResult.id;
    toastUpdateRef.current = toastResult.update;

    let finished = false;
    const finish = (kind: 'complete' | 'error', message?: string) => {
      if (finished) return;
      finished = true;
      closeStreamRef.current?.();
      closeStreamRef.current = null;
      if (kind === 'complete') onComplete?.();
      else onError?.(message ?? 'Unknown error');
    };

    const path = `/models/progress/${encodeURIComponent(modelName)}`;

    sidecarTransport
      .stream(
        path,
        (data: string) => {
          try {
            const progress = JSON.parse(data) as ModelProgress;

            if (toastIdRef.current && toastUpdateRef.current) {
              const progressPercent = progress.total > 0 ? progress.progress : 0;
              const progressText =
                progress.total > 0
                  ? `${formatBytes(progress.current)} / ${formatBytes(progress.total)} (${progress.progress.toFixed(1)}%)`
                  : '';

              let statusIcon: React.ReactNode = null;
              let statusText = 'Processing...';

              switch (progress.status) {
                case 'complete':
                  statusIcon = <CheckCircle2 className="h-4 w-4 text-green-500" />;
                  statusText = 'Download complete';
                  break;
                case 'error':
                  statusIcon = <XCircle className="h-4 w-4 text-destructive" />;
                  statusText = 'Download failed. See Problems panel for details.';
                  break;
                case 'downloading':
                  statusIcon = <Loader2 className="h-4 w-4 animate-spin" />;
                  statusText = progress.filename || 'Downloading...';
                  break;
                case 'extracting':
                  statusIcon = <Loader2 className="h-4 w-4 animate-spin" />;
                  statusText = 'Extracting...';
                  break;
              }

              toastUpdateRef.current({
                title: (
                  <div className="flex items-center gap-2">
                    {statusIcon}
                    <span>{displayName}</span>
                  </div>
                ),
                description: (
                  <div className="space-y-2">
                    <div className="text-sm">{statusText}</div>
                    {progress.total > 0 && (
                      <>
                        <Progress value={progressPercent} className="h-2" />
                        <div className="text-xs text-muted-foreground">{progressText}</div>
                      </>
                    )}
                  </div>
                ),
                duration:
                  progress.status === 'complete' || progress.status === 'error' ? 5000 : Infinity,
              });

              const isComplete = progress.status === 'complete' || progress.progress >= 100;
              const isError = progress.status === 'error';
              if (isComplete) {
                finish('complete');
              } else if (isError) {
                finish('error', progress.error);
              }
            }
          } catch {
            /* ignore parse errors */
          }
        },
        () => {
          // Stream-Abriss (Sidecarwechsel/Verbindung) — fail-closed.
          if (!finished && toastIdRef.current && toastUpdateRef.current) {
            toastUpdateRef.current({
              title: displayName,
              description: 'Failed to track download progress',
              variant: 'destructive',
              duration: 5000,
            });
            toastIdRef.current = null;
            toastUpdateRef.current = null;
          }
          finish('error', 'Connection lost');
        },
      )
      .then((s) => {
        closeStreamRef.current = s.close;
      })
      .catch(() => {
        if (!finished && toastIdRef.current && toastUpdateRef.current) {
          toastUpdateRef.current({
            title: displayName,
            description: 'Failed to track download progress',
            variant: 'destructive',
            duration: 5000,
          });
          toastIdRef.current = null;
          toastUpdateRef.current = null;
        }
        finish('error', 'Could not open progress stream');
      });

    return () => {
      closeStreamRef.current?.();
      closeStreamRef.current = null;
    };
  }, [enabled, modelName, displayName, toast, formatBytes, onComplete, onError]);

  return {
    isTracking: enabled && closeStreamRef.current !== null,
  };
}
