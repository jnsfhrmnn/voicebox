import type { LanguageCode } from '@/lib/constants/languages';
import { sidecarTransport } from './sidecarTransport';
import type {
  ActiveTasksResponse,
  CaptureCreateResponse,
  CaptureListResponse,
  CaptureReadinessResponse,
  CaptureSettings,
  CaptureSettingsUpdate,
  CaptureSource,
  FilesystemHealthResponse,
  HealthResponse,
  ModelDownloadRequest,
  ModelStatusListResponse,
  TranscriptionResponse,
  WhisperModelSize,
} from './types';

/**
 * JFW-1: API-Client des Transkriptionsprofils.
 *
 * Alle Aufrufe laufen ueber den Sidecar-Transport (tauri/src-tauri/src/sidecar.rs):
 * in Produktion injiziert Rust Auth + Generation, die Webview kennt weder Port
 * noch Token. Verbotene Flaechen des Profils (/generate, /profiles, /stories,
 * /voices, /effects, /samples, /audio/*, /settings/generation, /mcp/...) haben
 * hier bewusst keine Methoden mehr — sie existieren im Backend auch nicht.
 */
class ApiClient {
  private async request<T>(endpoint: string, options?: RequestInit): Promise<T> {
    // Der Body ist in diesem Client immer JSON; der Transport serialisiert.
    let body: unknown;
    if (typeof options?.body === 'string') {
      body = JSON.parse(options.body);
    } else if (options?.body !== undefined) {
      body = options.body as unknown;
    }
    return sidecarTransport.request<T>(options?.method ?? 'GET', endpoint, body);
  }

  // Health
  async getHealth(): Promise<HealthResponse> {
    return this.request<HealthResponse>('/health');
  }

  /** JFW-1: Health-Dateisystem-Info ueber den Sidecar-Transport. */
  async getHealthFilesystem(): Promise<FilesystemHealthResponse> {
    return this.request<FilesystemHealthResponse>('/health/filesystem');
  }

  // Transcription (direkter Upload, z. B. aus dem Diktations-Pfad)
  async transcribeAudio(
    file: File,
    options?: { language?: LanguageCode; model?: WhisperModelSize },
  ): Promise<TranscriptionResponse> {
    const fields: Record<string, string> = {};
    if (options?.language) fields.language = options.language;
    if (options?.model) fields.model = options.model;
    return sidecarTransport.upload<TranscriptionResponse>('/transcribe', file, fields);
  }

  // Captures
  async listCaptures(limit = 50, offset = 0): Promise<CaptureListResponse> {
    return this.request<CaptureListResponse>(`/captures?limit=${limit}&offset=${offset}`);
  }

  async createCapture(
    file: File,
    options?: {
      source?: CaptureSource;
      language?: LanguageCode;
      sttModel?: WhisperModelSize;
    },
  ): Promise<CaptureCreateResponse> {
    const fields: Record<string, string> = { source: options?.source ?? 'file' };
    if (options?.language) fields.language = options.language;
    if (options?.sttModel) fields.stt_model = options.sttModel;
    return sidecarTransport.upload<CaptureCreateResponse>('/captures', file, fields);
  }

  async deleteCapture(captureId: string): Promise<{ message: string }> {
    return this.request<{ message: string }>(`/captures/${captureId}`, {
      method: 'DELETE',
    });
  }

  /** JFW-1: Capture-Audio ueber den Sidecar-Transport (Auth/Generation). */
  async getCaptureAudio(
    captureId: string,
  ): Promise<{ blob: Blob; contentType: string }> {
    return sidecarTransport.fetchBytes(`/captures/${captureId}/audio`);
  }

  /** JFW-1: SSE-Fortschritt ueber den Sidecar-Transport. */
  streamProgress(
    path: string,
    onData: (data: string) => void,
    onEnd?: () => void,
  ): Promise<{ generation: number; close: () => void }> {
    return sidecarTransport.stream(path, onData, onEnd);
  }

  // Settings
  async getCaptureSettings(): Promise<CaptureSettings> {
    return this.request<CaptureSettings>('/settings/captures');
  }

  async updateCaptureSettings(patch: CaptureSettingsUpdate): Promise<CaptureSettings> {
    return this.request<CaptureSettings>('/settings/captures', {
      method: 'PUT',
      body: JSON.stringify(patch),
    });
  }

  async getCaptureReadiness(): Promise<CaptureReadinessResponse> {
    return this.request<CaptureReadinessResponse>('/capture/readiness');
  }

  // Model Management (STT-Modelle)
  async getModelStatus(): Promise<ModelStatusListResponse> {
    return this.request<ModelStatusListResponse>('/models/status');
  }

  async getModelsCacheDir(): Promise<{ path: string }> {
    return this.request<{ path: string }>('/models/cache-dir');
  }

  async migrateModels(
    destination: string,
  ): Promise<{ source: string; destination: string; moved: number; errors: string[] }> {
    return this.request('/models/migrate', {
      method: 'POST',
      body: JSON.stringify({ destination }),
    });
  }

  async triggerModelDownload(modelName: string): Promise<{ message: string }> {
    return this.request<{ message: string }>('/models/download', {
      method: 'POST',
      body: JSON.stringify({ model_name: modelName } as ModelDownloadRequest),
    });
  }

  async cancelDownload(modelName: string): Promise<{ message: string }> {
    return this.request<{ message: string }>('/models/download/cancel', {
      method: 'POST',
      body: JSON.stringify({ model_name: modelName } as ModelDownloadRequest),
    });
  }

  async deleteModel(modelName: string): Promise<{ message: string }> {
    return this.request<{ message: string }>(`/models/${modelName}`, {
      method: 'DELETE',
    });
  }

  async unloadModel(modelName: string): Promise<{ message: string }> {
    return this.request<{ message: string }>(`/models/${modelName}/unload`, {
      method: 'POST',
    });
  }

  // Task Management
  async getActiveTasks(): Promise<ActiveTasksResponse> {
    return this.request<ActiveTasksResponse>('/tasks/active');
  }

  async clearAllTasks(): Promise<{ message: string }> {
    return this.request<{ message: string }>('/tasks/clear', { method: 'POST' });
  }
}

export const apiClient = new ApiClient();
