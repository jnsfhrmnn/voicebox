#[cfg(test)]
mod switch_bench {
    //! JFW-12 P3: 30×-Idle-Switch-Benchmark auf realer Hardware (Spec B5/B6,
    //! Abnahme-Zeilen 121/124). Treibt dieselbe Orchestrierung wie die Produktion
    //! (`switch_driver::run_switch`) mit echten Prozessschritten: `spawn_sidecar` +
    //! Job-Object, Ready-Handshake, Silence-WAV-Inferenz-Smoke, NVML-VRAM-Evidence.
    //! Läuft nur mit `JFW_SWITCH_BENCH=1`; sonst skip (CI bleibt grün).
//!
//! BEWUSSTES TEST-DOUBLE (Review 2026-09-22, F-05): Start-/Teardown-/Smoke-Logik
//! ist hier eine zweite Implementierung neben `main.rs::MainSwitchSteps`.
//! PARITÄTS-REGEL: jede Maßnahme am Prozesspfad MUSS an BEIDEN Stellen gesetzt
//! werden (Drift-Erfahrung 87845d9: Graceful-Fenster). Geteilte Werte gehören in
//! `super::`-Konstanten (z. B. `GRACEFUL_SHUTDOWN_WINDOW`); Abweichungen sind im
//! Kommentar zu begründen.
    //!
    //! Voraussetzungen: CPU-Sidecar in binaries/, installierter CUDA-Build unter
    //! %LOCALAPPDATA%\JFWhisper-bench\backends\cuda (wird bei Bedarf per
    //! `install_release()` aus dem Live-Releasepfad nachinstalliert), Whisper-Modell
    //! im HF-Cache, Release-Server auf :443.

    use crate::backend::gpu_evidence::{GpuContextProbe, GpuReceipt, ReleaseVerification};
    use crate::backend::process_windows::{spawn_sidecar, SidecarJob, SidecarLine};
    use crate::backend::state::{BackendVariant, SidecarInstance};
    use crate::backend::switch_driver::{run_switch, SwitchContext, SwitchOutcome};
    use crate::backend::switch_evidence::SwitchDirection;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::{Arc, Mutex};

    /// Laufendes Backend (PID/Port/Token/Generation/Job) — wie ServerState in der App.
    struct Live {
        pid: u32,
        port: u16,
        token: String,
        generation: u64,
        variant: BackendVariant,
        job: SidecarJob,
    }

    /// Startet das Backend `variant` und wartet auf den Ready-Handshake.
    fn start_backend(
        slot: &mut Option<Live>,
        variant: BackendVariant,
        cpu_exe: &Path,
        cuda_dir: &Path,
        data_root: &Path,
    ) -> Result<(), String> {
        let (exe_owned, cwd_owned): (PathBuf, Option<PathBuf>) = match variant {
            // CPU-Build ist onedir (JFW-12 P3): Exe + _internal/ im selben Ordner;
            // cwd auf den Installationsordner setzen wie beim CUDA-Pfad.
            BackendVariant::Cpu => (cpu_exe.to_path_buf(), cpu_exe.parent().map(|p| p.to_path_buf())),
            BackendVariant::Cuda => (cuda_dir.join("jf-whisper-server-cuda.exe"), Some(cuda_dir.to_path_buf())),
        };
        if !exe_owned.exists() {
            return Err(format!("Binary fehlt: {}", exe_owned.display()));
        }
        let (exe_path, cwd): (&Path, Option<&Path>) = match &cwd_owned {
            None => (exe_owned.as_path(), None),
            Some(c) => (exe_owned.as_path(), Some(c.as_path())),
        };
        let generation = slot.as_ref().map(|l| l.generation).unwrap_or(0) + 1;
        let token = "bench".repeat(8); // 32 Zeichen, wie in der App.
        let args = vec![
            "--data-dir".into(),
            data_root.to_string_lossy().to_string(),
            "--port".into(),
            "0".into(),
            "--parent-pid".into(),
            std::process::id().to_string(),
        ];
        let env: Vec<(String, String)> = vec![
            ("JFWHISPER_API_TOKEN".to_string(), token.clone()),
            ("JFWHISPER_GENERATION".to_string(), generation.to_string()),
        ];
        let (job, mut line_rx) = spawn_sidecar(exe_path, &args, &env, cwd).map_err(|e| e.to_string())?;
        // Handshake-Brücke: tokio-MPSC → std-Kanal auf einem eigenen Thread.
        let (std_tx, std_rx) = std::sync::mpsc::channel::<Result<String, String>>();
        {
            let tx = std_tx;
            std::thread::spawn(move || {
                let rt = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .unwrap();
                rt.block_on(async move {
                    while let Some(line) = line_rx.recv().await {
                        let s = match line {
                            SidecarLine::Stdout(s) => Ok(s),
                            SidecarLine::Stderr(s) => Err(s),
                        };
                        if tx.send(s).is_err() {
                            break;
                        }
                    }
                });
            });
        }
        let instance = super::wait_sidecar_ready(&std_rx, job.identity.pid, variant, exe_path).map_err(|e| e.to_string())?;
        *slot = Some(Live {
            pid: instance.pid,
            port: instance.port,
            token,
            generation,
            variant,
            job,
        });
        Ok(())
    }

    /// Graceful Shutdown + Job-Close (beendet den kompletten Prozessbaum).
    fn stop_backend(slot: &mut Option<Live>) {
        if let Some(live) = slot.take() {
            if let Ok(client) = reqwest::blocking::Client::builder()
                .timeout(std::time::Duration::from_secs(2))
                .build()
            {
                let _ = client.post(format!("http://127.0.0.1:{}/shutdown", live.port)).send();
            }
            // Kurzes Graceful-Fenster identisch zur Produktion (main.rs,
            // teardown_source): 1 s — Maßnahmen-Parität, damit der 30x-Bench das
            // Idle-Budget mit demselben Verhalten misst wie der Produkt-Pfad.
            let deadline = std::time::Instant::now() + super::GRACEFUL_SHUTDOWN_WINDOW;
            while super::is_process_alive(live.pid) && std::time::Instant::now() < deadline {
                std::thread::sleep(std::time::Duration::from_millis(100));
            }
            drop(live); // SidecarJob-Drop → KILL_ON_JOB_CLOSE.
        }
    }

    /// Ein gemessener Durchlauf mit getrennten Phasen (Spec: „getrennte Zeitanteile").
    #[derive(Debug)]
    struct Run {
        ok: bool,
        total_ms: u64,
        drain_ms: u64,
        teardown_ms: u64,
        start_ms: u64,
        smoke_ms: u64,
        vram_ms: u64,
        error: Option<String>,
    }

    /// Führt einen Idle-Switch über `run_switch` aus (Produktions-Sequenz) und misst Phasen.
    fn run_one(
        state: &Arc<Mutex<Option<Live>>>,
        direction: SwitchDirection,
        cpu_exe: &Path,
        cuda_dir: &Path,
        data_root: &Path,
    ) -> Run {
        let source = match direction {
            SwitchDirection::CpuToCuda => BackendVariant::Cpu,
            SwitchDirection::CudaToCpu => BackendVariant::Cuda,
        };

        // Setup (nicht gemessen): Ausgangs-Backend muss laufen — sonst booten.
        let setup_err = {
            let mut g = state.lock().unwrap();
            if g.as_ref().map(|l| l.variant) != Some(source) {
                stop_backend(&mut *g);
                start_backend(&mut *g, source, cpu_exe, cuda_dir, data_root).err()
            } else {
                None
            }
        };
        let old_cuda_pid = if direction == SwitchDirection::CudaToCpu {
            state.lock().unwrap().as_ref().map(|l| l.pid)
        } else {
            None
        };

        // Jede Closure bekommt ihren eigenen Arc-Klon (mehrere move-Closures
        // dürfen denselben Wert nicht bewegen).
        let st_drain = Arc::clone(state);
        let st_teardown = Arc::clone(state);
        let st_start = Arc::clone(state);
        let st_smoke = Arc::clone(state);
        let st_fail = Arc::clone(state);

        let drain_ms = Arc::new(AtomicU64::new(0));
        let teardown_ms = Arc::new(AtomicU64::new(0));
        let start_ms = Arc::new(AtomicU64::new(0));
        let smoke_ms = Arc::new(AtomicU64::new(0));
        let vram_ms = Arc::new(AtomicU64::new(0));
        let err_slot: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));

        // Jede Closure bekommt einen Klon; die Originale bleiben für das Lesen lesbar.
        let (drain_c, teardown_c, start_c, smoke_c, vram_c) = (
            Arc::clone(&drain_ms), Arc::clone(&teardown_ms), Arc::clone(&start_ms),
            Arc::clone(&smoke_ms), Arc::clone(&vram_ms),
        );
        let err_closure = Arc::clone(&err_slot); // für die on_failed-Closure; Original bleibt lesbar.

        let t0 = std::time::Instant::now();
        let outcome = match setup_err {
            Some(e) => SwitchOutcome::Failed(format!("Setup fehlgeschlagen: {e}")),
            None => run_switch(
                &SwitchContext { op_id: "bench".into(), direction },
                // 1) Drain (B5.2/B6.1): aktive Jobs drainieren, bis null aktiv.
                move || {
                    let t = std::time::Instant::now();
                    let r = (|| -> Result<(), String> {
                        let (port, token, gen) = {
                            let g = st_drain.lock().unwrap();
                            let live = g.as_ref().ok_or("kein aktives Backend")?;
                            (live.port, live.token.clone(), live.generation)
                        };
                        let client = reqwest::blocking::Client::builder()
                            .timeout(std::time::Duration::from_secs(2))
                            .build().map_err(|e| e.to_string())?;
                        loop {
                            let resp = client
                                .get(format!("http://127.0.0.1:{port}/tasks/active"))
                                .header("Authorization", format!("Bearer {token}"))
                                .header("x-jfwhisper-generation", &gen.to_string())
                                .send().map_err(|e| e.to_string())?;
                            if !resp.status().is_success() {
                                return Err(format!("Drain HTTP {}", resp.status()));
                            }
                            let v: serde_json::Value = resp.json().map_err(|e| e.to_string())?;
                            if super::parse_active_task_count(&v)? == 0 {
                                return Ok(());
                            }
                            std::thread::sleep(std::time::Duration::from_millis(250));
                        }
                    })();
                    drain_c.fetch_add(t.elapsed().as_millis() as u64, Ordering::Relaxed);
                    r.map_err(|e| e.to_string())
                },
                // 2) Teardown des Ausgangs-Backends (B5/B6): graceful + Job-Close.
                move || {
                    let t = std::time::Instant::now();
                    stop_backend(&mut *st_teardown.lock().unwrap());
                    teardown_c.fetch_add(t.elapsed().as_millis() as u64, Ordering::Relaxed);
                    Ok(())
                },
                // 3) Zielstart + Handshake (B5.4/B6.2).
                move || {
                    let t = std::time::Instant::now();
                    let target = match direction {
                        SwitchDirection::CpuToCuda => BackendVariant::Cuda,
                        SwitchDirection::CudaToCpu => BackendVariant::Cpu,
                    };
                    let r = start_backend(&mut *st_start.lock().unwrap(), target, cpu_exe, cuda_dir, data_root);
                    start_c.fetch_add(t.elapsed().as_millis() as u64, Ordering::Relaxed);
                    r.map(|_| {
                        let g = st_start.lock().unwrap();
                        let live = g.as_ref().expect("Ziel läuft nach Start");
                        SidecarInstance::new(live.pid, String::new(), target, live.port, String::new())
                    })
                },
                // 4) Readiness-Smoke (B5.5/B6.2): Health + Silence-WAV-Inferenz + Modellvertrag.
                move |instance: &SidecarInstance| {
                    let t = std::time::Instant::now();
                    let r = (|| -> Result<(), String> {
                        let (token, gen) = {
                            let g = st_smoke.lock().unwrap();
                            let live = g.as_ref().ok_or("kein aktives Backend")?;
                            (live.token.clone(), live.generation)
                        };
                        let port = instance.port;
                        let client = reqwest::blocking::Client::builder()
                            .timeout(std::time::Duration::from_secs(5))
                            .build().map_err(|e| e.to_string())?;
                        // uvicorn bindet erst NACH der Handshake-Zeile — kurzer Retry.
                        for _ in 0..20 {
                            match client.get(format!("http://127.0.0.1:{port}/health")).send() {
                                Ok(resp) if resp.status().is_success() => break,
                                Err(_) | Ok(_) => {}
                            }
                            std::thread::sleep(std::time::Duration::from_millis(250));
                        }
                        let resp = client.get(format!("http://127.0.0.1:{port}/health")).send().map_err(|e| e.to_string())?;
                        if !resp.status().is_success() {
                            return Err(format!("Health HTTP {}", resp.status()));
                        }
                        let v: serde_json::Value = resp.json().map_err(|e| e.to_string())?;
                        if v.get("status").and_then(|s| s.as_str()) != Some("healthy") {
                            return Err("Health nicht healthy".into());
                        }
                        // Inferenz-Smoke (B5.5): Silence-WAV durch echten Transkriptionspfad.
                        let wav = super::silence_wav_bytes();
                        let form = reqwest::blocking::multipart::Form::new()
                            .part("file", reqwest::blocking::multipart::Part::bytes(wav).file_name("smoke.wav"));
                        let smoke_client = reqwest::blocking::Client::builder()
                            .timeout(std::time::Duration::from_secs(120))
                            .build().map_err(|e| e.to_string())?;
                        let resp = smoke_client
                            .post(format!("http://127.0.0.1:{port}/transcribe"))
                            .header("Authorization", format!("Bearer {token}"))
                            .header("x-jfwhisper-generation", &gen.to_string())
                            .multipart(form)
                            .send().map_err(|e| e.to_string())?;
                        if !resp.status().is_success() {
                            return Err(format!("Inferenz-Smoke HTTP {}", resp.status()));
                        }
                        // Modellvertrag (B5.6): geladenes Modell + Zielvariante bestätigt.
                        let h2 = client.get(format!("http://127.0.0.1:{port}/health")).send().map_err(|e| e.to_string())?;
                        let v2: serde_json::Value = h2.json().map_err(|e| e.to_string())?;
                        if !v2.get("model_loaded").and_then(|b| b.as_bool()).unwrap_or(false) {
                            return Err("Modell nach Smoke nicht geladen".into());
                        }
                        let expected = instance.variant.as_str();
                        match v2.get("backend_variant").and_then(|s| s.as_str()) {
                            Some(bv) if bv == expected => Ok(()),
                            other => Err(format!("Variant-Vertrag verletzt: erwartet {expected}, gemeldet {other:?}")),
                        }
                    })();
                    smoke_c.fetch_add(t.elapsed().as_millis() as u64, Ordering::Relaxed);
                    r.map_err(|e| e.to_string())
                },
                // 5) VRAM-Evidence (B6.6–7, nur CudaToCpu): doppelte negative NVML-Probe.
                if direction == SwitchDirection::CudaToCpu {
                    let old_pid = old_cuda_pid;
                    Some(Box::new(move |_instance: &SidecarInstance| {
                        let pid = match old_pid {
                            Some(p) => p,
                            None => return Err("VRAM-Evidence unmöglich: kein bekannter CUDA-PID".into()),
                        };
                        let t = std::time::Instant::now();
                        let r = (|| -> Result<(), String> {
                            // Ausgangs-Probe **nach** dem CUDA-Teardown (B6.4–5).
                            let probe = GpuContextProbe::capture().map_err(|e| format!("NVML-Probe: {e}"))?;
                            if probe.contains_pid(pid) {
                                return Err(format!("CUDA-PID {pid} nach Teardown noch aktiv"));
                            }
                            // Nur der jf-whisper-CUDA-PID zählt (B6); zwei grüne Proben ≥1 s.
                            let mut receipt = GpuReceipt::for_pids([pid]);
                            for attempt in 0..2u32 {
                                if attempt == 1 {
                                    std::thread::sleep(std::time::Duration::from_secs(1));
                                }
                                let p = GpuContextProbe::capture().map_err(|e| format!("NVML-Wiederholprobe: {e}"))?;
                                match receipt.verify(&p, std::time::Duration::from_secs(1)) {
                                    Ok(ReleaseVerification::Green) => {}
                                    Ok(ReleaseVerification::Red(pids)) => {
                                        return Err(format!("Compute-PIDs noch aktiv: {pids:?}"));
                                    }
                                    Err(e) => return Err(format!("{e}")),
                                }
                            }
                            let mib = receipt.attributable_mib().map_err(|e| format!("{e}"))?;
                            debug_assert_eq!(mib, 0);
                            Ok(())
                        })();
                        vram_c.fetch_add(t.elapsed().as_millis() as u64, Ordering::Relaxed);
                        r.map_err(|e| e.to_string())
                    }))
                } else {
                    None
                },
                // Fail-closed nach Zielstart (B5.8/B6.8): frisch gestartetes Ziel beenden.
                move |instance: &SidecarInstance| {
                    let mut g = st_fail.lock().unwrap();
                    if g.as_ref().map(|l| l.pid) == Some(instance.pid) {
                        stop_backend(&mut *g);
                    }
                },
                || {},
                |_| {},
                move |reason| {
                    *err_closure.lock().unwrap() = Some(reason);
                },
            )
        };
        let total_ms = t0.elapsed().as_millis() as u64;

        let run = Run {
            ok: matches!(outcome, SwitchOutcome::Completed),
            total_ms,
            drain_ms: drain_ms.load(Ordering::Relaxed),
            teardown_ms: teardown_ms.load(Ordering::Relaxed),
            start_ms: start_ms.load(Ordering::Relaxed),
            smoke_ms: smoke_ms.load(Ordering::Relaxed),
            vram_ms: vram_ms.load(Ordering::Relaxed),
            error: err_slot.lock().unwrap().clone(),
        };
        run
    }

    /// 30×-Benchmark je Richtung (Spec-Zeilen 119/122): ≥29/30 innerhalb des Idle-Budgets.
    #[test]
    fn bench_30_switches_per_direction() {
        if std::env::var("JFW_SWITCH_BENCH").is_err() {
            eprintln!("SKIP: JFW_SWITCH_BENCH nicht gesetzt (reale Hardware + beide Backends erforderlich)");
            return;
        }
        let data_root = PathBuf::from(std::env::var("LOCALAPPDATA").unwrap_or_else(|_| ".".into())).join("JFWhisper-bench");
        std::fs::create_dir_all(&data_root).unwrap();
        // CPU-Sidecar auflösen: onedir-Layout (Ordner + Exe darin) bevorzugen,
        // Fallback auf das alte onefile-Einzel-Exe. JFW-12 P3: onedir spart den
        // ~18-s-Self-Unpack; die Messung muss exakt das messen, was gebaut wurde.
        let bin_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("binaries");
        let triple_dir = if cfg!(windows) { "jf-whisper-server-x86_64-pc-windows-msvc" } else { "jf-whisper-server" };
        let onedir_exe = bin_root.join(triple_dir).join(if cfg!(windows) { "jf-whisper-server.exe" } else { "jf-whisper-server" });
        let onefile_exe = bin_root.join(if cfg!(windows) { format!("{triple_dir}.exe") } else { triple_dir.to_string() });
        let cpu_exe = if onedir_exe.exists() { onedir_exe.clone() } else { onefile_exe.clone() };
        if !cpu_exe.exists() {
            panic!("CPU-Sidecar fehlt (onedir: {} / onefile: {})", onedir_exe.display(), onefile_exe.display());
        }

        // Installierten CUDA-Build auflösen; bei Bedarf per install_release() nachinstallieren.
        let cuda_root = data_root.join("backends").join("cuda");
        let current_path = cuda_root.join("current.json");
        if !current_path.exists() {
            eprintln!("CUDA-Build nicht installiert — install_release() aus dem Live-Releasepfad (einmalig) …");
            let manager = crate::backend::artifact::Manager::new(
                data_root.join("backends"),
                "0.5.0".into(),
                env!("JFW_BUILD_ID").to_string(),
            );
            manager
                .install_release(|phase| eprintln!("[install] {phase}"))
                .expect("CUDA-Installation muss gelingen");
        }
        let current: crate::backend::artifact::CurrentPointer =
            serde_json::from_str(&std::fs::read_to_string(&current_path).unwrap()).unwrap();
        let cuda_dir = cuda_root.join(&current.build_id);

        let state: Arc<Mutex<Option<Live>>> = Arc::new(Mutex::new(None));
        // JFW_BENCH_DIR=cpu2cuda|cuda2cpu schränkt auf eine Richtung ein (gezielte
        // Nachläufe); Default = beide Richtungen in einem Lauf.
        let scope = std::env::var("JFW_BENCH_DIR").unwrap_or_default();
        // Warm-up je Richtung (Erstladung/Cache) — wird NICHT in die 30 gezählt.
        let warm_dirs: Vec<SwitchDirection> = match scope.as_str() {
            "cpu2cuda" => vec![SwitchDirection::CpuToCuda],
            "cuda2cpu" => vec![SwitchDirection::CudaToCpu],
            _ => vec![SwitchDirection::CpuToCuda, SwitchDirection::CudaToCpu],
        };
        for dir in warm_dirs {
            eprintln!("Warm-up {:?} …", dir);
            let warm = run_one(&state, dir, &cpu_exe, &cuda_dir, &data_root);
            if !warm.ok {
                panic!("Warm-up {:?} fehlgeschlagen: {:?}", dir, warm.error);
            }
        }

        // Beide Richtungen vollständig messen (evidenzvollständige Matrix),
        // dann beide Abnahmen prüfen — ein Lauf liefert das komplette Bild.
        let directions: Vec<(SwitchDirection, u64)> = match scope.as_str() {
            "cpu2cuda" => vec![(SwitchDirection::CpuToCuda, 15_000)],
            "cuda2cpu" => vec![(SwitchDirection::CudaToCpu, 13_500)],
            _ => vec![
                (SwitchDirection::CpuToCuda, 15_000),
                // CudaToCpu: 13,5 s (Spec AC Zeile 124, Kalibrierung JFW-12 P3
                // 2026-09-19 mit onedir-CPU-Sidecar; die alte 10-s-Zahl war eine
                // onefile-Annahme und ist gegen die kalibrierte Spec ersetzbar).
                (SwitchDirection::CudaToCpu, 13_500),
            ],
        };
        let mut failures = Vec::new();
        for (direction, budget_ms) in directions {
            let mut within = 0u32;
            for i in 0..30u32 {
                let r = run_one(&state, direction, &cpu_exe, &cuda_dir, &data_root);
                if r.ok && r.total_ms <= budget_ms {
                    within += 1;
                }
                if let Some(e) = &r.error {
                    eprintln!("  FEHLER: {e}");
                }
                eprintln!(
                    "[{}/30] {:?} {} ms (drain {} / teardown {} / start {} / smoke {} / vram {}) {}",
                    i + 1,
                    direction,
                    r.total_ms,
                    r.drain_ms,
                    r.teardown_ms,
                    r.start_ms,
                    r.smoke_ms,
                    r.vram_ms,
                    if r.ok { "OK" } else { "FEHLER" }
                );
            }
            eprintln!(
                "ERGEBNIS {:?}: {within}/30 innerhalb {} ms",
                direction, budget_ms
            );
            if within < 29 {
                failures.push(format!(
                    "{:?}: nur {within}/30 innerhalb des Budgets ({budget_ms} ms)",
                    direction
                ));
            }
        }
        assert!(
            failures.is_empty(),
            "Abnahme nicht erfüllt: {}",
            failures.join(" | ")
        );
    }
}
