import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { save } from '@tauri-apps/plugin-dialog';
import { writeFile, writeTextFile } from '@tauri-apps/plugin-fs';
import {
  Captions,
  ChevronDown,
  CircleDot,
  Copy,
  Download,
  FileAudio,
  FileText,
  Loader2,
  Mic,
  Settings2,
  Square,
  Trash2,
  Upload,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CapturePill } from '@/components/CapturePill/CapturePill';
import { CaptureInlinePlayer } from '@/components/CapturesTab/CaptureInlinePlayer';
import { DictationReadinessChecklist } from '@/components/CapturesTab/DictationReadinessChecklist';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Textarea } from '@/components/ui/textarea';
import {
  ListPane,
  ListPaneHeader,
  ListPaneScroll,
  ListPaneSearch,
  ListPaneTitle,
  ListPaneTitleRow,
} from '@/components/ListPane';
import { useToast } from '@/components/ui/use-toast';
import { apiClient } from '@/lib/api/client';
import type {
  CaptureListResponse,
  CaptureResponse,
  CaptureSource,
} from '@/lib/api/types';
import { cn } from '@/lib/utils/cn';
import { useCaptureRecordingSession } from '@/lib/hooks/useCaptureRecordingSession';
import { useDictationReadiness } from '@/lib/hooks/useDictationReadiness';
import { useCaptureSettings } from '@/lib/hooks/useSettings';
import { formatAbsoluteDate, formatDate } from '@/lib/utils/format';
import { displayLabelForKey, modifierSideHint } from '@/lib/utils/keyCodes';

const CAPTURE_AUDIO_MIME = 'audio/*,.wav,.mp3,.m4a,.flac,.ogg,.webm';

function formatDuration(ms?: number | null): string {
  if (!ms || ms < 0) return '0:00';
  const total = Math.round(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function ChordKeys({ keys }: { keys: string[] }) {
  if (keys.length === 0) return null;
  return (
    <div className="flex items-center gap-1">
      {keys.map((k) => {
        const side = modifierSideHint(k);
        return (
          <span
            key={k}
            className="relative inline-flex items-center justify-center h-6 min-w-[1.5rem] px-1.5 rounded-md border border-border bg-muted/60 font-mono text-[11px] font-medium shadow-sm text-foreground"
          >
            {displayLabelForKey(k)}
            {side ? (
              <span className="absolute -top-1 -right-1 h-3 min-w-[0.75rem] px-0.5 rounded-sm bg-accent text-[7px] font-bold leading-none flex items-center justify-center text-accent-foreground">
                {side}
              </span>
            ) : null}
          </span>
        );
      })}
    </div>
  );
}

function SourceBadge({ source }: { source: CaptureSource }) {
  const { t } = useTranslation();
  const Icon = source === 'dictation' ? Mic : source === 'recording' ? CircleDot : FileAudio;
  const label =
    source === 'dictation'
      ? t('captures.source.dictation')
      : source === 'recording'
        ? t('captures.source.recording')
        : t('captures.source.file');
  return (
    <Badge
      variant="secondary"
      className="h-5 px-1.5 text-[10px] gap-1 font-medium bg-muted/60 text-muted-foreground"
    >
      <Icon className="h-2.5 w-2.5" />
      {label}
    </Badge>
  );
}

export function CapturesTab() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const uploadInputRef = useRef<HTMLInputElement>(null);

  const snippetOf = (capture: CaptureResponse): string => {
    const source = capture.transcript_raw || '';
    return source.trim() || t('captures.snippetEmpty');
  };

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);

  const { settings: captureSettings } = useCaptureSettings();
  const sttModel = captureSettings?.stt_model ?? 'turbo';
  const hotkeyEnabled = captureSettings?.hotkey_enabled ?? false;
  const pushToTalkKeys = captureSettings?.chord_push_to_talk_keys ?? [];
  const toggleToTalkKeys = captureSettings?.chord_toggle_to_talk_keys ?? [];
  const readiness = useDictationReadiness();

  const session = useCaptureRecordingSession({
    onCaptureCreated: (capture) => setSelectedId(capture.id),
  });

  const { data: capturesData, isLoading: capturesLoading } = useQuery({
    queryKey: ['captures'],
    queryFn: () => apiClient.listCaptures(200, 0),
  });

  const captures = capturesData?.items ?? [];

  // Keep a selection. If the current selection disappears (e.g. deletion),
  // fall through to the first capture, then to null.
  useEffect(() => {
    if (!captures.length) {
      if (selectedId !== null) setSelectedId(null);
      return;
    }
    if (!selectedId || !captures.find((c) => c.id === selectedId)) {
      setSelectedId(captures[0].id);
    }
  }, [captures, selectedId]);

  // Live sync from sibling Tauri webviews (the floating dictate window).
  // ``capture:created`` carries the full row so we can seed the cache before
  // the refetch lands and focus the new capture in one shot — without the
  // seed, the selection-guard effect would snap back to ``captures[0]`` in
  // the race window between ``setSelectedId(new)`` and the refetched list
  // actually containing the new row.
  useEffect(() => {
    const unlistens: Promise<UnlistenFn>[] = [];
    unlistens.push(
      listen<{ capture: CaptureResponse }>('capture:created', (event) => {
        const capture = event.payload?.capture;
        if (capture) {
          queryClient.setQueryData<CaptureListResponse>(['captures'], (prev) => {
            if (!prev) return prev;
            if (prev.items.some((c) => c.id === capture.id)) return prev;
            return { ...prev, items: [capture, ...prev.items], total: prev.total + 1 };
          });
          setSelectedId(capture.id);
        }
        queryClient.invalidateQueries({ queryKey: ['captures'] });
      }),
    );
    unlistens.push(
      listen('capture:updated', () => {
        queryClient.invalidateQueries({ queryKey: ['captures'] });
      }),
    );
    return () => {
      for (const p of unlistens) p.then((fn) => fn()).catch(() => {});
    };
  }, [queryClient]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return captures;
    return captures.filter((c) => (c.transcript_raw || '').toLowerCase().includes(q));
  }, [search, captures]);

  const selected = captures.find((c) => c.id === selectedId) ?? null;

  const deleteMutation = useMutation({
    mutationFn: async (captureId: string) => apiClient.deleteCapture(captureId),
    onSuccess: () => {
      setDeleteDialogOpen(false);
      queryClient.invalidateQueries({ queryKey: ['captures'] });
    },
    onError: (err: Error) => {
      toast({ title: t('captures.toast.deleteFailed'), description: err.message, variant: 'destructive' });
    },
  });

  const handleUploadClick = () => uploadInputRef.current?.click();

  const handleUploadFile = (e: React.ChangeEvent<HTMLInputElement>, source: CaptureSource) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    session.uploadFile(file, source);
  };

  const handleCopy = async () => {
    if (!selected) return;
    const text = selected.transcript_raw || '';
    try {
      await navigator.clipboard.writeText(text || '');
      toast({ title: t('captures.toast.transcriptCopied') });
    } catch {
      toast({ title: t('captures.toast.copyFailed'), variant: 'destructive' });
    }
  };

  const exportToastSuccess = (path: string) => {
    const name = path.split(/[\\/]/).pop() ?? path;
    toast({ title: t('captures.toast.exportSuccess', { path: name }) });
  };

  const exportToastError = (err: unknown) => {
    toast({
      title: t('captures.toast.exportFailed'),
      description: err instanceof Error ? err.message : String(err),
      variant: 'destructive',
    });
  };

  const handleExportAudio = async () => {
    if (!selected) return;
    try {
      const dest = await save({
        defaultPath: `capture_${selected.id.slice(0, 8)}.wav`,
        filters: [{ name: 'Audio', extensions: ['wav'] }],
      });
      if (!dest) return;
      // JFW-1: Audio ueber den Sidecar-Transport (Auth/Generation).
      const { blob } = await apiClient.getCaptureAudio(selected.id);
      const buf = new Uint8Array(await blob.arrayBuffer());
      await writeFile(dest, buf);
      exportToastSuccess(dest);
    } catch (err) {
      exportToastError(err);
    }
  };

  const handleExportTranscript = async () => {
    if (!selected) return;
    const text = (selected.transcript_raw || '').trim();
    if (!text) {
      toast({ title: t('captures.toast.exportEmpty'), variant: 'destructive' });
      return;
    }
    try {
      const dest = await save({
        defaultPath: `capture_${selected.id.slice(0, 8)}.txt`,
        filters: [{ name: 'Text', extensions: ['txt'] }],
      });
      if (!dest) return;
      await writeTextFile(dest, text);
      exportToastSuccess(dest);
    } catch (err) {
      exportToastError(err);
    }
  };

  const buildCaptureMarkdown = (capture: CaptureResponse): string => {
    const lines: string[] = [];
    lines.push(`# Capture ${capture.id}`, '');
    lines.push(`- **Source:** ${capture.source}`);
    lines.push(`- **Created:** ${capture.created_at}`);
    if (capture.duration_ms != null) lines.push(`- **Duration:** ${formatDuration(capture.duration_ms)}`);
    if (capture.language) lines.push(`- **Language:** ${capture.language}`);
    if (capture.stt_model) lines.push(`- **STT model:** ${capture.stt_model}`);
    lines.push('');
    if (capture.transcript_raw?.trim()) {
      lines.push('## Transcript', '', capture.transcript_raw.trim(), '');
    }
    return lines.join('\n');
  };

  const handleExportMarkdown = async () => {
    if (!selected) return;
    const hasContent = (selected.transcript_raw || '').trim();
    if (!hasContent) {
      toast({ title: t('captures.toast.exportEmpty'), variant: 'destructive' });
      return;
    }
    try {
      const dest = await save({
        defaultPath: `capture_${selected.id.slice(0, 8)}.md`,
        filters: [{ name: 'Markdown', extensions: ['md'] }],
      });
      if (!dest) return;
      await writeTextFile(dest, buildCaptureMarkdown(selected));
      exportToastSuccess(dest);
    } catch (err) {
      exportToastError(err);
    }
  };

  return (
    <div className="h-full flex gap-0 overflow-hidden -mx-8">
      <input
        ref={uploadInputRef}
        type="file"
        accept={CAPTURE_AUDIO_MIME}
        onChange={(e) => handleUploadFile(e, 'file')}
        className="hidden"
      />
      <input
        ref={fileInputRef}
        type="file"
        accept={CAPTURE_AUDIO_MIME}
        onChange={(e) => handleUploadFile(e, 'file')}
        className="hidden"
      />

      {/* Left: capture list */}
      <div className="w-[340px] shrink-0">
        <ListPane>
          <ListPaneHeader>
            <ListPaneTitleRow>
              <ListPaneTitle>{t('captures.title')}</ListPaneTitle>
              <Badge
                variant="secondary"
                className="h-5 px-1.5 -ml-2 text-[10px] font-medium text-accent bg-accent/10 border border-accent/20"
              >
                {t('captures.beta')}
              </Badge>
            </ListPaneTitleRow>
            <ListPaneSearch
              value={search}
              onChange={setSearch}
              placeholder={t('captures.searchPlaceholder')}
            />
          </ListPaneHeader>

          <ListPaneScroll>
            <div className="px-4 pb-6 space-y-1">
              {capturesLoading ? (
                <div className="px-4 py-12 flex items-center justify-center text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" />
                </div>
              ) : filtered.length === 0 ? (
                <div className="px-4 py-12 text-center text-sm text-muted-foreground">
                  {search ? (
                    <p>{t('captures.empty.noMatches', { query: search })}</p>
                  ) : (
                    <p>{t('captures.empty.none')}</p>
                  )}
                </div>
              ) : (
                filtered.map((capture) => {
                const isActive = selectedId === capture.id;
                return (
                  <button
                    type="button"
                    key={capture.id}
                    onClick={() => setSelectedId(capture.id)}
                    className={cn(
                      'w-full text-left p-3 rounded-lg transition-colors block',
                      isActive
                        ? 'bg-muted/70 border border-border'
                        : 'border border-transparent hover:bg-muted/30',
                    )}
                  >
                    <div className="flex items-center gap-2 mb-1.5">
                      <span className="text-[11px] text-muted-foreground font-medium">
                        {formatDate(capture.created_at)}
                      </span>
                      <div className="flex-1" />
                      <span className="text-[10px] text-muted-foreground/70 tabular-nums">
                        {formatDuration(capture.duration_ms)}
                      </span>
                    </div>
                    <div className="text-[13px] text-foreground/90 line-clamp-2 leading-snug mb-2">
                      {snippetOf(capture)}
                    </div>
                    <SourceBadge source={capture.source} />
                  </button>
                );
              })
            )}
            </div>
          </ListPaneScroll>
        </ListPane>
      </div>

      {/* Right: capture detail */}
      <div className="flex-1 flex flex-col relative overflow-hidden min-w-0">
        <div className="absolute top-0 left-0 right-0 h-20 bg-gradient-to-b from-background to-transparent z-10 pointer-events-none" />

        {/* Top action bar */}
        <div className="absolute top-0 left-0 right-0 z-20 px-8">
          <div className="flex items-center gap-3 py-4">
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <span className="w-1.5 h-1.5 rounded-full bg-accent" />
              <span>
                {t('captures.header.modelSummary', {
                  stt: sttModel.charAt(0).toUpperCase() + sttModel.slice(1),
                })}
              </span>
            </div>
            <div className="flex-1" />
            {session.pillState !== 'hidden' && (
              <CapturePill
                state={session.pillState}
                elapsedMs={session.pillElapsedMs}
                errorMessage={session.errorMessage}
                onDismiss={session.dismissError}
                onStop={session.isRecording ? session.stopRecording : undefined}
              />
            )}
            {session.pillState === 'hidden' && (
              <>
                <Button variant="outline" asChild>
                  <Link to="/settings/captures">
                    <Settings2 className="mr-2 h-4 w-4" />
                    {t('captures.actions.configure')}
                  </Link>
                </Button>
                {readiness.canRecord && (
                  <Button
                    variant="outline"
                    onClick={handleUploadClick}
                    disabled={session.isUploading}
                  >
                    {session.isUploading ? (
                      <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    ) : (
                      <Upload className="h-4 w-4 mr-2" />
                    )}
                    {session.isUploading ? t('captures.actions.importing') : t('captures.actions.import')}
                  </Button>
                )}
              </>
            )}
            {/* Hide Dictate when recording readiness fails so the user can't kick off
                a capture that has nowhere to land. Stop stays visible if a
                recording is somehow already in flight (e.g. a model was
                uninstalled mid-record) so the user can always cancel. */}
            {(readiness.canRecord || session.isRecording) && (
              <Button
                onClick={session.toggleRecording}
                disabled={session.isUploading && !session.isRecording}
                className="relative overflow-hidden transition-all bg-accent text-accent-foreground hover:bg-accent/90"
              >
                {session.isRecording ? (
                  <>
                    <Square className="h-4 w-4 mr-2 fill-current" />
                    {t('captures.actions.stop')}
                  </>
                ) : (
                  <>
                    <Mic className="h-4 w-4 mr-2" />
                    {t('captures.actions.dictate')}
                  </>
                )}
              </Button>
            )}
          </div>
        </div>

        {selected ? (
          <div className="flex-1 overflow-y-auto pt-20 px-8 pb-8">
            {/* Meta row */}
            <div className="flex items-center gap-3 mb-4 text-xs text-muted-foreground">
              <span>{formatAbsoluteDate(selected.created_at)}</span>
              {selected.language && (
                <>
                  <span className="text-muted-foreground/40">·</span>
                  <span>{selected.language.toUpperCase()}</span>
                </>
              )}
              <span className="text-muted-foreground/40">·</span>
              <SourceBadge source={selected.source} />
            </div>

            {/* Audio player card */}
            <div className="rounded-xl border border-border bg-muted/20 p-4 mb-6">
              <CaptureInlinePlayer
                captureId={selected.id}
                fallbackDurationMs={selected.duration_ms}
              />
            </div>

            {/* Transcript header */}
            <div className="flex items-center gap-3 mb-3">
              <span className="text-xs font-medium text-muted-foreground">
                {t('captures.transcript.raw')}
              </span>
              <div className="flex-1" />
              <span className="text-xs text-muted-foreground">
                {selected.stt_model
                  ? t('captures.transcript.rawHint', { model: selected.stt_model })
                  : null}
              </span>
            </div>

            {/* Transcript body */}
            <div className="rounded-xl border border-border bg-muted/10">
              <Textarea
                key={selected.id}
                defaultValue={selected.transcript_raw}
                readOnly
                className="text-[15px] leading-relaxed min-h-[260px] border-0 bg-transparent resize-none focus-visible:ring-0 focus-visible:ring-offset-0 p-6"
              />
            </div>

            {/* Bottom actions */}
            <div className="flex items-center gap-2 mt-4 flex-wrap">
              <Button variant="outline" size="sm" onClick={handleCopy}>
                <Copy className="h-3.5 w-3.5 mr-1.5" />
                {t('captures.actions.copy')}
              </Button>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="outline" size="sm">
                    <Download className="h-3.5 w-3.5 mr-1.5" />
                    {t('captures.actions.export')}
                    <ChevronDown className="h-3.5 w-3.5 ml-1 opacity-70" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="w-56">
                  <DropdownMenuLabel className="text-[11px] font-medium text-muted-foreground uppercase tracking-wide">
                    {t('captures.actions.exportDropdownLabel')}
                  </DropdownMenuLabel>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem onClick={handleExportAudio}>
                    <FileAudio className="h-3.5 w-3.5 mr-2 text-muted-foreground" />
                    {t('captures.actions.exportAudio')}
                  </DropdownMenuItem>
                  <DropdownMenuItem onClick={handleExportTranscript}>
                    <Captions className="h-3.5 w-3.5 mr-2 text-muted-foreground" />
                    {t('captures.actions.exportTranscript')}
                  </DropdownMenuItem>
                  <DropdownMenuItem onClick={handleExportMarkdown}>
                    <FileText className="h-3.5 w-3.5 mr-2 text-muted-foreground" />
                    {t('captures.actions.exportMarkdown')}
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
              <div className="flex-1" />
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setDeleteDialogOpen(true)}
                disabled={deleteMutation.isPending}
                className="text-muted-foreground "
              >
                {deleteMutation.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
                ) : (
                  <Trash2 className="h-3.5 w-3.5 mr-1.5" />
                )}
                {t('captures.actions.delete')}
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex-1 flex items-center justify-center text-muted-foreground pt-20">
            {capturesLoading ? (
              <div className="text-center space-y-3">
                <Captions className="h-10 w-10 mx-auto opacity-40" />
                <p className="text-sm">{t('captures.empty.loading')}</p>
              </div>
            ) : captures.length ? (
              <div className="text-center space-y-3">
                <Captions className="h-10 w-10 mx-auto opacity-40" />
                <p className="text-sm">{t('captures.empty.pickOne')}</p>
              </div>
            ) : hotkeyEnabled && !readiness.canRecord ? (
              <DictationReadinessChecklist readiness={readiness} />
            ) : hotkeyEnabled && (pushToTalkKeys.length || toggleToTalkKeys.length) ? (
              <div className="max-w-sm mx-auto text-center space-y-5">
                <div className="space-y-2">
                  {pushToTalkKeys.length ? (
                    <div className="flex items-center justify-center gap-3">
                      <ChordKeys keys={pushToTalkKeys} />
                      <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
                        {t('captures.empty.holdToRecord')}
                      </span>
                    </div>
                  ) : null}
                  {toggleToTalkKeys.length ? (
                    <div className="flex items-center justify-center gap-3">
                      <ChordKeys keys={toggleToTalkKeys} />
                      <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
                        {t('captures.empty.toggleHandsFree')}
                      </span>
                    </div>
                  ) : null}
                </div>
                <p className="text-sm">
                  {t('captures.empty.pressShortcut')}
                </p>
              </div>
            ) : (
              <div className="max-w-sm mx-auto text-center space-y-3">
                <Captions className="h-10 w-10 mx-auto opacity-40" />
                <p className="text-sm">{t('captures.empty.none')}</p>
                <p className="text-xs text-muted-foreground leading-relaxed">
                  {t('captures.empty.turnOnShortcut')}
                </p>
                <Button asChild variant="outline" size="sm">
                  <Link to="/settings/captures">{t('captures.empty.openSettings')}</Link>
                </Button>
              </div>
            )}
          </div>
        )}
      </div>

      <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('captures.deleteDialog.title')}</AlertDialogTitle>
            <AlertDialogDescription>{t('captures.deleteDialog.description')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction asChild>
              <Button
                onClick={() => selected && deleteMutation.mutate(selected.id)}
                disabled={deleteMutation.isPending}
                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              >
                {deleteMutation.isPending ? t('captures.deleteDialog.deleting') : t('common.delete')}
              </Button>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
