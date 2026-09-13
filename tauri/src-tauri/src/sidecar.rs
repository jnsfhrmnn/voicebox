//! JFW-1: Rust-vermittelte Sidecar-Kommandoschicht (Spec "Prozess- und Sicherheitsgrenze").
//!
//! Webviews sprechen den Python-Sidecar nie direkt an und erhalten weder Port
//! noch Token. Alle API-Aufrufe laufen ueber diese typisierten Tauri-Kommandos:
//! sie lesen die `SidecarSession` (Port/Token/Generation) aus dem Rust-State,
//! pruefen das Caller-Fensterlabel gegen eine fachliche Allowlist und
//! injizieren Auth + Generation im Rust-HTTP-Client. Ein Sidecarwechsel
//! (neue Generation) verwirft spaete Antworten: jede Antwort wird gegen die
//! Generation geprueft, unter der sie gestartet wurde.

use std::time::Duration;

use serde::Deserialize;
use tauri::{AppHandle, Emitter, Manager, State, WebviewWindow};

use crate::ServerState;

/// Hauptfenster-Label (Tauri Default).
const MAIN_WINDOW_LABEL: &str = "main";
/// Diktations-Pill-Fenster (siehe `DICTATE_WINDOW_LABEL` in main.rs).
const DICTATE_WINDOW_LABEL: &str = "dictate";

/// Fachliche Allowlist des Hauptfensters: die aktiven Endpunkt-Bereiche des
/// Transkriptionsprofils (Prafixe). Verbotene Flaechen (/generate, /profiles,
/// /stories, /voices, /effects, /samples, /audio/*, /settings/generation, ...)
/// sind bewusst nicht enthalten — sie existieren im Backend auch nicht mehr.
const MAIN_ALLOWLIST: &[&str] = &[
    "/health",
    "/capture/readiness",
    "/captures",
    "/transcribe",
    "/models",
    "/settings/captures",
    "/tasks",
    "/logs",
    "/about",
    "/server",
];

/// Fachliche Allowlist der Diktations-Pill: nur das, was die Pill braucht.
const DICTATE_ALLOWLIST: &[&str] = &["/capture/readiness", "/captures"];

fn allowlist_for(window_label: &str) -> Option<&[&str]> {
    match window_label {
        MAIN_WINDOW_LABEL => Some(MAIN_ALLOWLIST),
        DICTATE_WINDOW_LABEL => Some(DICTATE_ALLOWLIST),
        _ => None,
    }
}

/// Prafix-Match gegen die Allowlist: exakter Pfad oder Pfadsegment-Grenze.
/// Query-Strings (`/captures?limit=50`) werden vor dem Match entfernt; sie
/// sind Teil der Anfrage, nicht des Routenpfads.
fn is_allowed(path: &str, allowlist: &[&str]) -> bool {
    let route = path.split('?').next().unwrap_or(path);
    allowlist.iter().any(|allowed| {
        route == *allowed || (route.starts_with(allowed) && route.as_bytes()[allowed.len()] == b'/')
    })
}

/// Pfad-Validierung: nur relative Routenpfade, keine absoluten URLs, keine
/// Whitespace/Backslash-Injection in die reqwest-URL.
fn is_valid_path(path: &str) -> bool {
    path.starts_with('/')
        && !path.contains(' ')
        && !path.contains("\\")
        && !path.contains("://")
}

struct Session {
    base_url: String,
    token: String,
    generation: u64,
}

fn session(state: &ServerState) -> Result<Session, String> {
    let port = state.sidecar_port.lock().unwrap().ok_or_else(|| "Sidecar ist nicht gestartet".to_string())?;
    let token = state.api_token.lock().unwrap().clone().ok_or_else(|| {
        "Kein API-Token im State — Sidecar wurde ohne JFW-1-Handshake gestartet".to_string()
    })?;
    let generation = *state.generation.lock().unwrap();
    Ok(Session {
        base_url: format!("http://127.0.0.1:{port}"),
        token,
        generation,
    })
}

/// Caller-Check: das aufrufende Webview wird von Tauri injiziert; sein Label
/// muss eine fachliche Allowlist besitzen und der Pfad darin erlaubt sein.
fn check_caller(window: &WebviewWindow, path: &str) -> Result<(), String> {
    if !is_valid_path(path) {
        return Err(format!("Ungueltiger Pfad: '{path}'"));
    }
    let caller = window.label();
    let allowlist = allowlist_for(caller).ok_or_else(|| {
        format!("Fenster '{caller}' hat keine Sidecar-Allowlist (Custom-Command ohne fachliche Erlaubnis)")
    })?;
    if !is_allowed(path, allowlist) {
        return Err(format!("Pfad '{path}' ist fuer Fenster '{caller}' nicht erlaubt"));
    }
    Ok(())
}

fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(Duration::from_secs(300))
        .build()
        .expect("reqwest-Client")
}

/// Sidecarwechsel-Wache: wenn die Generation zwischen Request-Start und
/// Antwort gewechselt hat, ist diese Antwort spaet — verworfen.
fn generation_stale(state: &ServerState, started_at: u64) -> bool {
    *state.generation.lock().unwrap() != started_at
}

#[tauri::command]
pub async fn sidecar_request(
    window: WebviewWindow,
    state: State<'_, ServerState>,
    method: String,
    path: String,
    body: Option<serde_json::Value>,
) -> Result<serde_json::Value, String> {
    check_caller(&window, &path)?;
    let sess = session(&state)?;

    let mut req = client()
        .request(
            reqwest::Method::from_bytes(method.as_bytes())
                .map_err(|e| format!("Ungueltige Methode: {e}"))?,
            format!("{}{}", sess.base_url, path),
        )
        .header("Authorization", format!("Bearer {}", sess.token))
        .header("X-JFWHISPER-Generation", sess.generation.to_string());

    if let Some(b) = &body {
        req = req.json(b);
    }

    let resp = req.send().await.map_err(|e| e.to_string())?;
    if generation_stale(&state, sess.generation) {
        return Err("Sidecar wurde neu gestartet — Antwort verworfen".to_string());
    }
    let status = resp.status();
    let value: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        let detail = value
            .get("detail")
            .map(|d| d.to_string())
            .unwrap_or_else(|| format!("HTTP {status}"));
        return Err(detail);
    }
    Ok(value)
}

#[tauri::command]
pub async fn sidecar_upload(
    window: WebviewWindow,
    state: State<'_, ServerState>,
    path: String,
    filename: String,
    content_type: String,
    data_base64: String,
    fields: Option<serde_json::Value>,
) -> Result<serde_json::Value, String> {
    check_caller(&window, &path)?;
    let sess = session(&state)?;

    use base64::Engine as _;
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(&data_base64)
        .map_err(|e| format!("Ungueltiges Base64: {e}"))?;

    let mut form = reqwest::multipart::Form::new().part(
        "file",
        reqwest::multipart::Part::bytes(bytes)
            .file_name(filename)
            .mime_str(&content_type).map_err(|e| format!("Ungueltiger Content-Type: {e}"))?,
    );
    if let Some(fields_value) = &fields {
        if let Some(obj) = fields_value.as_object() {
            for (k, v) in obj {
                form = form.text(k.clone(), v.as_str().unwrap_or_default().to_string());
            }
        }
    }

    let resp = client()
        .post(format!("{}{}", sess.base_url, path))
        .header("Authorization", format!("Bearer {}", sess.token))
        .header("X-JFWHISPER-Generation", sess.generation.to_string())
        .multipart(form)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if generation_stale(&state, sess.generation) {
        return Err("Sidecar wurde neu gestartet — Antwort verworfen".to_string());
    }
    let status = resp.status();
    let value: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        let detail = value
            .get("detail")
            .map(|d| d.to_string())
            .unwrap_or_else(|| format!("HTTP {status}"));
        return Err(detail);
    }
    Ok(value)
}

#[derive(serde::Serialize)]
pub struct BytePayload {
    pub base64: String,
    pub content_type: String,
}

/// Binärantwort (z. B. Capture-Audio) als Base64 — die Webview bekommt nie
/// einen direkten Loopback-Zugang, sondern nur den validierten Inhalt.
#[tauri::command]
pub async fn sidecar_fetch_bytes(
    window: WebviewWindow,
    state: State<'_, ServerState>,
    path: String,
) -> Result<BytePayload, String> {
    check_caller(&window, &path)?;
    let sess = session(&state)?;

    use base64::Engine as _;
    let resp = client()
        .get(format!("{}{}", sess.base_url, path))
        .header("Authorization", format!("Bearer {}", sess.token))
        .header("X-JFWHISPER-Generation", sess.generation.to_string())
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if generation_stale(&state, sess.generation) {
        return Err("Sidecar wurde neu gestartet — Antwort verworfen".to_string());
    }
    let status = resp.status();
    let content_type = resp
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("application/octet-stream")
        .to_string();
    if !status.is_success() {
        return Err(format!("HTTP {status}"));
    }
    let bytes = resp.bytes().await.map_err(|e| e.to_string())?;
    Ok(BytePayload {
        base64: base64::engine::general_purpose::STANDARD.encode(&bytes),
        content_type,
    })
}

/// SSE-Stream (z. B. Modell-Download-Fortschritt) als Tauri-Events an den
/// autorisierten Caller. Channel-Bindung: `(caller_window_label, generation)` —
/// ein Sidecarwechsel beendet den Stream, spate Events erreichen niemanden.
#[tauri::command]
pub async fn sidecar_stream(
    window: WebviewWindow,
    state: State<'_, ServerState>,
    path: String,
) -> Result<u64, String> {
    check_caller(&window, &path)?;
    let sess = session(&state)?;

    use futures_util::StreamExt as _;
    let resp = client()
        .get(format!("{}{}", sess.base_url, path))
        .header("Authorization", format!("Bearer {}", sess.token))
        .header("X-JFWHISPER-Generation", sess.generation.to_string())
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if !resp.status().is_success() {
        return Err(format!("HTTP {}", resp.status()));
    }

    let generation = sess.generation;
    let window_label = window.label().to_string();
    let app: AppHandle = window.app_handle().clone();
    let event_name = format!("sidecar:sse:{generation}");
    let end_event = format!("sidecar:sse-end:{generation}");

    tokio::spawn(async move {
        let mut stream = resp.bytes_stream();
        while let Some(chunk) = stream.next().await {
            // Sidecarwechsel: Stream wird hier beendet, spate Events fallen weg.
            if *app.state::<ServerState>().generation.lock().unwrap() != generation {
                break;
            }
            let chunk = match chunk {
                Ok(c) => c,
                Err(_) => break,
            };
            for line in String::from_utf8_lossy(&chunk).lines() {
                let Some(payload) = line.strip_prefix("data:") else {
                    continue;
                };
                let payload = payload.trim();
                if payload.is_empty() {
                    continue;
                }
                // Fortschritt erreicht ausschliesslich den autorisierten Caller.
                let _ = app.emit_to(&window_label, &event_name, payload);
            }
        }
        // Stream-Ende (Sidecarwechsel oder Verbindungsriss) an den Caller melden.
        let _ = app.emit_to(&window_label, &end_event, "ended");
    });

    Ok(generation)
}

#[allow(dead_code)]
#[derive(Deserialize)]
struct HandshakeProbe {
    #[allow(dead_code)]
    port: u64,
}
