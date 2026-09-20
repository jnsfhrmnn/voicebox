// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod accessibility;
mod audio_capture;
mod audio_output;
mod backend;
mod clipboard;
mod focus_capture;
#[cfg(desktop)]
mod hotkey_monitor;
mod input_monitoring;
#[cfg(desktop)]
mod key_codes;
mod keyboard_layout;
mod sidecar;
mod synthetic_keys;

use std::sync::Mutex;
use tauri::{command, State, Manager, WindowEvent, Emitter, Listener, RunEvent, WebviewUrl, WebviewWindowBuilder, PhysicalPosition};
use tauri_plugin_shell::ShellExt;
use tokio::sync::mpsc;

// JFW-12 B2/B3: BackendSupervisor (serieller Actor) + typisierte Zustände.
use backend::state::{BackendVariant, SidecarInstance};
use backend::supervisor::{AdmissionOutcome, Supervisor};

pub const DICTATE_WINDOW_LABEL: &str = "dictate";
const DICTATE_WINDOW_WIDTH: f64 = 420.0;
const DICTATE_WINDOW_HEIGHT: f64 = 64.0;

/// Create the floating dictate webview hidden. The HotkeyMonitor shows it on
/// chord-start; the frontend hides it when the capture pipeline finishes.
/// Building it at setup avoids a race where the first chord or agent-speech
/// event fires before the webview subscribes to the `dictate:*` events.
#[cfg(desktop)]
fn build_dictate_window(app: &tauri::AppHandle) -> tauri::Result<tauri::WebviewWindow> {
    let window = WebviewWindowBuilder::new(
        app,
        DICTATE_WINDOW_LABEL,
        WebviewUrl::App("?view=dictate".into()),
    )
    .title("Voicebox Dictate")
    .inner_size(DICTATE_WINDOW_WIDTH, DICTATE_WINDOW_HEIGHT)
    .decorations(false)
    .transparent(true)
    .always_on_top(true)
    // Follow the user across macOS Spaces / virtual desktops instead of
    // being pinned to the Space where the window was first created.
    .visible_on_all_workspaces(true)
    .skip_taskbar(true)
    .resizable(false)
    .shadow(false)
    .visible(false)
    .build()?;

    if let Some(monitor) = window.current_monitor()? {
        let monitor_size = monitor.size();
        let win_size = window.outer_size()?;
        let x = (monitor_size.width as i32 - win_size.width as i32) / 2;
        let y = (monitor_size.height as f64 * 0.04) as i32;
        window.set_position(PhysicalPosition::new(x, y))?;
    }

    Ok(window)
}

/// Position, undo click-through, and show the dictate pill window.
///
/// The hide path parks the window at (-10_000, -10_000) and toggles
/// `ignore_cursor_events(true)` so invisible click targets don't leak; we
/// undo both here. Mirrors the logic the hotkey_monitor's
/// `Effect::StartRecording` path runs, minus the focus snapshot — this is
/// for agent-initiated speech, not dictation, so there's no focused text
/// field to paste into.
/// Build the pill webview if it doesn't exist yet. Idempotent — used by
/// agent-speech to prime the webview on speak-start so its listeners can
/// register before the actual show arrives from `audio.onplaying`.
#[cfg(desktop)]
pub fn ensure_dictate_window(app: &tauri::AppHandle) {
    if app.get_webview_window(DICTATE_WINDOW_LABEL).is_none() {
        if let Err(e) = build_dictate_window(app) {
            eprintln!("ensure_dictate_window: failed to build pill: {e}");
        }
    }
}

#[cfg(desktop)]
pub fn show_dictate_window(app: &tauri::AppHandle) {
    // Build on demand so agent-initiated speech works before the user has
    // enabled the global hotkey (the hotkey path is the other place this
    // window gets built, see `enable_hotkey`).
    let window = match app.get_webview_window(DICTATE_WINDOW_LABEL) {
        Some(w) => w,
        None => match build_dictate_window(app) {
            Ok(w) => w,
            Err(e) => {
                eprintln!("show_dictate_window: failed to build pill window: {e}");
                return;
            }
        },
    };
    // current_monitor() returns None when the window has been parked
    // off any display by the hide path; fall back to the primary.
    let monitor = window
        .current_monitor()
        .ok()
        .flatten()
        .or_else(|| window.primary_monitor().ok().flatten());
    if let Some(monitor) = monitor {
        let monitor_pos = monitor.position();
        let monitor_size = monitor.size();
        if let Ok(win_size) = window.outer_size() {
            let x = monitor_pos.x
                + (monitor_size.width as i32 - win_size.width as i32) / 2;
            let y = monitor_pos.y + (monitor_size.height as f64 * 0.04) as i32;
            let _ = window.set_position(PhysicalPosition::new(x, y));
        }
    }
    let _ = window.set_ignore_cursor_events(false);
    let _ = window.show();
}

pub(crate) const SERVER_PORT: u16 = 17493;
struct ServerState {
    /// JFW-12 B8 (Windows): Das Job Object des Sidecar-Prozessbaums. Drop/Close
    /// beendet den kompletten Baum (KILL_ON_JOB_CLOSE). Auf Nicht-Windows bleibt
    /// der Tauri-Shell-Child als Fallback-Pfad erhalten.
    #[cfg(windows)]
    sidecar_job: Mutex<Option<backend::process_windows::SidecarJob>>,
    #[cfg(not(windows))]
    child: Mutex<Option<tauri_plugin_shell::process::CommandChild>>,
    server_pid: Mutex<Option<u32>>,
    models_dir: Mutex<Option<String>>,
    /// JFW-1: pro Start erzeugtes Bearer-Token. Bleibt im Rust-RAM, wird nie an
    /// Webviews oder Logs ausgegeben; der Sidecar bekommt es nur per Umgebung.
    pub(crate) api_token: Mutex<Option<String>>,
    /// JFW-1: Generationennummer des aktiven Sidecars (Channel-Bindung).
    pub(crate) generation: Mutex<u64>,
    /// JFW-1: vom Sidecar im Ready-Handshake gemeldeter dynamischer Port.
    pub(crate) sidecar_port: Mutex<Option<u16>>,
}

/// JFW-1: Pfad der gebündelten CPU-Sidecar-Binary.
///
/// Produktionslayout (tauri build): die Sidecar liegt neben dem App-Exe
/// (externalBin aus tauri.conf.json). Dev-Fallback: das binaries/-Verzeichnis
/// des src-tauri-Projekts (dort liegen die Platzhalter von setup-dev-sidecar.js).
/// JFW-1 (Spec): Single-Instance-Lock pro Datenroot.
///
/// Der Lock liegt unter `<Datenroot>/.jfwhisper-instance.lock` und traegt die
/// PID der laufenden Instanz. Ein toter Prozess (Stale-Lock) wird uebernommen;
/// eine lebende zweite Instanz wird fail-closed abgelehnt — zwei Instanzen
/// duerfen nicht auf denselben Datenroot zugreifen.
fn acquire_instance_lock(data_dir: &std::path::Path) -> Result<(), String> {
    let lock_path = data_dir.join(".jfwhisper-instance.lock");

    if let Ok(content) = std::fs::read_to_string(&lock_path) {
        let pid_str: String = content.lines().next().unwrap_or("").trim().to_string();
        if let Ok(pid) = pid_str.parse::<u32>() {
            if is_process_alive(pid) {
                return Err(format!(
                    "JF Whisper laeuft bereits am Datenroot {} (PID {}). Schliessen Sie die andere Instanz oder waehlen Sie einen anderen Datenroot.",
                    data_dir.display(),
                    pid
                ));
            }
            println!("Stale Instance-Lock (PID {} tot) wird uebernommen", pid);
        }
    }

    let own_pid = std::process::id();
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs().to_string())
        .unwrap_or_default();
    std::fs::write(&lock_path, format!("{}\n{}", own_pid, ts))
        .map_err(|e| format!("Instance-Lock konnte nicht geschrieben werden: {}", e))?;
    Ok(())
}

/// Ist der Prozess mit dieser PID noch aktiv? (Windows: tasklist, POSIX: kill -0)
fn is_process_alive(pid: u32) -> bool {
    #[cfg(windows)]
    {
        let output = std::process::Command::new("tasklist")
            .args(["/FI", &format!("PID eq {}", pid), "/NH"])
            .output()
            .ok();
        match output {
            Some(o) => o.status.success() && String::from_utf8_lossy(&o.stdout).contains(&pid.to_string()),
            None => false,
        }
    }
    #[cfg(not(windows))]
    {
        let status = std::process::Command::new("kill")
            .args(["-0", &pid.to_string()])
            .output();
        matches!(status, Ok(o) if o.status.success())
    }
}

fn resolve_sidecar_path() -> std::path::PathBuf {
    // JFW-1 (Spec): Das Pre-Flight muss EXAKT dieselbe Binary auflösen wie der
    // Runtime-Spawn ueber `app.shell().sidecar("jf-whisper-server")`. Tauri löst
    // den Sidecar relativ zum App-Exe auf: <exe_dir>/jf-whisper-server[.exe] —
    // OHNE Target-Triple (tauri-build legt das externalBin so neben dem Exe ab).
    // Würde das Pre-Flight eine andere Datei prüfen als die, die danach gestartet
    // wird, migrierte es ins Leere (falsche DB / falscher Head) — deshalb identische
    // Auflösung wie relative_command_path() im Shell-Plugin.
    const BASE: &str = "jf-whisper-server";
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            // JFW-12 P3: onedir-Layout bevorzugen — der CPU-Sidecar liegt als Ordner
            // <exe_dir>/jf-whisper-server/ mit dem Exe darin (kein Self-Unpack, Start
            // ~4,6 s statt ~18,4 s). Das Exe wird aus dem Ordner gestartet; cwd muss
            // dann auf den Ordner zeigen (siehe Spawn-Stellen), damit _internal/ gefunden wird.
            let onedir_exe = if cfg!(windows) {
                parent.join(BASE).join(format!("{BASE}.exe"))
            } else {
                parent.join(BASE).join(BASE)
            };
            if onedir_exe.exists() {
                return onedir_exe;
            }
            // Fallback: altes onefile-Einzel-Exe neben dem App-Exe.
            let candidate = if cfg!(windows) {
                parent.join(format!("{BASE}.exe"))
            } else {
                parent.join(BASE)
            };
            if candidate.exists() {
                return candidate;
            }
        }
    }
    // Dev-Fallback (in Release nicht erreichbar — das Pre-Flight ist debug-gated).
    let dev = std::path::PathBuf::from("tauri/src-tauri/binaries");
    if cfg!(windows) {
        dev.join(format!("{BASE}.exe"))
    } else {
        dev.join(BASE)
    }
}

#[command]
async fn start_server(
    app: tauri::AppHandle,
    state: State<'_, ServerState>,
    supervisor: State<'_, Supervisor>,
    models_dir: Option<String>,
) -> Result<(), String> {
    // Store models_dir for use on restart (empty string means reset to default)
    if let Some(ref dir) = models_dir {
        if dir.is_empty() {
            *state.models_dir.lock().unwrap() = None;
        } else {
            *state.models_dir.lock().unwrap() = Some(dir.clone());
        }
    }
    // Check if server is already running (managed by this app instance)
    #[cfg(windows)]
    {
        if state.sidecar_job.lock().unwrap().is_some() {
            return Ok(());
        }
    }
    #[cfg(not(windows))]
    {
        if state.child.lock().unwrap().is_some() {
            // JFW-1: Der Port bleibt im Rust-State; die Webview bekommt nur den
            // Start-Bestatigungs-Marker, nie eine URL.
            return Ok(());
        }
    }

    // JFW-1: kein Wiederverwenden fremder Server mehr. Der Sidecar bindet einen
    // dynamischen Loopback-Port und meldet ihn im Ready-Handshake; ein Prozess,
    // der nur ein Health-Payload spricht, wird nicht uebernommen (Spec).
    #[cfg(unix)]
    {
        // JFW-1: Port-Wiederverwendung entfernt — dynamischer Port + Handshake.
    }
    
    #[cfg(windows)]
    {
        // JFW-1: Port-Wiederverwendung entfernt — dynamischer Port + Handshake.
    }

    // JFW-1: eigener Datenroot %LOCALAPPDATA%\JFWhisper (Profil: app_identity.data_root).
    // Die produktive Voicebox-Installation unter sh.voicebox.app bleibt unangetastet.
    let data_dir = {
        let local = std::env::var("LOCALAPPDATA")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|_| app.path().app_data_dir()
                .expect("no LOCALAPPDATA and no app_data_dir"));
        local.join("JFWhisper")
    };

    // Ensure data directory exists
    std::fs::create_dir_all(&data_dir)
        .map_err(|e| format!("Failed to create data dir: {}", e))?;

    // JFW-1 (Spec): Single-Instance-Lock pro Datenroot — fail-closed bei
    // zweiter Instanz, Stale-PID wird uebernommen.
    acquire_instance_lock(&data_dir)?;

    println!("=================================================================");
    println!("Starting voicebox-server sidecar");
    println!("Data directory: {:?}", data_dir);

    // JFW-12 B2: Supervisor erfährt vom Boot-Versuch (BootingCpu).
    supervisor.boot_started();

    // Check for CUDA backend in data directory (onedir layout: backends/cuda/).
    // JFW-12 Block (d)/(g): Installierte Builds liegen versioniert unter
    // `backends/cuda/<build_id>/` mit atomarem Current-Pointer (B9); der flache
    // Legacy-Pfad (`cuda/jf-whisper-server-cuda.exe`) bleibt als Fallback.
    let cuda_binary = {
        let cuda_dir = data_dir.join("backends").join("cuda");
        let cuda_name = if cfg!(windows) {
            "jf-whisper-server-cuda.exe"
        } else {
            "jf-whisper-server-cuda"
        };
        // 1) Versionierter Build über den Current-Pointer (B9).
        let versioned_exe: Option<std::path::PathBuf> = std::fs::read_to_string(cuda_dir.join("current.json"))
            .ok()
            .and_then(|raw| serde_json::from_str::<backend::artifact::CurrentPointer>(&raw).ok())
            .map(|current| cuda_dir.join(&current.build_id).join(cuda_name));
        let exe_path = match versioned_exe {
            Some(p) if p.exists() => p,
            _ => cuda_dir.join(cuda_name), // 2) Legacy-Flachlayout.
        };
        if exe_path.exists() {
            println!("Found CUDA backend at {:?}", cuda_dir);

            // Version check: run --version from the onedir directory so
            // PyInstaller can find its support files for the fast --version path
            let app_version = app.config().version.clone().unwrap_or_default();
            let version_ok = match std::process::Command::new(&exe_path)
                .arg("--version")
                .current_dir(exe_path.parent().unwrap_or(std::path::Path::new(".")))
                .output()
            {
                Ok(output) => {
                    // Output format: "voicebox-server X.Y.Z\n"
                    let version_str = String::from_utf8_lossy(&output.stdout);
                    let binary_version = version_str.trim().split_whitespace().last().unwrap_or("");
                    if binary_version == app_version {
                        println!("CUDA binary version {} matches app version", binary_version);
                        true
                    } else {
                        println!(
                            "CUDA binary version mismatch: binary={}, app={}. Falling back to CPU.",
                            binary_version, app_version
                        );
                        false
                    }
                }
                Err(e) => {
                    println!("Failed to check CUDA binary version: {}. Falling back to CPU.", e);
                    false
                }
            };

            if version_ok {
                Some(exe_path)
            } else {
                None
            }
        } else {
            println!("No CUDA backend found, using bundled CPU binary");
            None
        }
    };

    // JFW-12 B8: Sidecar wird per CreateProcessW + Job Object erzeugt.
    // Der Tauri-Shell-Spawn ist ersetzt; die Binary-Auflösung bleibt identisch
    // (resolve_sidecar_path / CUDA-Detection).
    let sidecar_exe = resolve_sidecar_path();

    // JFW-1: pro Start zufaelliges Bearer-Token + Generation. Token bleibt im
    // Rust-RAM (state.api_token) und wird nur per Umgebung an den Sidecar
    // uebergeben — nie in Logs, nie an Webviews.
    let api_token = {
        use rand::Rng;
        let mut rng = rand::thread_rng();
        const ALPHABET: [char; 16] = ['a','b','c','d','e','f','g','h','i','j','k','l','m','n','o','p'];
        let token: String = (0..32).map(|_| ALPHABET[rng.gen_range(0usize..16)]).collect();
        *state.api_token.lock().unwrap() = Some(token.clone());
        token
    };
    let generation = {
        let mut g = state.generation.lock().unwrap();
        *g += 1;
        *g
    };

    // Build common args
    let data_dir_str = data_dir
        .to_str()
        .ok_or_else(|| "Invalid data dir path".to_string())?
        .to_string();

    // JFW-1 DB-Migrations-Kontrakt (Spec): Tauri vergleicht den erwarteten
    // Schema-Head ueber den leichten CPU-Early-Entrypoint VOR jedem Serverstart
    // und orchestriert die Migration. Fail-closed: jeder Exit != 0 (unbekannter
    // Head, Abbruch, Kontraktverletzung) stoppt den Start — weder CPU noch CUDA
    // starten auf dieser DB. Die DB bleibt unveraendert; das Backup liegt in
    // <data_dir>/backups/. Dev-Modus: die Sidecar-Binary ist ein Platzhalter
    // (setup-dev-sidecar.js), daher laeuft die Migration dort ueber init_db()
    // im Python-Server selbst.
    #[cfg(not(debug_assertions))]
    {
        let db_path = data_dir.join("data").join("jf-whisper.db");

        // 1) Schema-Status: erwarteter Head + Migrationskettenhash (kein
        //    Schreibzugriff). Abweichung vom gebuendelten Stand = Start abgelehnt.
        //    Spec: NUR das gebuendelte CPU-Artefakt besitzt --schema-status /
        //    --migrate-only — die Operationen laufen daher immer ueber die
        //    CPU-Binary, nie ueber CUDA.
        let sidecar_exe = resolve_sidecar_path();
        let mut status_cmd = std::process::Command::new(&sidecar_exe);
        let status_output = status_cmd
            .arg("--schema-status")
            .arg(&db_path)
            .output()
            .map_err(|e| format!("Schema-Status konnte nicht gestartet werden: {}", e))?;
        if !status_output.status.success() {
            let stderr = String::from_utf8_lossy(&status_output.stderr);
            return Err(format!(
                "DB-Schema-Status fehlgeschlagen (Exit {}): {}. Start abgelehnt.",
                status_output.status.code().unwrap_or(-1),
                stderr.trim()
            ));
        }

        // 2) Migration: exklusiver Upgrade-Lauf mit WAL-Checkpoint, Backup,
        //    Batchmodus und abschliessendem Integritaetscheck (CPU-Binary).
        let mut migrate_cmd = std::process::Command::new(&sidecar_exe);
        let output = migrate_cmd
            .arg("--migrate-only")
            .arg(&db_path)
            .output()
            .map_err(|e| format!("Schema-Migration konnte nicht gestartet werden: {}", e))?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Err(format!(
                "DB-Schema-Migration fehlgeschlagen (Exit {}): {}. Start abgelehnt — \
                 die DB bleibt unverändert, Backup liegt in backups/.",
                output.status.code().unwrap_or(-1),
                stderr.trim()
            ));
        }
    }

    // JFW-1: dynamischer Loopback-Port. "0" laesst das OS einen ephemeren Port
    // waehlen; der Sidecar meldet den echten Wert im Ready-Handshake zurueck.
    let port_str = "0".to_string();
    let parent_pid_str = std::process::id().to_string();

    // Resolve the custom models directory from the parameter or stored state
    let effective_models_dir = models_dir.or_else(|| state.models_dir.lock().unwrap().clone());
    if let Some(ref dir) = effective_models_dir {
        println!("Custom models directory: {}", dir);
    }

    // JFW-12 B8: Der Sidecar wird per CreateProcessW + Job Object erzeugt —
    // suspendiert, dem Job (KILL_ON_JOB_CLOSE) zugeordnet und erst dann
    // resumed. Die Zeilen werden auf CommandEvent gemappt, damit der
    // bestehende Handshake-Loop unverändert bleibt.
    #[cfg(windows)]
    let (mut rx, sidecar_job) = {
        use backend::process_windows::{spawn_sidecar, SidecarLine};
        use tauri_plugin_shell::process::CommandEvent;

        let exe_path = cuda_binary.clone().unwrap_or_else(|| sidecar_exe.clone());
        // PyInstaller onedir erwartet DLLs/_internal relativ zum Exe → cwd = Exe-Verzeichnis.
        // Gilt fuer beide Varianten (CUDA-Build-Ordner / CPU-onedir-Ordner).
        let cwd: Option<std::path::PathBuf> = exe_path.parent().map(std::path::PathBuf::from);

        let args: Vec<String> = vec![
            "--data-dir".to_string(),
            data_dir_str,
            "--port".to_string(),
            port_str,
            "--parent-pid".to_string(),
            parent_pid_str,
        ];
        let mut extra_env: Vec<(String, String)> = vec![
            ("JFWHISPER_API_TOKEN".to_string(), api_token.clone()),
            ("JFWHISPER_GENERATION".to_string(), generation.to_string()),
        ];
        if let Some(ref dir) = effective_models_dir {
            extra_env.push(("VOICEBOX_MODELS_DIR".to_string(), dir.clone()));
        }

        match spawn_sidecar(&exe_path, &args, &extra_env, cwd.as_deref()) {
            Ok((proc, mut line_rx)) => {
                // Bridge: SidecarLine → CommandEvent (bestehender Loop bleibt identisch).
                let (cmd_tx, cmd_rx) = tokio::sync::mpsc::unbounded_channel::<CommandEvent>();
                tokio::spawn(async move {
                    while let Some(line) = line_rx.recv().await {
                        let ev = match line {
                            SidecarLine::Stdout(s) => CommandEvent::Stdout(s.into_bytes()),
                            SidecarLine::Stderr(s) => CommandEvent::Stderr(s.into_bytes()),
                        };
                        if cmd_tx.send(ev).is_err() {
                            break; // Receiver weg — Loop beendet.
                        }
                    }
                });
                (cmd_rx, proc)
            }
            Err(e) => {
                eprintln!("Failed to spawn server process: {}", e);
                supervisor.boot_failed("spawn_fehler".to_string());
                return Err(format!("Failed to spawn: {}", e));
            }
        }
    };

    #[cfg(not(windows))]
    let (mut rx, child) = {
        // Nicht-Windows-Fallback: Tauri-Shell-Spawn (Produktziel ist Windows;
        // B8-Job-Objects gelten fuer den produktiven Pfad).
        let mut sidecar = app.shell().sidecar("jf-whisper-server").map_err(|e| {
            format!("Failed to get sidecar: {}", e)
        })?;
        sidecar = sidecar.args(["--data-dir", &data_dir_str, "--port", &port_str, "--parent-pid", &parent_pid_str]);
        if let Some(ref dir) = effective_models_dir {
            sidecar = sidecar.env("VOICEBOX_MODELS_DIR", dir);
        }
        sidecar = sidecar.env("JFWHISPER_API_TOKEN", &api_token).env("JFWHISPER_GENERATION", generation.to_string());
        let spawn_result = sidecar.spawn();
        match spawn_result {
            Ok(result) => result,
            Err(e) => {
                eprintln!("Failed to spawn server process: {}", e);
                supervisor.boot_failed("spawn_fehler".to_string());
                return Err(format!("Failed to spawn: {}", e));
            }
        }
    };

    println!("Server process spawned, waiting for ready signal...");
    println!("=================================================================");

    // Store child process and PID
    #[cfg(windows)]
    let process_pid = sidecar_job.identity.pid;
    #[cfg(not(windows))]
    let process_pid = child.pid();
    *state.server_pid.lock().unwrap() = Some(process_pid);
    #[cfg(windows)]
    {
        *state.sidecar_job.lock().unwrap() = Some(sidecar_job);
    }
    #[cfg(not(windows))]
    {
        *state.child.lock().unwrap() = Some(child);
    }

    // Wait for server to be ready by listening for startup log
    // PyInstaller bundles can be slow on first import, especially torch/transformers
    let timeout = tokio::time::Duration::from_secs(120);
    let start_time = tokio::time::Instant::now();
    let mut error_output = Vec::new();

    loop {
        if start_time.elapsed() > timeout {
            eprintln!("Server startup timeout after 120 seconds");
            if !error_output.is_empty() {
                eprintln!("Collected error output:");
                for line in &error_output {
                    eprintln!("  {}", line);
                }
            }

            // JFW-1: Kein Adoption-Fallback — Timeout bleibt Fehler.
            // JFW-12 B2: Fehlerphase → NoBackendReady (sichtbar, nicht still).
            supervisor.boot_failed("boot_timeout".into());

            return Err("Server startup timeout - check Console.app for detailed logs".to_string());
        }

        match tokio::time::timeout(tokio::time::Duration::from_millis(100), rx.recv()).await {
            Ok(Some(event)) => {
                match event {
                    tauri_plugin_shell::process::CommandEvent::Stdout(line) => {
                        let line_str = String::from_utf8_lossy(&line);
                        // JFW-1: strukturierte Ready-Zeile (JSON) — Port/PID/
                        // Variante/Generation sind fuer den Caller bindend.
                        if let Ok(v) = serde_json::from_str::<serde_json::Value>(line_str.trim()) {
                            if v.get("jfwhisper_ready").and_then(|b| b.as_bool()) == Some(true) {
                                let port = v.get("port").and_then(|p| p.as_u64()).unwrap_or(0) as u16;
                                *state.sidecar_port.lock().unwrap() = Some(port);
                                // JFW-12 Bugfix: Die Variante kommt aus dem Handshake
                                // (server.py meldet "variant": cpu|cuda). Ein Boot direkt in
                                // CUDA (Current-Pointer) wurde hier hart als CPU gemeldet —
                                // die UI zeigte dann "cpu", obwohl der CUDA-Build lief.
                                let variant = match v.get("variant").and_then(|s| s.as_str()) {
                                    Some("cuda") => BackendVariant::Cuda,
                                    _ => BackendVariant::Cpu,
                                };
                                println!("Server is ready! (Handshake: port={}, generation={}, variant={:?})",
                                    port, v.get("generation").map(|g| g.to_string()).unwrap_or_default(), variant);
                                // JFW-12 B3/B8: Prozessidentität binden — PID + Erzeugungszeit
                                // + normalisierter Executable-Pfad; Lease aktiviert Admission.
                                let (identity_path, identity_build_id) = match &cuda_binary {
                                    Some(p) => (
                                        p.clone(),
                                        p.parent()
                                            .and_then(|d| d.file_name())
                                            .map(|n| n.to_string_lossy().into_owned()),
                                    ),
                                    None => (sidecar_exe.clone(), None),
                                };
                                supervisor.mark_cpu_ready(SidecarInstance::new(
                                    process_pid,
                                    identity_path.to_string_lossy().into_owned(),
                                    variant,
                                    port,
                                    identity_build_id.unwrap_or_default(),
                                ));
                                break;
                            }
                        }
                        println!("Server output: {}", line_str);
                        let _ = app.emit("server-log", serde_json::json!({
                            "stream": "stdout",
                            "line": line_str.trim_end(),
                        }));

                        if line_str.contains("Uvicorn running") || line_str.contains("Application startup complete") {
                            println!("Server is ready!");
                            break;
                        }
                    }
                    tauri_plugin_shell::process::CommandEvent::Stderr(line) => {
                        let line_str = String::from_utf8_lossy(&line).to_string();
                        eprintln!("Server: {}", line_str);
                        let _ = app.emit("server-log", serde_json::json!({
                            "stream": "stderr",
                            "line": line_str.trim_end(),
                        }));

                        // Collect error lines for debugging
                        if line_str.contains("ERROR") || line_str.contains("Error") || line_str.contains("Failed") {
                            error_output.push(line_str.clone());
                        }

                        // Uvicorn logs to stderr, so check there too.
                        // JFW-1: Fallback ohne Handshake — Port aus State (0 = unbekannt).
                        if line_str.contains("Uvicorn running") || line_str.contains("Application startup complete") {
                            println!("Server is ready! (stderr-Fallback)");
                            break;
                        }
                    }
                    _ => {}
                }
            }
            Ok(None) => {
                // In dev mode, this is expected when using the placeholder binary
                #[cfg(debug_assertions)]
                {
                    eprintln!("Server process ended (dev mode placeholder detected)");
                    // JFW-1: Kein Adoption-Fallback — der manuell gestartete Server
                    // wird nicht uebernommen; die Webview spricht ihn im Dev-Modus
                    // direkt an, Rust startet hier nichts.

                    eprintln!("");
                    eprintln!("=================================================================");
                    eprintln!("DEV MODE: No bundled server binary available");
                    eprintln!("");
                    eprintln!("Start the Python server in a separate terminal:");
                    eprintln!("  bun run dev:server");
                    eprintln!("=================================================================");
                    eprintln!("");
                    return Err("Dev mode: Start server manually with 'bun run dev:server'".to_string());
                }

                #[cfg(not(debug_assertions))]
                {
                    eprintln!("Server process ended unexpectedly during startup!");
                    eprintln!("The server binary may have crashed or exited with an error.");
                    eprintln!("Check Console.app logs for more details (search for 'voicebox')");
                    return Err("Server process ended unexpectedly".to_string());
                }
            }
            Err(_) => {
                // Timeout on this recv, continue loop
                continue;
            }
        }
    }

    // Spawn task to continue reading output and emit to frontend
    let app_handle = app.clone();
    tokio::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                tauri_plugin_shell::process::CommandEvent::Stdout(line) => {
                    let line_str = String::from_utf8_lossy(&line);
                    println!("Server: {}", line_str);
                    let _ = app_handle.emit("server-log", serde_json::json!({
                        "stream": "stdout",
                        "line": line_str.trim_end(),
                    }));
                }
                tauri_plugin_shell::process::CommandEvent::Stderr(line) => {
                    let line_str = String::from_utf8_lossy(&line);
                    eprintln!("Server error: {}", line_str);
                    let _ = app_handle.emit("server-log", serde_json::json!({
                        "stream": "stderr",
                        "line": line_str.trim_end(),
                    }));
                }
                _ => {}
            }
        }
    });

    // JFW-1: Port/Token bleiben im Rust-State (sidecar.rs liest sie fuer die
    // typisierten Kommandos). Die Webview bekommt nur den Bestaetigungs-Marker.
    Ok(())
}

#[command]
async fn stop_server(
    state: State<'_, ServerState>,
    supervisor: State<'_, Supervisor>,
) -> Result<(), String> {
    let pid = state.server_pid.lock().unwrap().take();

    // JFW-12 B8 (Windows): Das Job Object wird hier entnommen. Sein Drop am Ende
    // dieser Funktion schließt das Job → KILL_ON_JOB_CLOSE beendet den kompletten
    // Sidecar-Prozessbaum, auch wenn der HTTP-Shutdown nicht ankam.
    #[cfg(windows)]
    let job = state.sidecar_job.lock().unwrap().take();

    #[cfg(not(windows))]
    {
        let _child = state.child.lock().unwrap().take();
    }

    if let Some(pid) = pid {
        println!("stop_server: Stopping server with PID: {}", pid);

        #[cfg(unix)]
        {
            use std::process::Command;
            // Kill process group with SIGTERM first
            let _ = Command::new("kill")
                .args(["-TERM", "--", &format!("-{}", pid)])
                .output();

            // Brief wait then force kill
            std::thread::sleep(std::time::Duration::from_millis(100));

            let _ = Command::new("kill")
                .args(["-9", "--", &format!("-{}", pid)])
                .output();
            let _ = Command::new("kill")
                .args(["-9", &pid.to_string()])
                .output();

            println!("stop_server: Process group kill completed");
        }

        #[cfg(windows)]
        {
            // Zuerst graceful Shutdown per HTTP (Server kann sich sauber verabschieden),
            // danach garantiert das Job-Close unten den Baum-Kill.
            println!("Sending graceful shutdown via HTTP...");
            let client = reqwest::blocking::Client::builder()
                .timeout(std::time::Duration::from_secs(2))
                .build()
                .unwrap();

            let port = *state.sidecar_port.lock().unwrap();
            if let Some(port) = port {
                let _ = client
                    .post(&format!("http://127.0.0.1:{}/shutdown", port))
                    .send();
            }
        }

        // JFW-12 B2: Supervisor kennt den Stopp — Runtime zurück nach
        // NoBackendReady, Admission geschlossen; dieselbe App-Epoche.
        supervisor.stopped();
    }

    // JFW-12 B8 (Windows): Job-Close am Ende der Funktion → kompletter
    // Prozessbaum wird beendet (KILL_ON_JOB_CLOSE). `job` wird hier gedroppt.
    #[cfg(windows)]
    {
        if job.is_some() {
            println!("stop_server: Closing sidecar job object (process tree kill)");
        }
    }

    Ok(())
}

// ── JFW-12 Block (g): Echte Switch-Prozessschritte (B5/B6) ────────────────
// Der Supervisor-Actor orchestriert die Sequenz über `switch_driver::run_switch`;
// diese Factory liefert die echten, blocking Schritte und läuft im Switch-
// Blocking-Task. Fail-closed: jeder Schrittfehler ist ein `Err` → der Actor
// setzt NoBackendReady (sichtbar) und schließt das Journal mit dem Grund.

/// Datenroot dieser App-Instanz (%LOCALAPPDATA%\JFWhisper).
fn jfwhisper_data_root() -> std::path::PathBuf {
    std::env::var("LOCALAPPDATA")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| std::path::PathBuf::from("."))
        .join("JFWhisper")
}

/// Aktive Tasks zählen (`/tasks/active`): Downloads + Generationen.
fn parse_active_task_count(v: &serde_json::Value) -> Result<usize, String> {
    let dl = v.get("downloads").and_then(|d| d.as_array()).map(|a| a.len()).unwrap_or(0);
    let gen = v.get("generations").and_then(|g| g.as_array()).map(|a| a.len()).unwrap_or(0);
    Ok(dl + gen)
}

/// Deterministischer Silence-WAV für den Readiness-Smoke (B5.5): 16 kHz, mono,
/// 16-bit PCM, 0,5 s Stille — inhaltsfrei und nicht persistierend. Hashgebunden:
/// Der erwartete SHA-256 wird im Unit-Test verifiziert, damit ein versehentlich
/// geänderter Smoke sofort auffällt (Spec: „fest eingebauter, hashgebundener
/// Silence-WAV").
fn silence_wav_bytes() -> Vec<u8> {
    const RATE: u32 = 16_000;
    let data_len: usize = (RATE / 2) as usize * 2; // 0,5 s × 16-bit mono
    let mut buf: Vec<u8> = Vec::with_capacity(44 + data_len);
    buf.extend_from_slice(b"RIFF");
    buf.extend_from_slice(&(36u32 + data_len as u32).to_le_bytes());
    buf.extend_from_slice(b"WAVE");
    buf.extend_from_slice(b"fmt ");
    buf.extend_from_slice(&16u32.to_le_bytes()); // fmt-Chunkgröße
    buf.extend_from_slice(&1u16.to_le_bytes());  // PCM
    buf.extend_from_slice(&1u16.to_le_bytes()); // mono
    buf.extend_from_slice(&RATE.to_le_bytes());
    buf.extend_from_slice(&(RATE * 2).to_le_bytes()); // Byte-Rate
    buf.extend_from_slice(&2u16.to_le_bytes());      // Block-Align
    buf.extend_from_slice(&16u16.to_le_bytes());    // Bits pro Sample
    buf.extend_from_slice(b"data");
    buf.extend_from_slice(&(data_len as u32).to_le_bytes());
    buf.resize(44 + data_len, 0); // Stille (Null-PCM)
    buf
}

/// JFW-12 Block (g): Echte Prozessschritte für den Supervisor-Switch.
struct MainSwitchSteps {
    app: tauri::AppHandle,
}

impl backend::switch_driver::SwitchStepFactory for MainSwitchSteps {
    /// Schritt 1 (B5.2/B6.1): Aktive Jobs drainieren, bis null aktiv.
    fn drain(&self, _direction: backend::switch_evidence::SwitchDirection) -> Result<(), String> {
        let state = self.app.state::<ServerState>();
        let port = *state.sidecar_port.lock().unwrap();
        let token = state.api_token.lock().unwrap().clone();
        let generation = state.generation.lock().unwrap().to_string();
        let (port, token) = match (port, token) {
            (Some(p), Some(t)) => (p, t),
            _ => return Err("kein aktives Sidecar zum Drainen".into()),
        };
        let client = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(2))
            .build()
            .map_err(|e| format!("Drain-Client: {e}"))?;
        // AC-D: Laufende Aufträge dürfen auf ihrer Generation kontrolliert fertig-
        // laufen — kein automatischer Abbruch, auch wenn sie länger als das Idle-
        // Budget brauchen (Drainzeit wird separat gemessen, Spec B5/B6). Deshalb
        // bewusst KEIN hartes Timeout hier: wir warten, bis null aktive Tasks bleiben.
        loop {
            let resp = client
                .get(format!("http://127.0.0.1:{port}/tasks/active"))
                .header("Authorization", format!("Bearer {token}"))
                .header("x-jfwhisper-generation", &generation)
                .send()
                .map_err(|e| format!("Drain-Abfrage fehlgeschlagen: {e}"))?;
            if !resp.status().is_success() {
                return Err(format!("Drain-Abfrage HTTP {}", resp.status()));
            }
            let v: serde_json::Value = resp.json().map_err(|e| format!("Drain-Payload: {e}"))?;
            let active = parse_active_task_count(&v)?;
            if active == 0 {
                return Ok(());
            }
            std::thread::sleep(std::time::Duration::from_millis(250));
        }
    }

    /// Schritt 2 (B5/B6): Ausgangs-Backend graceful beenden + Prozessbaum schließen.
    fn teardown_source(&self, _direction: backend::switch_evidence::SwitchDirection) -> Result<(), String> {
        let state = self.app.state::<ServerState>();
        let pid = *state.server_pid.lock().unwrap();

        // JFW-12 B8 (Windows): Job entnommen — sein Drop am Ende der Funktion
        // schließt das Job → KILL_ON_JOB_CLOSE beendet den kompletten Baum.
        #[cfg(windows)]
        let _job = state.sidecar_job.lock().unwrap().take();
        #[cfg(not(windows))]
        {
            let _child = state.child.lock().unwrap().take();
        }

        if let Some(pid) = pid {
            // Zuerst graceful Shutdown per HTTP (interner Pfad, token-frei).
            if let Some(port) = *state.sidecar_port.lock().unwrap() {
                let client = reqwest::blocking::Client::builder()
                    .timeout(std::time::Duration::from_secs(2))
                    .build()
                    .map_err(|e| format!("Teardown-Client: {e}"))?;
                let _ = client.post(format!("http://127.0.0.1:{port}/shutdown")).send();
            }

            // Auf Exit warten (max. 5 s); bleibt der Prozess, tötet das Job-Close.
            #[cfg(windows)]
            {
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
                while is_process_alive(pid) && std::time::Instant::now() < deadline {
                    std::thread::sleep(std::time::Duration::from_millis(100));
                }
                if is_process_alive(pid) {
                    eprintln!("teardown_source: PID {pid} überlebt graceful Shutdown — Job-Close tötet den Baum");
                }
            }
            #[cfg(not(windows))]
            {
                use std::process::Command;
                let _ = Command::new("kill").args(["-TERM", "--", &format!("-{pid}")]).output();
                std::thread::sleep(std::time::Duration::from_millis(100));
                let _ = Command::new("kill").args(["-9", "--", &format!("-{pid}")]).output();
            }

            *state.server_pid.lock().unwrap() = None;
        }
        // `job` wird hier gedroppt → KILL_ON_JOB_CLOSE (Windows).
        Ok(())
    }

    /// Schritt 3 (B5.4/B6.2): Ziel-Backend starten + Ready-Handshake abwarten.
    fn start_target(&self, direction: backend::switch_evidence::SwitchDirection) -> Result<SidecarInstance, String> {
        let variant = match direction {
            backend::switch_evidence::SwitchDirection::CpuToCuda => BackendVariant::Cuda,
            backend::switch_evidence::SwitchDirection::CudaToCpu => BackendVariant::Cpu,
        };

        // 1) Ziel-Binary auflösen. CPU: gebündelte Sidecar (identisch zum Boot).
        //    CUDA: installierter Build über den Current-Pointer (B9).
        let data_dir = jfwhisper_data_root();
        let (exe_path, cwd): (std::path::PathBuf, Option<std::path::PathBuf>) = match variant {
            BackendVariant::Cpu => {
                // JFW-12 P3: onedir erwartet cwd = Exe-Verzeichnis, damit _internal/
                // gefunden wird. Für das onefile-Fallback ist cwd harmlos (Self-Unpack).
                let exe = resolve_sidecar_path();
                let cwd = exe.parent().map(|p| p.to_path_buf());
                (exe, cwd)
            }
            BackendVariant::Cuda => {
                #[cfg(not(windows))]
                return Err("CUDA-Switch erfordert Windows (B8: Job-Objects)".into());
                #[cfg(windows)]
                {
                    let cuda_root = data_dir.join("backends").join("cuda");
                    let current_raw = std::fs::read_to_string(cuda_root.join("current.json"))
                        .map_err(|e| format!("CUDA-Artefakt nicht installiert (current.json: {e})"))?;
                    let current: backend::artifact::CurrentPointer =
                        serde_json::from_str(&current_raw).map_err(|e| format!("current.json ungültig: {e}"))?;
                    let dir = cuda_root.join(&current.build_id);
                    (dir.join("jf-whisper-server-cuda.exe"), Some(dir))
                }
            }
        };
        if !exe_path.exists() {
            return Err(format!("Ziel-Binary fehlt: {}", exe_path.display()));
        }

        // 2) Neues Bearer-Token + Generation (JFW-1: pro Start, nur per Umgebung).
        let state = self.app.state::<ServerState>();
        use rand::Rng;
        let api_token: String = {
            let mut rng = rand::thread_rng();
            const ALPHABET: [char; 16] = ['a','b','c','d','e','f','g','h','i','j','k','l','m','n','o','p'];
            (0..32).map(|_| ALPHABET[rng.gen_range(0usize..16)]).collect()
        };
        *state.api_token.lock().unwrap() = Some(api_token.clone());
        let generation = {
            let mut g = state.generation.lock().unwrap();
            *g += 1;
            *g
        };

        // 3) DB-Migrations-Kontrakt (Release, CPU-Binary — identisch zum Boot).
        //    Innerhalb einer App-Epoche ist die DB unverändert; der Check bleibt
        //    fail-closed und schützt vor einem fremden Schema-Head.
        #[cfg(not(debug_assertions))]
        {
            let db_path = data_dir.join("data").join("jf-whisper.db");
            let cpu_exe = resolve_sidecar_path();
            let status_output = std::process::Command::new(&cpu_exe)
                .arg("--schema-status")
                .arg(&db_path)
                .output()
                .map_err(|e| format!("Schema-Status konnte nicht gestartet werden: {e}"))?;
            if !status_output.status.success() {
                let stderr = String::from_utf8_lossy(&status_output.stderr);
                return Err(format!(
                    "DB-Schema-Status fehlgeschlagen (Exit {}): {}. Switch abgelehnt.",
                    status_output.status.code().unwrap_or(-1),
                    stderr.trim()
                ));
            }
            let migrate_output = std::process::Command::new(&cpu_exe)
                .arg("--migrate-only")
                .arg(&db_path)
                .output()
                .map_err(|e| format!("Schema-Migration konnte nicht gestartet werden: {e}"))?;
            if !migrate_output.status.success() {
                let stderr = String::from_utf8_lossy(&migrate_output.stderr);
                return Err(format!(
                    "DB-Schema-Migration fehlgeschlagen (Exit {}): {}. Switch abgelehnt — \
                     die DB bleibt unverändert, Backup liegt in backups/.",
                    migrate_output.status.code().unwrap_or(-1),
                    stderr.trim()
                ));
            }
        }

        // 4) Spawn: Windows → CreateProcessW + Job Object (B8); sonst Tauri-Shell.
        let data_dir_str = data_dir.to_string_lossy().into_owned();
        let port_str = "0".to_string();
        let parent_pid_str = std::process::id().to_string();
        let extra_env: Vec<(String, String)> = vec![
            ("JFWHISPER_API_TOKEN".to_string(), api_token),
            ("JFWHISPER_GENERATION".to_string(), generation.to_string()),
        ];

        #[cfg(windows)]
        {
            use backend::process_windows::{spawn_sidecar, SidecarLine};
            let args: Vec<String> = vec![
                "--data-dir".into(), data_dir_str.clone(),
                "--port".into(), port_str.clone(),
                "--parent-pid".into(), parent_pid_str.clone(),
            ];
            let (proc, mut line_rx): (backend::process_windows::SidecarJob, tokio::sync::mpsc::Receiver<backend::process_windows::SidecarLine>) =
                spawn_sidecar(&exe_path, &args, &extra_env, cwd.as_deref())
                    .map_err(|e| format!("Ziel-Spawn fehlgeschlagen: {e}"))?;
            let pid = proc.identity.pid;

            // Bridge: SidecarLine → std-Channel (blocking recv mit Timeout).
            let (std_tx, std_rx) = std::sync::mpsc::channel::<Result<String, String>>();
            tauri::async_runtime::spawn(async move {
                while let Some(line) = line_rx.recv().await {
                    let s = match line {
                        SidecarLine::Stdout(s) => Ok(s),
                        SidecarLine::Stderr(s) => Err(s),
                    };
                    if std_tx.send(s).is_err() {
                        break;
                    }
                }
            });

            // 5) Ready-Handshake (JFW-1: strukturierte JSON-Zeile, Port bindend).
            let instance = wait_sidecar_ready(&std_rx, pid, variant, &exe_path)?;
            *state.server_pid.lock().unwrap() = Some(pid);
            *state.sidecar_job.lock().unwrap() = Some(proc);
            *state.sidecar_port.lock().unwrap() = Some(instance.port);
            Ok(instance)
        }

        #[cfg(not(windows))]
        {
            use tauri_plugin_shell::process::CommandEvent;
            let (mut rx, child) = {
                let mut sidecar = self.app.shell().sidecar("jf-whisper-server")
                    .map_err(|e| format!("Failed to get sidecar: {e}"))?;
                sidecar = sidecar.args(["--data-dir", &data_dir_str, "--port", &port_str, "--parent-pid", &parent_pid_str]);
                for (k, v) in &extra_env {
                    sidecar = sidecar.env(k.as_str(), v.as_str());
                }
                match sidecar.spawn() {
                    Ok(result) => result,
                    Err(e) => return Err(format!("Ziel-Spawn fehlgeschlagen: {e}")),
                }
            };
            let pid = child.pid();

            // Bridge: CommandEvent → std-Channel (blocking recv mit Timeout).
            let (std_tx, std_rx) = std::sync::mpsc::channel::<Result<String, String>>();
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    let s = match event {
                        CommandEvent::Stdout(b) => Ok(String::from_utf8_lossy(&b).into_owned()),
                        CommandEvent::Stderr(b) => Err(String::from_utf8_lossy(&b).into_owned()),
                        _ => continue,
                    };
                    if std_tx.send(s).is_err() {
                        break;
                    }
                }
            });

            let instance = wait_sidecar_ready(&std_rx, pid, variant, &exe_path)?;
            *state.server_pid.lock().unwrap() = Some(pid);
            *state.child.lock().unwrap() = Some(child);
            *state.sidecar_port.lock().unwrap() = Some(instance.port);
            Ok(instance)
        }
    }

    /// Schritt 4 (B5.5/B6.2): Echte Inferenz-Smoke — kein bloßer Port-/Prozesscheck.
    ///
    /// Der Sidecar muss exakt das gewählte Modell geladen haben und auf einem
    /// fest eingebauten, hashgebundenen Silence-WAV einen nicht persistierenden
    /// Inferenz-Smoke ausführen (Spec B5.5/B6.2, Decision Log 2026-09-09).
    /// `/transcribe` persistiert nichts (verifiziert: keine DB-Mutation) und der
    /// Smoke ist inhaltsfrei (reine Stille), daher bleibt die Inhaltsfreiheit (C).
    fn readiness_smoke(&self, instance: &SidecarInstance) -> Result<(), String> {
        let state = self.app.state::<ServerState>();
        let token = state.api_token.lock().unwrap().clone();
        let generation = state.generation.lock().unwrap().to_string();
        let (token, generation) = match token {
            Some(t) => (t, generation),
            None => return Err("kein API-Token für Readiness-Smoke".into()),
        };

        // 1) Health: Server antwortet + Modell ist geladen (nicht nur healthy).
        let client = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(5))
            .build()
            .map_err(|e| format!("Readiness-Client: {e}"))?;
        let resp = client
            .get(format!("http://127.0.0.1:{}/health", instance.port))
            .send()
            .map_err(|e| format!("Health-Abfrage fehlgeschlagen: {e}"))?;
        if !resp.status().is_success() {
            return Err(format!("Health HTTP {}", resp.status()));
        }
        let v: serde_json::Value = resp.json().map_err(|e| format!("Health-Payload: {e}"))?;
        match v.get("status").and_then(|s| s.as_str()) {
            Some("healthy") => {}
            other => return Err(format!("Health nicht healthy: {other:?}")),
        }

        // 2) Inferenz-Smoke (B5.5): Silence-WAV durch den echten Transkriptionspfad.
        //    Lädt das Modell bei Bedarf und beweist Gerät + Inferenz ohne Nutzeraufnahme.
        let wav = silence_wav_bytes();
        let form = reqwest::blocking::multipart::Form::new()
            .part(
                "file",
                reqwest::blocking::multipart::Part::bytes(wav).file_name("smoke.wav"),
            );
        // Modell-Laden kann dauern (Cache-Read + Device-Move) — großzügiges Budget.
        let smoke_client = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(120))
            .build()
            .map_err(|e| format!("Smoke-Client: {e}"))?;
        let resp = smoke_client
            .post(format!("http://127.0.0.1:{}/transcribe", instance.port))
            .header("Authorization", format!("Bearer {token}"))
            .header("x-jfwhisper-generation", &generation)
            .multipart(form)
            .send()
            .map_err(|e| format!("Inferenz-Smoke fehlgeschlagen: {e}"))?;
        let status = resp.status();
        if !status.is_success() {
            return Err(format!("Inferenz-Smoke HTTP {status}"));
        }

        // 3) Modellvertrag (B5.6): geladenes Modell + Zielvariante bestätigt.
        let health2 = client
            .get(format!("http://127.0.0.1:{}/health", instance.port))
            .send()
            .map_err(|e| format!("Health-Abfrage (nach Smoke) fehlgeschlagen: {e}"))?;
        let v2: serde_json::Value = health2.json().map_err(|e| format!("Health-Payload: {e}"))?;
        if !v2.get("model_loaded").and_then(|b| b.as_bool()).unwrap_or(false) {
            return Err("Modell nach Smoke nicht geladen".into());
        }
        let expected = instance.variant.as_str();
        match v2.get("backend_variant").and_then(|s| s.as_str()) {
            Some(bv) if bv == expected => Ok(()),
            other => Err(format!("Variant-Vertrag verletzt: erwartet {expected}, gemeldet {other:?}")),
        }
    }

    /// Schritt 5 (B6.6–7, nur CudaToCpu): VRAM-Nachweis über die getestete
    /// [`GpuReceipt`]-API — der alte CUDA-PID darf in zwei aufeinanderfolgenden
    /// NVML-Proben (≥1 s Abstand) nicht mehr als Compute-Prozess geführt werden.
    /// Erst nach Abschluss des Receipts ist `0 MiB zurechenbar` abgeleitet.
    fn vram_evidence(&self, old_cuda_pid: Option<u32>) -> Result<(), String> {
        let pid = old_cuda_pid.ok_or("VRAM-Evidence unmöglich: kein bekannter CUDA-PID")?;

        // Ausgangs-Probe **nach** dem CUDA-Teardown (B6.4–5): der alte PID muss
        // dann schon weg sein, sonst ist die Freigabe rot (fail-closed).
        let probe = backend::gpu_evidence::GpuContextProbe::capture()
            .map_err(|e| format!("NVML-Probe fehlgeschlagen: {e}"))?;
        if probe.contains_pid(pid) {
            return Err(format!(
                "CUDA-Prozess (PID {pid}) nach Teardown noch aktiv — VRAM nicht freigegeben"
            ));
        }

        // Nur der jf-whisper-CUDA-PID zählt als aufgezeichnet (B6); fremde
        // Compute-Prozesse dürfen die Freigabe nicht rot machen. Zwei grüne
        // Proben mit ≥1 s Abstand, dann 0-MiB-Ableitung (B6.7).
        let mut receipt = backend::gpu_evidence::GpuReceipt::for_pids([pid]);
        for attempt in 0..2u32 {
            if attempt == 1 {
                std::thread::sleep(std::time::Duration::from_secs(1));
            }
            let p = backend::gpu_evidence::GpuContextProbe::capture()
                .map_err(|e| format!("NVML-Wiederholprobe fehlgeschlagen: {e}"))?;
            match receipt.verify(&p, std::time::Duration::from_secs(1)) {
                Ok(backend::gpu_evidence::ReleaseVerification::Green) => {}
                Ok(backend::gpu_evidence::ReleaseVerification::Red(pids)) => {
                    return Err(format!(
                        "CUDA-Compute-PIDs noch aktiv: {pids:?} — VRAM nicht freigegeben"
                    ));
                }
                Err(e) => return Err(format!("VRAM-Probe fehlgeschlagen: {e}")),
            }
        }
        let attributable = receipt
            .attributable_mib()
            .map_err(|e| format!("0-MiB-Ableitung verweigert: {e}"))?;
        debug_assert_eq!(attributable, 0);
        Ok(())
    }

    /// Fail-closed nach Zielstart (B5.8/B6.8): frisch gestartetes Ziel beenden.
    fn teardown_target(&self, instance: &SidecarInstance) {
        let state = self.app.state::<ServerState>();
        if *state.server_pid.lock().unwrap() != Some(instance.pid) {
            eprintln!("teardown_target: PID {} ist nicht das aktuelle Sidecar — übersprungen", instance.pid);
            return;
        }
        // Graceful per HTTP, dann Job-Close (Drop am Ende der Funktion).
        #[cfg(windows)]
        let _job = state.sidecar_job.lock().unwrap().take();
        #[cfg(not(windows))]
        {
            let _child = state.child.lock().unwrap().take();
        }
        if let Ok(client) = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(2))
            .build()
        {
            let _ = client.post(format!("http://127.0.0.1:{}/shutdown", instance.port)).send();
        }
        #[cfg(windows)]
        {
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
            while is_process_alive(instance.pid) && std::time::Instant::now() < deadline {
                std::thread::sleep(std::time::Duration::from_millis(100));
            }
        }
        *state.server_pid.lock().unwrap() = None;
        // `job` wird hier gedroppt → KILL_ON_JOB_CLOSE (Windows).
    }
}

/// Ready-Handshake abwarten: bis 120 s auf die strukturierte JSON-Zeile
/// (`jfwhisper_ready`) warten; Port/PID/Variante sind für den Caller bindend.
fn wait_sidecar_ready(
    rx: &std::sync::mpsc::Receiver<Result<String, String>>,
    pid: u32,
    variant: BackendVariant,
    exe_path: &std::path::Path,
) -> Result<SidecarInstance, String> {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(120);
    loop {
        if std::time::Instant::now() > deadline {
            return Err("Ziel-Startup-Timeout nach 120 s".into());
        }
        match rx.recv_timeout(std::time::Duration::from_millis(100)) {
            Ok(Ok(line)) => {
                let line_str = line;
                if let Ok(v) = serde_json::from_str::<serde_json::Value>(line_str.trim()) {
                    if v.get("jfwhisper_ready").and_then(|b| b.as_bool()) == Some(true) {
                        let port = v.get("port").and_then(|p| p.as_u64()).unwrap_or(0) as u16;
                        return Ok(SidecarInstance::new(
                            pid,
                            exe_path.to_string_lossy().into_owned(),
                            variant,
                            port,
                            String::new(), // Build-ID fehlt im Handshake (wie Boot-Pfad).
                        ));
                    }
                }
            }
            Ok(Err(err_line)) => {
                eprintln!("Ziel-Sidecar stderr: {}", err_line);
            }
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => continue,
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                return Err("Ziel-Prozess endete unerwartet während des Startups".into());
            }
        }
    }
}

// ── JFW-12 B2/D: Typisierte Supervisor-Commands (Surface) ────────────────
// Der Webview spricht den Sidecar nie direkt an; alle Zustandsfragen laufen
// ueber diese Commands. Events kommen ueber `backend:supervisor`.

/// Aktueller typisierter Snapshot des BackendSupervisors (C: nur RAM).
#[command]
fn supervisor_snapshot(state: State<'_, Supervisor>) -> backend::state::SupervisorSnapshot {
    state.snapshot()
}

/// Admission-Entscheidung fuer einen neuen Job (B5 Schritt 1): das Frontend
/// fragt vor produktiven Requests hier an und bekommt die bindende Generation.
#[command]
fn supervisor_admit(
    state: State<'_, Supervisor>,
    requested_generation: Option<u64>,
) -> AdmissionOutcome {
    state.admit(requested_generation)
}

/// Nutzerinitiiertes Backend-Switch-Request von der GPU-Seite (B9).
/// `target` ist "cpu" oder "cuda"; CUDA ist fail-closed, bis Block (d) das
/// signierte Artefakt installiert hat.
#[command]
fn request_backend_switch(
    state: State<'_, Supervisor>,
    target: String,
) -> Result<(), String> {
    let variant = match target.as_str() {
        "cpu" => BackendVariant::Cpu,
        "cuda" => BackendVariant::Cuda,
        other => return Err(format!("unbekannte Variante: {other}")),
    };
    state.request_switch(variant);
    Ok(())
}

// ── JFW-12 Block (d): CUDA-Addon-Lifecycle, ausschließlich nutzerinitiiert ──

/// Nutzerstart: signiertes CUDA-Addon aus dem eingebetteten Releasepfad
/// installieren (B9). Die UI akzeptiert keine freie URL.
#[command]
fn install_cuda_addon(state: State<'_, Supervisor>) -> Result<(), String> {
    state.install_addon();
    Ok(())
}

/// Nutzerstart: defektes Build neu installieren (dieselbe Pipeline wie Install).
#[command]
fn repair_cuda_addon(state: State<'_, Supervisor>) -> Result<(), String> {
    state.repair_addon();
    Ok(())
}

/// Nutzerstart: installierte Builds + Current-Pointer entfernen.
#[command]
fn remove_cuda_addon(state: State<'_, Supervisor>) -> Result<(), String> {
    state.remove_addon();
    Ok(())
}

#[command]
async fn restart_server(
    app: tauri::AppHandle,
    state: State<'_, ServerState>,
    supervisor: State<'_, Supervisor>,
    models_dir: Option<String>,
) -> Result<(), String> {
    println!("restart_server: stopping current server...");

    // Update stored models_dir: empty string means reset to default, non-empty means set
    if let Some(ref dir) = models_dir {
        if dir.is_empty() {
            *state.models_dir.lock().unwrap() = None;
        } else {
            *state.models_dir.lock().unwrap() = Some(dir.clone());
        }
    }

    // Stop the current server
    stop_server(state.clone(), supervisor.clone()).await?;

    // Wait for port to be released
    println!("restart_server: waiting for port release...");
    tokio::time::sleep(tokio::time::Duration::from_millis(1000)).await;

    // Start server again (will auto-detect CUDA binary and use stored models_dir)
    println!("restart_server: starting server...");
    start_server(app, state, supervisor, None).await
}

#[command]
async fn start_system_audio_capture(
    state: State<'_, audio_capture::AudioCaptureState>,
    max_duration_secs: u32,
) -> Result<(), String> {
    audio_capture::start_capture(&state, max_duration_secs).await
}

#[command]
async fn stop_system_audio_capture(
    state: State<'_, audio_capture::AudioCaptureState>,
) -> Result<String, String> {
    audio_capture::stop_capture(&state).await
}

#[command]
fn is_system_audio_supported() -> bool {
    audio_capture::is_supported()
}

#[command]
fn list_audio_output_devices(
    state: State<'_, audio_output::AudioOutputState>,
) -> Result<Vec<audio_output::AudioOutputDevice>, String> {
    state.list_output_devices()
}

#[command]
async fn play_audio_to_devices(
    state: State<'_, audio_output::AudioOutputState>,
    audio_data: Vec<u8>,
    device_ids: Vec<String>,
) -> Result<(), String> {
    state.play_audio_to_devices(audio_data, device_ids).await
}

#[command]
fn stop_audio_playback(
    state: State<'_, audio_output::AudioOutputState>,
) -> Result<(), String> {
    state.stop_all_playback()
}

/// Identifier of the Voicebox app itself — used to short-circuit auto-paste
/// when the user fires a chord while focus was inside one of our own
/// windows. Paste into Voicebox-internal targets is step 6 territory and
/// goes through a different (JS-side) injection path.
///
/// Value matches what `focus_capture::capture_focus` writes into
/// `FocusSnapshot::bundle_id` on the current platform — reverse-DNS bundle
/// id on macOS, lowercased exe basename on Windows/Linux.
#[cfg(target_os = "macos")]
const VOICEBOX_BUNDLE_ID: &str = "sh.voicebox.app";
#[cfg(target_os = "windows")]
const VOICEBOX_BUNDLE_ID: &str = "voicebox.exe";
#[cfg(not(any(target_os = "macos", target_os = "windows")))]
const VOICEBOX_BUNDLE_ID: &str = "voicebox";

/// Milliseconds to wait between activating the target app and firing the
/// synthetic ⌘V, giving AppKit time to finish re-ordering windows and
/// restoring its last-focused field.
const POST_ACTIVATE_SETTLE_MS: u64 = 120;

/// Milliseconds the staged text lives on the clipboard after the paste
/// keystroke, before we restore the user's original clipboard contents.
/// Too short and slow apps haven't consumed the paste yet; too long and
/// the user sees our text if they look at their clipboard manager.
const PASTE_CONSUME_MS: u64 = 400;

/// Reports whether the process currently has macOS Accessibility trust.
/// Used by the settings UI and the paste debug harness to decide whether
/// synthetic key events will actually land.
#[command]
fn check_accessibility_permission() -> bool {
    accessibility::is_trusted()
}

/// Reports whether the process can observe global keyboard events. Read by
/// the Captures settings UI to surface a "missing — open Settings" hint
/// beside the hotkey toggle. No prompt side-effect.
#[command]
fn check_input_monitoring_permission() -> bool {
    input_monitoring::is_trusted()
}

/// Holds the lazily-spawned global hotkey monitor. The monitor is `None`
/// until the user opts in via the Captures settings toggle — that opt-in is
/// what triggers the macOS Input Monitoring TCC prompt, so a fresh-install
/// user who never enables the hotkey never sees the prompt.
///
/// Disabling the hotkey clears the monitor's internal `ChordMatcher` so
/// keytap's event tap is released while Tauri still owns this `HotkeyState`
/// for the rest of the process. A subsequent enable re-arms without
/// re-prompting for the Input Monitoring permission.
#[cfg(desktop)]
#[derive(Default)]
pub struct HotkeyState {
    monitor: Mutex<Option<hotkey_monitor::HotkeyMonitor>>,
}

#[cfg(desktop)]
fn build_chord_bindings(
    push_to_talk: &[String],
    toggle_to_talk: &[String],
) -> Result<hotkey_monitor::Bindings, String> {
    use hotkey_monitor::{Bindings, ChordAction};
    use keytap::Key;
    use std::collections::HashSet;

    fn build_chord(name: &str, names: &[String]) -> Result<HashSet<Key>, String> {
        if names.is_empty() {
            return Err(format!("{name} chord must have at least one key"));
        }
        let mut chord = HashSet::new();
        for raw in names {
            let key = key_codes::key_from_str(raw)
                .ok_or_else(|| format!("Unsupported key in {name} chord: {raw}"))?;
            chord.insert(key);
        }
        Ok(chord)
    }

    let push_chord = build_chord("push-to-talk", push_to_talk)?;
    let toggle_chord = build_chord("toggle-to-talk", toggle_to_talk)?;

    let mut bindings = Bindings::new();
    bindings.insert(ChordAction::PushToTalk, push_chord);
    bindings.insert(ChordAction::ToggleToTalk, toggle_chord);
    Ok(bindings)
}

/// Spawn the global hotkey monitor on first call; subsequent calls just push
/// the new bindings into the existing monitor. Idempotent on purpose — the
/// frontend invokes this both at startup (when `capture_settings.hotkey_enabled`
/// is true) and from the settings toggle.
///
/// On macOS this is the call that triggers the "Voicebox would like to receive
/// keystrokes from any application" TCC prompt, since keytap's `Tap` creates
/// the CGEventTap inside `HotkeyMonitor::spawn`.
#[cfg(desktop)]
#[command]
fn enable_hotkey(
    app: tauri::AppHandle,
    state: State<'_, HotkeyState>,
    push_to_talk: Vec<String>,
    toggle_to_talk: Vec<String>,
) -> Result<(), String> {
    let bindings = build_chord_bindings(&push_to_talk, &toggle_to_talk)?;

    // Fire the Input Monitoring TCC prompt explicitly from the user's
    // toggle click, before keytap's Tap would do it implicitly via
    // CGEventTap creation. Two reasons: (1) the prompt timing becomes
    // deterministic — it appears in response to a click instead of as a
    // mysterious side-effect of "the app started"; (2) on subsequent
    // launches we can short-circuit the spawn entirely if the user
    // revoked the grant, instead of relying on the tap silently failing.
    // The call returns the current grant state; we ignore it because
    // keytap surfaces its own error via stderr, and the settings UI
    // polls `check_input_monitoring_permission` separately.
    let _ = input_monitoring::request();

    // The dictate pill webview must exist before the first chord fires so it
    // can subscribe to `dictate:start`. Build it here (idempotent — Tauri
    // returns the existing window when one with this label already exists).
    if app.get_webview_window(DICTATE_WINDOW_LABEL).is_none() {
        if let Err(e) = build_dictate_window(&app) {
            eprintln!("Failed to build dictate window: {}", e);
        }
    }

    let mut slot = state.monitor.lock().map_err(|e| e.to_string())?;
    match slot.as_mut() {
        Some(monitor) => monitor.update_bindings(bindings),
        None => {
            *slot = Some(hotkey_monitor::HotkeyMonitor::spawn(app, bindings));
        }
    }
    Ok(())
}

/// Quiet the global hotkey. Tears down the `ChordMatcher` (which stops
/// keytap's chord worker and closes the OS event tap) but keeps the
/// `HotkeyMonitor` handle around so a subsequent `enable_hotkey` re-arms
/// without re-prompting for Input Monitoring permission.
#[cfg(desktop)]
#[command]
fn disable_hotkey(state: State<'_, HotkeyState>) -> Result<(), String> {
    let mut slot = state.monitor.lock().map_err(|e| e.to_string())?;
    if let Some(monitor) = slot.as_mut() {
        monitor.update_bindings(hotkey_monitor::Bindings::new());
    }
    Ok(())
}

/// Push a new chord configuration into the running `HotkeyMonitor`. Called
/// by the chord-picker UI when the user edits the chord. No-ops when the
/// monitor isn't spawned — the picker is gated behind the enable toggle, so
/// this can only happen if the frontend races; the next `enable_hotkey` will
/// pick up the saved chords.
///
/// Returns an error when a key name doesn't map to a `keytap::Key`, so the
/// picker UI can surface "this key isn't supported" instead of silently
/// dropping it from the chord.
#[cfg(desktop)]
#[command]
fn update_chord_bindings(
    state: State<'_, HotkeyState>,
    push_to_talk: Vec<String>,
    toggle_to_talk: Vec<String>,
) -> Result<(), String> {
    let bindings = build_chord_bindings(&push_to_talk, &toggle_to_talk)?;
    let mut slot = state.monitor.lock().map_err(|e| e.to_string())?;
    if let Some(monitor) = slot.as_mut() {
        monitor.update_bindings(bindings);
    }
    Ok(())
}

/// Open the Privacy & Security → Accessibility pane in System Settings so
/// the user can grant the permission. The URL scheme is stable across
/// macOS 10.14–15; no-op on other platforms.
#[command]
fn open_accessibility_settings(app: tauri::AppHandle) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        let url = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility";
        app.shell()
            .open(url, None)
            .map_err(|e| format!("Failed to open Accessibility settings: {e}"))?;
        Ok(())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = app;
        Err("Accessibility settings pane is only implemented on macOS".into())
    }
}

/// Open the Privacy & Security → Input Monitoring pane in System Settings.
/// Used by the Captures settings UI when the toggle is on but the grant
/// is missing, so the user can flip the system toggle without hunting.
#[command]
fn open_input_monitoring_settings(app: tauri::AppHandle) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        let url = "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent";
        app.shell()
            .open(url, None)
            .map_err(|e| format!("Failed to open Input Monitoring settings: {e}"))?;
        Ok(())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = app;
        Err("Input Monitoring settings pane is only implemented on macOS".into())
    }
}

/// Deliver `text` into the UI that had focus when the chord fired.
///
/// Pipeline: activate the captured PID → settle → save the user's
/// clipboard → write `text` → fire ⌘V → wait for the target to consume it
/// → conditionally restore the original clipboard.
///
/// The restore is conditional on `NSPasteboard.changeCount` (or the
/// Windows sequence number) matching the value captured right after
/// `write_text`: if something else wrote to the clipboard during the
/// paste-consume window — the user's own ⌘C in the target app, a
/// clipboard history tool (Paste, Pastebot, Maccy), Universal Clipboard
/// sync, 1Password inserting a secret — their newer content takes
/// priority over our snapshot and is preserved. A
/// [`clipboard::current_change_count`] read failure is treated the same
/// way: unknown state is safer than an unconditional overwrite.
///
/// `send_paste` failure is isolated from the restore decision: we always
/// attempt the conditional restore before propagating the paste error,
/// so a failed `CGEventPost` / `SendInput` never leaves the user's
/// clipboard stuck on the transcript.
///
/// Skips (returns `false`) without touching anything when:
/// - `focus.bundle_id` is Voicebox itself — step 6 will inject directly
///   into our own webview; pasting would just double-insert or miss the
///   real target.
/// - Accessibility is not trusted — `CGEventPost` would silently drop the
///   keystroke, leaving the user's clipboard clobbered with nothing to
///   show for it.
///
/// Returns `true` when the paste sequence completed end-to-end.
#[command]
async fn paste_final_text(
    text: String,
    focus: focus_capture::FocusSnapshot,
) -> Result<bool, String> {
    if focus.bundle_id.as_deref() == Some(VOICEBOX_BUNDLE_ID) {
        return Ok(false);
    }
    if !accessibility::is_trusted() {
        return Err(
            "Accessibility permission required for auto-paste. Open System Settings → Privacy & Security → Accessibility and enable Voicebox."
                .into(),
        );
    }

    focus_capture::activate_pid(focus.pid)?;
    tokio::time::sleep(std::time::Duration::from_millis(POST_ACTIVATE_SETTLE_MS)).await;

    let snapshot = clipboard::save_clipboard()?;
    let after_write = clipboard::write_text(&text)?;

    let paste_result = synthetic_keys::send_paste();
    tokio::time::sleep(std::time::Duration::from_millis(PASTE_CONSUME_MS)).await;

    let safe_to_restore = matches!(
        clipboard::current_change_count(),
        Ok(current) if current == after_write
    );
    if safe_to_restore {
        clipboard::restore_clipboard(&snapshot)?;
    } else {
        eprintln!(
            "[voicebox] clipboard mutated during paste window — skipping restore to preserve newer content"
        );
    }

    paste_result?;
    Ok(true)
}

/// Inspect the currently focused UI element. Returns the owning app's PID,
/// bundle id, and AX role. Useful for sanity-checking the focus pipeline
/// before committing to a paste.
#[command]
fn debug_capture_focus() -> Result<focus_capture::FocusSnapshot, String> {
    focus_capture::capture_focus()
}

/// Full auto-paste rehearsal: snapshot the focus target now, sleep
/// `drift_ms` so the user can deliberately switch to a different app
/// (proving we don't paste into whichever window is frontmost when the
/// transcribe finishes), then activate the captured PID, stage `text`,
/// fire ⌘V, and restore the clipboard.
#[command]
async fn debug_focus_roundtrip(
    text: String,
    drift_ms: u64,
    post_paste_delay_ms: u64,
) -> Result<serde_json::Value, String> {
    if !accessibility::is_trusted() {
        return Err(
            "Accessibility permission not granted. Open System Settings → Privacy & Security → Accessibility and enable Voicebox."
                .into(),
        );
    }

    let snapshot = focus_capture::capture_focus()?;

    tokio::time::sleep(std::time::Duration::from_millis(drift_ms)).await;

    focus_capture::activate_pid(snapshot.pid)?;
    // Give AppKit a beat to process the activation before the synthetic
    // Cmd+V arrives — without this the paste sometimes races ahead of the
    // window-ordering animation and lands in the previous frontmost app.
    tokio::time::sleep(std::time::Duration::from_millis(120)).await;

    let clip = clipboard::save_clipboard()?;
    let after_write = clipboard::write_text(&text)?;
    synthetic_keys::send_paste()?;
    tokio::time::sleep(std::time::Duration::from_millis(post_paste_delay_ms)).await;
    let before_restore = clipboard::current_change_count()?;
    clipboard::restore_clipboard(&clip)?;

    Ok(serde_json::json!({
        "focus": snapshot,
        "change_count_after_write": after_write,
        "change_count_before_restore": before_restore,
        "clobbered_during_paste": before_restore != after_write,
    }))
}

/// End-to-end smoke test for the auto-paste pipeline: save the user's
/// clipboard, stage `text`, optionally wait `pre_paste_delay_ms` so the
/// caller has time to focus the target app, synthesise ⌘V, wait
/// `post_paste_delay_ms` for the target app to consume the event, and put
/// the original clipboard back.
///
/// Short-circuits when Accessibility permission is missing — without it
/// `CGEventPost` silently drops events, so running the full sequence
/// would just clobber the clipboard with nothing to show for it.
#[command]
async fn debug_paste_text(
    text: String,
    pre_paste_delay_ms: u64,
    post_paste_delay_ms: u64,
) -> Result<serde_json::Value, String> {
    if !accessibility::is_trusted() {
        return Err(
            "Accessibility permission not granted. Open System Settings → Privacy & Security → Accessibility and enable Voicebox, then try again."
                .into(),
        );
    }

    let snapshot = clipboard::save_clipboard()?;
    let before = snapshot.change_count();
    let after_write = clipboard::write_text(&text)?;

    tokio::time::sleep(std::time::Duration::from_millis(pre_paste_delay_ms)).await;

    synthetic_keys::send_paste()?;

    tokio::time::sleep(std::time::Duration::from_millis(post_paste_delay_ms)).await;

    let before_restore = clipboard::current_change_count()?;
    clipboard::restore_clipboard(&snapshot)?;
    let after_restore = clipboard::current_change_count()?;

    Ok(serde_json::json!({
        "change_count_before": before,
        "change_count_after_write": after_write,
        "change_count_before_restore": before_restore,
        "change_count_after_restore": after_restore,
        "clobbered_during_paste": before_restore != after_write,
    }))
}

/// Manual smoke test for the clipboard snapshot/restore primitives used by
/// the auto-paste pipeline. Stages `text` on the pasteboard, waits
/// `hold_ms` so the caller can ⌘V into another app, then puts the original
/// clipboard contents back. The return value reports the change-count deltas
/// so the harness can verify no third party mutated the clipboard mid-paste.
#[command]
async fn debug_clipboard_roundtrip(
    text: String,
    hold_ms: u64,
) -> Result<serde_json::Value, String> {
    let snapshot = clipboard::save_clipboard()?;
    let before = snapshot.change_count();
    let item_count = snapshot.item_count();
    let after_write = clipboard::write_text(&text)?;

    tokio::time::sleep(std::time::Duration::from_millis(hold_ms)).await;

    let before_restore = clipboard::current_change_count()?;
    clipboard::restore_clipboard(&snapshot)?;
    let after_restore = clipboard::current_change_count()?;

    Ok(serde_json::json!({
        "saved_items": item_count,
        "change_count_before": before,
        "change_count_after_write": after_write,
        "change_count_before_restore": before_restore,
        "change_count_after_restore": after_restore,
        "clobbered_during_hold": before_restore != after_write,
    }))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_shell::init())
        // JFW-12 P3+: Globaler CPU/GPU-Switch-Hotkey. Die konkrete Belegung
        // kommt zur Laufzeit aus dem Frontend (frei in der UI änderbar) — hier
        // wird nur der Manager gestartet, keine Taste ist hardcoded.
        .plugin(tauri_plugin_global_shortcut::Builder::<tauri::Wry>::default().build())
        .manage(ServerState {
            #[cfg(windows)]
            sidecar_job: Mutex::new(None),
            #[cfg(not(windows))]
            child: Mutex::new(None),
            server_pid: Mutex::new(None),
            models_dir: Mutex::new(None),
            api_token: Mutex::new(None),
            generation: Mutex::new(0),
            sidecar_port: Mutex::new(None),
        })
        .manage(audio_capture::AudioCaptureState::new())
        .manage(audio_output::AudioOutputState::new())
        .setup(|app| {
            // JFW-12 B2: Der BackendSupervisor-Actor startet vor jedem Window/Command.
            // Genau ein Tokio-Task besitzt die mutable Backendwahrheit; Commands
            // laufen ueber eine begrenzte MPSC-Mailbox (B2).
            // JFW-12 B9: Der CUDA-Artefaktmanager trägt Backend-Root, App-Version
            // und exakte Build-ID des laufenden Builds.
            let backend_root = std::env::var("LOCALAPPDATA")
                .map(std::path::PathBuf::from)
                .unwrap_or_else(|_| app.path().app_data_dir()
                    .expect("no LOCALAPPDATA and no app_data_dir"))
                .join("JFWhisper")
                .join("backends");
            let manager = backend::artifact::Manager::new(
                backend_root,
                env!("CARGO_PKG_VERSION").to_string(),
                env!("JFW_BUILD_ID").to_string(),
            );
            // JFW-12 Block (g): Echte Switch-Prozessschritte aus main.rs — der
            // Supervisor spawnt pro angenommener Operation einen Blocking-Task,
            // der run_switch mit diesen Closures füttert.
            let switch_steps: std::sync::Arc<dyn backend::switch_driver::SwitchStepFactory> =
                std::sync::Arc::new(MainSwitchSteps { app: app.handle().clone() });
            app.manage(backend::supervisor::Supervisor::start(app.handle().clone(), manager, Some(switch_steps)));

            #[cfg(desktop)]
            {
                // JFW-1: Updater ist aus dem Produktprofil entfernt (updater_enabled=false) —
                // kein Update-Kanal, keine Fremdserver-Wiederverwendung.
                app.handle().plugin(tauri_plugin_process::init())?;

                // Resolve the active keyboard layout's V keycode now, on
                // the main thread, and register an observer for layout
                // changes. The synthetic-paste hot path then only reads an
                // atomic. See keyboard_layout.rs for why this matters
                // (Cmd+V is matched by translated character, not keycode,
                // so QWERTY keycode 9 produces Cmd+. on Dvorak).
                keyboard_layout::init();

                // HotkeyMonitor is spawned lazily via the `enable_hotkey`
                // command — see HotkeyState. The hidden dictate webview is
                // safe to build up front because it does not create the global
                // keyboard tap or trigger the macOS Input Monitoring prompt.
                app.manage(HotkeyState::default());

                // The frontend emits `dictate:hide` whenever the pill cycle
                // finishes (rest-fade → hidden). `hide()` alone has been
                // unreliable for transparent always-on-top windows on macOS
                // — the NSWindow lingers as an invisible click target that
                // steals focus to the Voicebox app when the user clicks
                // where it used to be. Park the window off-screen and mark
                // it click-through as well, so even if `hide()` no-ops the
                // user sees and interacts with nothing.
                let handle_for_hide = app.handle().clone();
                app.handle().listen("dictate:hide", move |_event| {
                    if let Some(window) = handle_for_hide.get_webview_window(DICTATE_WINDOW_LABEL) {
                        let _ = window.set_ignore_cursor_events(true);
                        let _ = window.set_position(PhysicalPosition::new(-10_000, -10_000));
                        let _ = window.hide();
                    }
                });

                // JFW-1: TTS-/SPEAK-Pfad entfernt — der `dictate:show`-Listener
                // bleibt für Frontend-Aufrufe, die das Pill-Fenster explizit
                // erzwingen wollen. Der speak_monitor (Backend-SSE /events/speak)
                // ist mit dem LLM-/TTS-Cut entfallen.
                let handle_for_show = app.handle().clone();
                app.handle().listen("dictate:show", move |_event| {
                    show_dictate_window(&handle_for_show);
                });

                ensure_dictate_window(app.handle());
            }

            // Hide title bar icon on Windows
            #[cfg(windows)]
            {
                use windows::Win32::Foundation::HWND;
                use windows::Win32::UI::WindowsAndMessaging::{SetClassLongPtrW, GCLP_HICON, GCLP_HICONSM};
                
                if let Some((_, window)) = app.webview_windows().iter().next() {
                    if let Ok(hwnd) = window.hwnd() {
                        let hwnd = HWND(hwnd.0);
                        unsafe {
                            // Set both small and regular icons to NULL to hide the title bar icon
                            SetClassLongPtrW(hwnd, GCLP_HICON, 0);
                            SetClassLongPtrW(hwnd, GCLP_HICONSM, 0);
                        }
                    }
                }
            }

            // Enable microphone access on Linux (WebKitGTK denies getUserMedia by default)
            #[cfg(target_os = "linux")]
            {
                use tauri::Manager;
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.with_webview(|webview| {
                        use webkit2gtk::{WebViewExt, SettingsExt, PermissionRequestExt};
                        use webkit2gtk::glib::ObjectExt;
                        let wk_webview = webview.inner();

                        // Enable media stream support in WebKitGTK settings
                        if let Some(settings) = WebViewExt::settings(&wk_webview) {
                            settings.set_enable_media_stream(true);
                        }

                        // Auto-grant UserMediaPermissionRequest (microphone access)
                        // Only for trusted local origins (Tauri dev server or custom protocol)
                        wk_webview.connect_permission_request(move |webview, request: &webkit2gtk::PermissionRequest| {
                            if request.is::<webkit2gtk::UserMediaPermissionRequest>() {
                                let uri = WebViewExt::uri(webview).unwrap_or_default();
                                let is_trusted = uri.starts_with("tauri://")
                                    || uri.starts_with("https://tauri.localhost")
                                    || uri.starts_with("http://localhost")
                                    || uri.starts_with("http://127.0.0.1");
                                if is_trusted {
                                    request.allow();
                                    return true;
                                }
                                request.deny();
                                return true;
                            }
                            false
                        });
                    });
                }
            }

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            start_server,
            stop_server,
            restart_server,
            supervisor_snapshot,
            supervisor_admit,
            request_backend_switch,
            install_cuda_addon,
            repair_cuda_addon,
            remove_cuda_addon,
            start_system_audio_capture,
            stop_system_audio_capture,
            is_system_audio_supported,
            list_audio_output_devices,
            play_audio_to_devices,
            stop_audio_playback,
            debug_clipboard_roundtrip,
            debug_paste_text,
            debug_capture_focus,
            debug_focus_roundtrip,
            check_accessibility_permission,
            check_input_monitoring_permission,
            open_accessibility_settings,
            open_input_monitoring_settings,
            paste_final_text,
            enable_hotkey,
            disable_hotkey,
            update_chord_bindings,
            sidecar::sidecar_request,
            sidecar::sidecar_upload,
            sidecar::sidecar_fetch_bytes,
            sidecar::sidecar_stream
        ])
        .on_window_event({
            let closing = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
            move |window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // If we're already in the close flow, let it proceed
                if closing.load(std::sync::atomic::Ordering::SeqCst) {
                    return;
                }
                closing.store(true, std::sync::atomic::Ordering::SeqCst);

                // Prevent automatic close so frontend can clean up
                api.prevent_close();

                // Emit event to frontend to check setting and stop server if needed
                let app_handle = window.app_handle();

                if let Err(e) = app_handle.emit("window-close-requested", ()) {
                    eprintln!("Failed to emit window-close-requested event: {}", e);
                    window.close().ok();
                    return;
                }

                // Set up listener for frontend response
                let window_for_close = window.clone();
                let closing_for_timeout = closing.clone();
                let (tx, mut rx) = mpsc::unbounded_channel::<()>();

                let listener_id = window.listen("window-close-allowed", move |_| {
                    let _ = tx.send(());
                });

                tauri::async_runtime::spawn(async move {
                    tokio::select! {
                        _ = rx.recv() => {
                            window_for_close.close().ok();
                        }
                        _ = tokio::time::sleep(tokio::time::Duration::from_secs(5)) => {
                            eprintln!("Window close timeout, closing anyway");
                            window_for_close.close().ok();
                        }
                    }
                    window_for_close.unlisten(listener_id);
                    closing_for_timeout.store(false, std::sync::atomic::Ordering::SeqCst);
                });
            }
        }})
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            let _ = &app; // used on unix
            match &event {
                RunEvent::Exit => {
                    let state = app.state::<ServerState>();
                    let has_pid = state.server_pid.lock().unwrap().is_some();
                    println!("RunEvent::Exit — has_pid={}", has_pid);

                    // JFW-12 B8 (Windows): Das Job Object wird hier entnommen;
                    // sein Drop am Ende des Arms schließt das Job und beendet
                    // den kompletten Sidecar-Prozessbaum (KILL_ON_JOB_CLOSE).
                    #[cfg(windows)]
                    let job = state.sidecar_job.lock().unwrap().take();

                    if has_pid {
                        println!("RunEvent::Exit - closing sidecar process tree");
                    }

                    // JFW-12 B8: Job-Close am Ende des Arms → `job` wird gedroppt.
                    #[cfg(windows)]
                    {
                        let _ = job;
                    }
                }
                RunEvent::ExitRequested { api, .. } => {
                    println!("RunEvent::ExitRequested received");
                    // Don't prevent exit, just log it
                    let _ = api;
                }
                _ => {}
            }
        });
}

fn main() {
    run();
}

include!(concat!(env!("CARGO_MANIFEST_DIR"), "/bench_module.rs"));
#[cfg(test)]
mod silence_wav_tests {
    use super::silence_wav_bytes;
    use sha2::{Digest, Sha256};

    /// Hashbindung (Spec B5.5): Der Silence-Smoke ist fest eingebaut und sein
    /// SHA-256 wird hier referenzgebunden geprüft — eine versehentliche Änderung
    /// des Smoke-Audio (Länge/Format) bricht den Test sofort. Referenzwert wurde
    /// gegen die Python-Erzeugung (16 kHz mono 16-bit, 0,5 s Stille) abgeglichen.
    #[test]
    fn silence_wav_hash_gebunden() {
        let wav = silence_wav_bytes();
        // Struktur-Sanity: RIFF-Header + exakt 44 Header-Bytes + 16000 Datenbytes.
        assert_eq!(wav.len(), 44 + 16_000, "WAV-Gesamtgröße");
        assert_eq!(&wav[0..4], b"RIFF", "RIFF-Magic");
        assert_eq!(&wav[8..12], b"WAVE", "WAVE-Formate");

        let mut h = Sha256::new();
        h.update(&wav);
        let digest = h.finalize();
        // Manueller Hex (kein hex-Crate nötig) — Referenz aus der Python-Erzeugung.
        let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(
            hex, "358c6dcef4442790decb0a5c03fb320154f9d1dd5b4618e301f9e6661a413cb5",
            "Silence-WAV-Hash hat sich geändert — Smoke-Audio bewusst anpassen + Referenz aktualisieren"
        );
    }
}
