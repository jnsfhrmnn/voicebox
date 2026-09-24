/**
 * JFW-Folge-Block: API-Client fuer die Vertragsoberflaechen JFW-2 … JFW-13.
 *
 * Alle Aufrufe laufen wie im ApiClient ueber den Sidecar-Transport (Auth und
 * Generation injiziert Rust). Hier ausschliesslich die bestehenden
 * Vertrags-Endpunkte — keine neuen Flaechen, keine Netzwerkpfade.
 */
import { sidecarTransport } from './sidecarTransport';

export type JsonObject = Record<string, unknown>;

// ---------------------------------------------------------------------------
// JFW-6 Aufnahme-Run (RunSession)
// ---------------------------------------------------------------------------

export type RecordingStatus =
  | 'idle'
  | 'starting'
  | 'recording'
  | 'stopping'
  | 'securing'
  | 'discarding'
  | 'canceled'
  | 'secured'
  | 'failed';

export interface SoundCueMark {
  kind: 'start_ton' | 'stopp_ton' | 'fehler_ton';
  start_100ns: number;
  end_100ns: number;
}

export interface RecordingRunSummary {
  identity_hash: string;
  run_id: string;
  status: RecordingStatus | string;
  stop_cause?: string | null;
  stop_reason?: string | null;
  audio_hash?: string | null;
  manifest_hash?: string | null;
  frames?: JsonObject;
  gaps?: JsonObject[];
  sound_cue_marks?: SoundCueMark[];
  recovery_status?: JsonObject;
  transcription_authorized?: boolean;
  handoff_count?: number;
  reason_code?: string | null;
}

// ---------------------------------------------------------------------------
// JFW-7 Diktat / JFW-2 Alignment — Statuszustaende
// ---------------------------------------------------------------------------

export interface DictationRunSummary {
  identity_hash: string;
  status: string; // waiting_for_backend | waiting_for_model | queued | transcribing | raw_ready | committed | no_speech | failed | canceled | interrupted
  reason_code?: string | null;
  waiting_reason?: string | null;
  [key: string]: unknown;
}

export interface AlignmentRunSummary {
  identity_hash: string;
  status: string;
  reason_code?: string | null;
  coverage?: JsonObject;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// JFW-3 Diarisierung
// ---------------------------------------------------------------------------

export interface DiarizationSummary {
  identity_hash: string;
  status: string; // diarized | partially_diarized | no_speech | failed | waiting_for_local_artifact | ...
  reason_code?: string | null;
  speaker_spec?: JsonObject;
  quality?: JsonObject;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// JFW-4 Export
// ---------------------------------------------------------------------------

export interface ExportReadiness {
  status?: string;
  partial_mode?: string | null; // timing_only | speaker_only | null
  dimensions?: JsonObject;
  counters?: JsonObject;
  warnings?: string[];
  [key: string]: unknown;
}

export interface ExportPrepareResponse {
  export_key?: string;
  readiness?: ExportReadiness;
  expected_files?: string[];
  existing_targets?: string[];
  not_exportable_ranges?: JsonObject[];
  trust_boundary_paths?: string[];
  [key: string]: unknown;
}

export interface ExportStatusResponse {
  export_key: string;
  status: string;
  reason_code?: string | null;
  readiness?: ExportReadiness;
  expected_files?: string[];
  formats?: string[];
  export_profile?: string;
  name_policy?: string;
  partial_mode?: string | null;
  target_dir?: string | null;
  job_id?: string;
  transcript_revision_id?: string;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// JFW-5 Batch
// ---------------------------------------------------------------------------

export interface BatchSelectionRef {
  kind: 'file' | 'folder';
  path: string;
}

export interface BatchDiscoveryBundle {
  selection?: BatchSelectionRef[];
  elements?: JsonObject[];
  excluded?: { path: string; reason_code: string }[];
  duplicate_groups?: string[][];
  counts?: { included: number; excluded: number };
  total_size?: number;
  trust_boundary_paths?: string[];
  [key: string]: unknown;
}

export interface BatchSnapshotInfo {
  identity_hash?: string;
  order?: string[];
  profile?: JsonObject;
  phases?: string[];
  output_policy?: JsonObject;
  resource_policy?: JsonObject;
  partial_failure_policy?: string;
  trust_boundary_paths?: string[];
  [key: string]: unknown;
}

export interface BatchItemView {
  item_id: string;
  order_index: number;
  status: string;
  current_phase?: string | null;
  phases?: JsonObject;
  result_refs?: JsonObject;
  warnings?: string[];
  reason_code?: string | null;
  source_path?: string | null;
  output?: JsonObject;
}

export interface BatchView {
  identity_hash: string;
  batch_id: string;
  status: string;
  reason_code?: string | null;
  revision_no?: number;
  phases?: string[];
  partial_failure_policy?: string;
  resource_policy?: JsonObject;
  aggregates?: { status: string; counts: JsonObject };
  items?: BatchItemView[];
  attempts?: JsonObject[];
  warnings?: string[];
  available_actions?: string[];
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// JFW-8 Zieluebergabe (Delivery)
// ---------------------------------------------------------------------------

export interface DeliverySummary {
  identity_hash: string;
  status: string; // pending | attempting | succeeded | failed | unknown | blocked | deleted
  target_confirmed?: boolean;
  manual_only?: boolean;
  blocked_reason?: string | null;
  reason_code?: string | null;
  budget_consumed?: boolean;
  parent_operation_id?: string | null;
  error_trace?: JsonObject;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// JFW-13 Protokoll (Minutes) + Pseudonymregister
// ---------------------------------------------------------------------------

export interface MinutesSummary {
  minutes_key: string;
  status: string; // blockiert | running | committed | failed | abgebrochen | invalidiert | ...
  reason_code?: string | null;
  reident_hinweise?: string[];
  fortsetzbar?: boolean;
  [key: string]: unknown;
}

export interface RegisterSummary {
  register_id: string;
  register_revision?: string;
  entries?: JsonObject[];
  [key: string]: unknown;
}

function req<T>(method: string, endpoint: string, body?: unknown): Promise<T> {
  return sidecarTransport.request<T>(method, endpoint, body);
}

export const jfwApi = {
  // --- JFW-6 -------------------------------------------------------------
  submitRecording(body: {
    run_id: string;
    device_stable_id_hash: string;
    format: JsonObject;
    contract_version?: string;
  }): Promise<{ identity_hash: string; outcome: string }> {
    return req('POST', '/recording/submit', body);
  },
  beginRecording(identityHash: string): Promise<JsonObject> {
    return req('POST', `/recording/${identityHash}/begin`, {});
  },
  stopRecording(identityHash: string, cause: string): Promise<JsonObject> {
    return req('POST', `/recording/${identityHash}/stop`, { cause });
  },
  commitRecording(
    identityHash: string,
    body: {
      stop_reason: string;
      audio_hash: string;
      manifest: JsonObject;
      frames?: JsonObject;
      gaps?: JsonObject[];
      sound_cue_marks?: SoundCueMark[];
      recovery_status?: JsonObject;
    },
  ): Promise<JsonObject> {
    return req('POST', `/recording/${identityHash}/commit`, body);
  },
  cancelRecording(
    identityHash: string,
    confirmed: boolean,
    deletionContractHash?: string | null,
  ): Promise<JsonObject> {
    return req('POST', `/recording/${identityHash}/cancel`, {
      confirmed,
      deletion_contract_hash: deletionContractHash ?? null,
    });
  },
  handoffRecording(
    identityHash: string,
    audioHash: string,
    manifestHash: string,
  ): Promise<JsonObject> {
    return req('POST', `/recording/${identityHash}/handoff`, {
      audio_hash: audioHash,
      manifest_hash: manifestHash,
    });
  },
  getRecording(identityHash: string): Promise<RecordingRunSummary> {
    return req('GET', `/recording/${identityHash}`);
  },

  // --- JFW-7 Diktat ------------------------------------------------------
  getDictation(identityHash: string): Promise<DictationRunSummary> {
    return req('GET', `/dictation/${identityHash}`);
  },

  // --- JFW-2 Alignment ---------------------------------------------------
  getAlignment(identityHash: string): Promise<AlignmentRunSummary> {
    return req('GET', `/alignment/${identityHash}`);
  },
  cancelAlignment(identityHash: string): Promise<JsonObject> {
    return req('POST', `/alignment/${identityHash}/cancel`, {});
  },

  // --- JFW-3 Diarisierung ------------------------------------------------
  getDiarization(identityHash: string): Promise<DiarizationSummary> {
    return req('GET', `/diarization/${identityHash}`);
  },
  cancelDiarization(identityHash: string): Promise<JsonObject> {
    return req('POST', `/diarization/${identityHash}/cancel`, {});
  },

  // --- JFW-4 Export ------------------------------------------------------
  prepareExport(body: JsonObject): Promise<ExportPrepareResponse> {
    return req('POST', '/export/prepare', body);
  },
  runExport(body: JsonObject): Promise<ExportStatusResponse> {
    return req('POST', '/export/run', body);
  },
  cancelExport(exportKey: string): Promise<{ export_key: string; outcome: string }> {
    return req('POST', `/export/${exportKey}/cancel`, {});
  },
  getExport(exportKey: string): Promise<ExportStatusResponse> {
    return req('GET', `/export/${exportKey}`);
  },

  // --- JFW-5 Batch -------------------------------------------------------
  discoverBatch(selection: BatchSelectionRef[]): Promise<BatchDiscoveryBundle> {
    return req('POST', '/batch/discover', { selection });
  },
  confirmBatch(body: {
    selection: BatchSelectionRef[];
    profile: JsonObject;
    created_at?: string | null;
  }): Promise<BatchSnapshotInfo> {
    return req('POST', '/batch/confirm', body);
  },
  startBatch(batchId: string): Promise<JsonObject> {
    return req('POST', `/batch/${batchId}/start`, {});
  },
  pauseBatch(batchId: string): Promise<JsonObject> {
    return req('POST', `/batch/${batchId}/pause`, {});
  },
  resumeBatch(batchId: string, confirmedOriginal: boolean): Promise<JsonObject> {
    return req('POST', `/batch/${batchId}/resume`, { confirmed_original: confirmedOriginal });
  },
  cancelBatch(batchId: string): Promise<JsonObject> {
    return req('POST', `/batch/${batchId}/cancel`, {});
  },
  getBatch(batchId: string): Promise<BatchView> {
    return req('GET', `/batch/${batchId}`);
  },

  // --- JFW-8 Delivery ----------------------------------------------------
  submitDelivery(body: {
    delivery_operation_id: string;
    handoff: JsonObject;
    target_snapshot?: JsonObject;
    parent_operation_id?: string | null;
  }): Promise<{ identity_hash: string; outcome: string }> {
    return req('POST', '/delivery/submit', body);
  },
  deliveryManual(identityHash: string): Promise<JsonObject> {
    return req('POST', `/delivery/${identityHash}/manual`, { reason_code: 'manual_only' });
  },
  deliveryCopy(identityHash: string, confirmed: boolean): Promise<JsonObject> {
    return req('POST', `/delivery/${identityHash}/copy`, { confirmed });
  },
  deliveryRetry(
    identityHash: string,
    handoff: JsonObject,
    targetSnapshot: JsonObject,
  ): Promise<JsonObject> {
    return req('POST', `/delivery/${identityHash}/retry`, {
      handoff,
      target_snapshot: targetSnapshot,
    });
  },
  getDelivery(identityHash: string): Promise<DeliverySummary> {
    return req('GET', `/delivery/${identityHash}`);
  },

  // --- JFW-13 Minutes ----------------------------------------------------
  prepareMinutes(body: JsonObject): Promise<MinutesSummary> {
    return req('POST', '/minutes/prepare', body);
  },
  runMinutes(body: JsonObject): Promise<MinutesSummary> {
    return req('POST', '/minutes/run', body);
  },
  cancelMinutes(minutesKey: string): Promise<JsonObject> {
    return req('POST', `/minutes/${minutesKey}/cancel`, {});
  },
  getMinutes(minutesKey: string): Promise<MinutesSummary> {
    return req('GET', `/minutes/${minutesKey}`);
  },
  getRegister(registerId: string): Promise<RegisterSummary> {
    return req('GET', `/minutes/register/${registerId}`);
  },
  deleteRegister(registerId: string): Promise<JsonObject> {
    return req('POST', `/minutes/register/${registerId}/delete`, {});
  },
  registerExportCheck(registerId: string, registerRevision: string): Promise<JsonObject> {
    return req('POST', `/minutes/register/${registerId}/export-check`, {
      register_revision: registerRevision,
    });
  },
};
