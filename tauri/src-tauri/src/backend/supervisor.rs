//! JFW-12 B2/B3: Der serielle BackendSupervisor-Actor.
//!
//! Genau ein Tokio-Task besitzt die mutable Backendwahrheit (`BackendSupervisorState`).
//! Commands gelangen über eine begrenzte MPSC-Mailbox zum Actor; Inferenz- und
//! Audiobytes laufen nicht durch diese Mailbox (B2). Jede Zustandsänderung wird
//! als typisiertes Event `backend:supervisor` an die Webviews gesendet — der
//! Webview spricht den Sidecar nie direkt an (D: Surface).
//!
//! Block-(b)-Scope: CPU-Boot-Lifecycle durch den Actor, Admission mit
//! Generationenfence, typisierte Commands. CUDA-Switches sind fail-closed, bis
//! Block (d) das signierte Artefakt liefert (`ArtifactPhase::NotInstalled`).

use std::sync::{Arc, Mutex};

use tauri::{AppHandle, Emitter};

use crate::backend::admission;
use crate::backend::artifact::Manager;
use crate::backend::state::*;
use crate::backend::switch_plan::{decide_switch, SwitchDecision};

/// Event-Name für typisierte Supervisor-Zustände (D: Surface).
pub const SUPERVISOR_EVENT: &str = "backend:supervisor";

const MAILBOX_CAPACITY: usize = 64;

/// Sendet ein Command mit begrenztem Wartefenster (Review 2026-09-22, F-03):
/// `try_send` allein verwirft bei voller Mailbox STILL — für terminale
/// (Boot-/Switch-Abschluss-)Commands ist das inakzeptabel. Hier: bis 2 s in
/// 50-ms-Schritten nachlegen; danach laut protokollieren statt schweigen.
/// Aufruf nur aus synchronen Kontexten (die Actor-Caller sind es).
fn send_with_timeout(tx: &tokio::sync::mpsc::Sender<Command>, mut cmd: Command) -> bool {
    for _ in 0..40 {
        match tx.try_send(cmd) {
            Ok(()) => return true,
            Err(tokio::sync::mpsc::error::TrySendError::Full(m)) => {
                cmd = m;
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(tokio::sync::mpsc::error::TrySendError::Closed(_)) => {
                eprintln!("[supervisor] FEHLER: Mailbox geschlossen — terminales Command verworfen");
                return false;
            }
        }
    }
    eprintln!("[supervisor] FEHLER: Mailbox 2 s voll — terminales Command verworfen");
    false
}


enum Command {
    /// CPU-Sidecar wird gespawnt (vorhandener Spawn-Pfad in main.rs).
    BootStarted,
    /// Boot fehlgeschlagen (Timeout/Crash) — B2: Fehlerphase → NoBackendReady.
    BootFailed(String),
    /// Manueller Stopp über `stop_server` — Runtime zurück nach NoBackendReady,
    /// Admission geschlossen; die App bleibt in derselben Epoche.
    Stopped,
    /// CPU-Handshake grün: Prozessidentität + Modellvertrag bekannt.
    MarkCpuReady { instance: SidecarInstance },
    /// Nutzerrequest von der GPU-Seite (main-window-initiiert, B9).
    RequestSwitch(BackendVariant),
    // ── JFW-12 Block (g): Switch-Prozessschritte (aus main.rs zurück) ──
    /// Drain + Teardown des Ausgangs-Backends bestätigt → Ziel-Vorbereitung.
    SwitchDrained { op_id: String },
    /// Ziel-Backend gestartet + Handshake grün → Ready mit neuer Generation.
    SwitchTargetReady { op_id: String, instance: SidecarInstance },
    /// Switch fehlgeschlagen → fail-closed NoBackendReady (sichtbar).
    SwitchFailed { op_id: String, reason: String },
    /// App-Ende: Admission schließen; aktive CUDA-Generation invalidieren (B8).
    Shutdown,
    // ── JFW-12 Block (d): Addon-Lifecycle (ausschließlich nutzerinitiiert) ──
    /// Nutzerstart: signiertes CUDA-Addon aus dem Releasepfad installieren.
    InstallAddon,
    /// Nutzerstart: defektes Build neu installieren (dieselbe Pipeline).
    RepairAddon,
    /// Nutzerstart: installierte Builds + Pointer entfernen.
    RemoveAddon,
    /// Phasenupdate der Artefakt-Pipeline (aus dem Blocking-Task zurück).
    ArtifactPhaseUpdate { phase: ArtifactPhase },
    /// Terminales Ergebnis einer Addon-Operation (Erfolg).
    ArtifactOperationDone { kind: OperationKind },
    /// Terminales Ergebnis einer Addon-Operation (Fehler, fail-closed).
    ArtifactOperationFailed { kind: OperationKind, reason: String },
}

/// Tauri-State: Mailbox-Handle + geteilter Read-Snapshot. Alle Mutationen laufen
/// ausschließlich im Actor; `state` dient nur typisierten Reads (Snapshot).
pub struct Supervisor {
    tx: tokio::sync::mpsc::Sender<Command>,
    state: Arc<Mutex<BackendSupervisorState>>,
    /// CUDA-Artefaktmanager (B9): Backend-Root, App-Version, Build-ID.
    manager: Manager,
    /// JFW-12 Block (g): Factory für die echten Switch-Prozessschritte aus
    /// `main.rs` (Drain/Teardown/Zielstart/Readiness/VRAM). `None` nur in
    /// Tests; ein angenommener Switch ohne Steps ist fail-closed.
    switch_steps: Option<Arc<dyn crate::backend::switch_driver::SwitchStepFactory>>,
}

impl Clone for Supervisor {
    fn clone(&self) -> Self {
        Self {
            tx: self.tx.clone(),
            state: Arc::clone(&self.state),
            manager: Manager::new(
                self.manager.base_dir.clone(),
                self.manager.app_version.clone(),
                self.manager.build_id.clone(),
            ),
            switch_steps: self.switch_steps.as_ref().map(Arc::clone),
        }
    }
}

/// Typisierter Admission-Entscheid für das Frontend (B5 Schritt 1).
#[derive(Debug, Clone, serde::Serialize)]
pub struct AdmissionOutcome {
    pub admitted: bool,
    /// `admitted=true`: bindende Generation + Variante des Permits.
    pub generation: Option<u64>,
    pub backend_variant: Option<&'static str>,
    /// Ablehnungsgrund (Closed/Stale) — inhaltsfrei, kein Audio/Text (C).
    pub reason: String,
}

impl Supervisor {
    /// Startet den Actor und liefert den State-Handle. Muss vor dem ersten
    /// Window/Command verfügbar sein (Tauri `setup`). Der übergebene Manager
    /// trägt Backend-Root, App-Version und Build-ID des laufenden Builds (B9).
    /// `switch_steps` sind die echten Prozessschritte aus `main.rs` (Block g);
    /// ohne Factory ist jeder angenommene Switch fail-closed.
    pub fn start(
        app: AppHandle,
        manager: Manager,
        switch_steps: Option<Arc<dyn crate::backend::switch_driver::SwitchStepFactory>>,
    ) -> Self {
        let app_epoch = new_app_epoch();
        // JFW-12: Ein Neustart darf ein installiertes + verifiziertes CUDA-Build
        // nicht vergessen. Der Initialzustand ist fail-closed NotInstalled; hier
        // wird die Phase aus dem atomaren Current-Pointer wiederhergestellt, wenn
        // der Build tatsächlich auf der Platte liegt (current.json wird nur nach
        // vollständigem Commit geschrieben). Ohne diesen Schritt zeigt die UI nach
        // jedem Neustart „nicht installiert" und decide_switch lehnt CpuToCuda ab —
        // obwohl das Artefakt vorhanden ist (B9).
        let mut state_inner = BackendSupervisorState::new(app_epoch.clone());
        if let Ok(Some(ptr)) = manager.current() {
            let exe_name = if cfg!(windows) {
                "jf-whisper-server-cuda.exe"
            } else {
                "jf-whisper-server-cuda"
            };
            let exe_path = manager.base_dir.join("cuda").join(&ptr.build_id).join(exe_name);
            if exe_path.exists() {
                state_inner.artifact = ArtifactPhase::Installed;
            }
        }
        let state = Arc::new(Mutex::new(state_inner));
        let (tx, mut rx) = tokio::sync::mpsc::channel::<Command>(MAILBOX_CAPACITY);

        let actor_state = Arc::clone(&state);
        // Sender-Klon für die Addon-Blocking-Tasks (tx selbst geht in den Loop).
        let tx_for_ops = tx.clone();
        // Manager-Felder vor dem Spawn extrahieren (Self wird am Ende zurückgegeben).
        let mgr_base_dir = manager.base_dir.clone();
        let mgr_app_version = manager.app_version.clone();
        let mgr_build_id = manager.build_id.clone();
        // JFW-12 Block (g): Steps-Factory für die Switch-Blocking-Tasks.
        let switch_steps_for_ops = switch_steps.as_ref().map(Arc::clone);
        tauri::async_runtime::spawn(async move {
            while let Some(cmd) = rx.recv().await {
                match cmd {
                    Command::BootStarted => {
                        apply(&app, &actor_state, |st| {
                            if !matches!(st.runtime, RuntimePhase::BootingCpu) {
                                st.runtime = RuntimePhase::BootingCpu;
                            }
                        });
                    }
                    Command::BootFailed(reason) => {
                        // B2: Jede Fehlerphase → CpuReady(generation) oder NoBackendReady.
                        apply(&app, &actor_state, |st| {
                            st.runtime = RuntimePhase::NoBackendReady(reason);
                            st.active_lease = None;
                        });
                    }
                    Command::Stopped => {
                        // Manueller Stopp: kein Fehler — die App bleibt in der
                        // selben Epoche und kann neu booten.
                        apply(&app, &actor_state, |st| {
                            if matches!(st.runtime, RuntimePhase::NoBackendReady(_)) {
                                return;
                            }
                            st.runtime = RuntimePhase::NoBackendReady(String::new());
                            st.active_lease = None;
                        });
                    }
                    Command::MarkCpuReady { instance } => {
                        let variant = instance.variant;
                        apply(&app, &actor_state, |st| {
                            // Generation innerhalb der Epoche monoton (B3).
                            let gen = st.next_generation();
                            let lease = ActiveLease {
                                app_epoch: st.app_epoch.clone(),
                                generation: gen,
                                backend_variant: variant,
                                model_contract_hash: model_contract_hash(variant),
                                sidecar_instance_id: format!("{}-{}", instance.pid, instance.creation_time_ms),
                            };
                            // JFW-12 Bugfix: Das Boot kann in CPU oder CUDA starten
                            // (main.rs wählt die Binary über den Current-Pointer). Die
                            // Ready-Phase muss zur Variante passen — sonst meldet die UI
                            // nach einem CUDA-Boot fälschlich "cpu".
                            let to = match variant {
                                BackendVariant::Cuda => RuntimePhase::CudaReady(gen),
                                BackendVariant::Cpu => RuntimePhase::CpuReady(gen),
                            };
                            if !runtime_transition(&st.runtime, &to) {
                                eprintln!(
                                    "supervisor: abgelehnte Transition {:?} -> Ready({gen})",
                                    st.runtime
                                );
                                return;
                            }
                            match variant {
                                BackendVariant::Cpu => st.cpu_instance = Some(instance),
                                BackendVariant::Cuda => st.cuda_instance = Some(instance),
                            }
                            st.active_lease = Some(lease);
                            st.runtime = to;
                        });
                    }
                    Command::RequestSwitch(target) => {
                        // JFW-12 Block (g): echte Switch-Entscheidung statt Fail-closed-Stub.
                        // Die reine Entscheidungsfunktion prüft Konflikt → Zielgleichheit →
                        // Readiness → Artefakt und liefert einen inhaltsfreien Grund bei
                        // Ablehnung (C). Bei Annahme wird die Operation angelegt und der
                        // Runtime-Automat in die Drain-Phase überführt; die Prozessschritte
                        // (Drain-Poll, Teardown, Zielstart, Readiness) laufen im Actor-Task.
                        let decision = {
                            let st = actor_state.lock().unwrap();
                            decide_switch(&st, target)
                        };
                        match decision {
                            SwitchDecision::Reject(reason) => {
                                eprintln!("supervisor: Switch abgelehnt ({target:?}): {reason}");
                            }
                            SwitchDecision::Admit(direction) => {
                                let kind = match direction {
                                    crate::backend::switch_evidence::SwitchDirection::CpuToCuda => {
                                        OperationKind::SwitchToCuda
                                    }
                                    crate::backend::switch_evidence::SwitchDirection::CudaToCpu => {
                                        OperationKind::SwitchToCpu
                                    }
                                };
                                // JFW-12 Block (g): Ausgangs-PID für die VRAM-Evidence
                                // (B6.6–7) — vor der Transition lesen, weil SwitchTargetReady
                                // die Instanz aus dem State entfernt.
                                let old_cuda_pid = {
                                    let st = actor_state.lock().unwrap();
                                    match direction {
                                        crate::backend::switch_evidence::SwitchDirection::CudaToCpu => {
                                            st.cuda_instance.as_ref().map(|i| i.pid)
                                        }
                                        _ => None,
                                    }
                                };
                                let op_id = {
                                    apply(&app, &actor_state, |st| {
                                        let from_gen = st.runtime.active_generation().unwrap_or(0);
                                        let mut op = Operation::new(kind);
                                        // JFW-12 Block (h): Zielgeneration VOR dem Start
                                        // reservieren (B5.4 „Kandidatengeneration") — das
                                        // Journal bindet sie terminal.
                                        op.target_generation = Some(st.next_generation());
                                        // Switch-Evidenz eröffnen (Spec C, AC-F): inhaltsfrei;
                                        // der Switch-Blocking-Task schreibt das Journal.
                                        let evidence = crate::backend::switch_evidence::SwitchEvidence::new(
                                            op.operation_id.clone(),
                                            direction,
                                            st.app_epoch.clone(),
                                            from_gen,
                                        );
                                        st.evidence = Some(evidence);
                                        st.operation = Some(op.clone());
                                        // Drain-Phase: Admission ist jetzt geschlossen (B2).
                                        match direction {
                                            crate::backend::switch_evidence::SwitchDirection::CpuToCuda => {
                                                if !runtime_transition(&st.runtime, &RuntimePhase::DrainingCpu(op.operation_id.clone())) {
                                                    eprintln!("supervisor: abgelehnte Transition {:?} -> DrainingCpu", st.runtime);
                                                    return;
                                                }
                                                st.runtime = RuntimePhase::DrainingCpu(op.operation_id);
                                            }
                                            crate::backend::switch_evidence::SwitchDirection::CudaToCpu => {
                                                if !runtime_transition(&st.runtime, &RuntimePhase::DrainingCuda(op.operation_id.clone())) {
                                                    eprintln!("supervisor: abgelehnte Transition {:?} -> DrainingCuda", st.runtime);
                                                    return;
                                                }
                                                st.runtime = RuntimePhase::DrainingCuda(op.operation_id);
                                            }
                                        }
                                    });
                                    // op_id + reservierte Zielgeneration aus dem State lesen.
                                    actor_state.lock().unwrap().operation.as_ref().map(|o| (o.operation_id.clone(), o.target_generation))
                                };
                                let Some((op_id, to_generation)) = op_id else { continue; };
                                let to_generation = to_generation.unwrap_or(0);
                                // JFW-12 Block (h): Collector in den Switch-Blocking-Task
                                // überführen — die Evidenz wird entlang der echten Schritte
                                // geschrieben (Phasen, Prozessende, VRAM-Proben).
                                let evidence = actor_state.lock().unwrap().evidence.take();
                                let Some(evidence) = evidence else {
                                    let _ = tx_for_ops.try_send(Command::SwitchFailed {
                                        op_id,
                                        reason: "keine Switch-Evidenz eröffnet".into(),
                                    });
                                    continue;
                                };

                                // JFW-12 Block (g): Echte Prozessschritte im Blocking-Task.
                                // Die Sequenz + Rollback-Logik lebt in switch_driver::run_switch;
                                // die Closures liefern Drain/Teardown/Zielstart/Readiness/VRAM aus
                                // main.rs und melden den terminalen Zustand über die Mailbox.
                                let steps = match &switch_steps_for_ops {
                                    Some(s) => Arc::clone(s),
                                    None => {
                                        eprintln!("supervisor: Switch angenommen, aber keine Prozessschritte injiziert — fail-closed");
                                        let _ = tx_for_ops.try_send(Command::SwitchFailed {
                                            op_id: op_id.clone(),
                                            reason: "keine Switch-Prozessschritte injiziert".into(),
                                        });
                                        continue;
                                    }
                                };
                                let tx2 = tx_for_ops.clone();
                                tauri::async_runtime::spawn_blocking(move || {
                                    use crate::backend::switch_driver::{
                                        run_switch_steps, SwitchContext, SwitchOutcome,
                                    };
                                    use crate::backend::switch_evidence::SwitchResult;
                                    let ctx = SwitchContext { op_id: op_id.clone(), direction };
                                    // JFW-12 Block (h): kompletter Prozesszyklus
                                    // Drain → Teardown (Job-Object-Kill) → Zielstart →
                                    // Readiness-Smoke → VRAM-Evidence → Ready, fail-closed
                                    // Rollback bei jedem Schrittfehler (B5.8/B6.8).
                                    let (outcome, collector) = run_switch_steps(
                                        &ctx,
                                        steps,
                                        evidence,
                                        to_generation,
                                        old_cuda_pid,
                                        std::time::Duration::from_secs(1),
                                        || { send_with_timeout(&tx2, Command::SwitchDrained { op_id: op_id.clone() }); },
                                        |instance| { send_with_timeout(&tx2, Command::SwitchTargetReady { op_id: op_id.clone(), instance }); },
                                        |reason| { send_with_timeout(&tx2, Command::SwitchFailed { op_id: op_id.clone(), reason }); },
                                    );
                                    let result = match &outcome {
                                        SwitchOutcome::Completed => SwitchResult::Success,
                                        SwitchOutcome::Failed(r) => SwitchResult::Failure(r.clone()),
                                    };
                                    let probe_count = collector.vram_probes().len();
                                    let path = collector.finish(result);
                                    eprintln!(
                                        "supervisor: Switch-Journal geschrieben ({}; {} VRAM-Proben)",
                                        path.display(),
                                        probe_count
                                    );
                                });
                            }
                        }
                    }
                    // ── JFW-12 Block (g): Switch-Prozessschritte aus main.rs zurück ──
                    Command::SwitchDrained { op_id } => {
                        apply(&app, &actor_state, |st| {
                            // Richtung ist in der Drain-Phase kodiert (B2).
                            let to = match &st.runtime {
                                RuntimePhase::DrainingCpu(o) if *o == op_id => {
                                    RuntimePhase::PreparingCuda(o.clone())
                                }
                                RuntimePhase::DrainingCuda(o) if *o == op_id => {
                                    RuntimePhase::PreparingCpu(o.clone())
                                }
                                _ => return, // Op-Wechsel/fremde Operation — hart abgelehnt.
                            };
                            if !runtime_transition(&st.runtime, &to) {
                                eprintln!("supervisor: abgelehnte Transition {:?} -> {:?}", st.runtime, to);
                                return;
                            }
                            st.runtime = to;
                        });
                    }
                    Command::SwitchTargetReady { op_id, instance } => {
                        apply(&app, &actor_state, |st| {
                            // Zielvariante aus der Vorbereitungsphase (B2).
                            let variant = match &st.runtime {
                                RuntimePhase::PreparingCuda(o) if *o == op_id => BackendVariant::Cuda,
                                RuntimePhase::PreparingCpu(o) if *o == op_id => BackendVariant::Cpu,
                                _ => return,
                            };
                            // JFW-12 Block (h): Die Zielgeneration wurde beim Admit
                            // reserviert (B5.4) und ist im Journal des Switch-Tasks
                            // gebunden; nur der Notfall reserviert hier neu.
                            let reserved = st.operation.as_ref().and_then(|o| o.target_generation);
                            let gen = match reserved {
                                Some(g) => g,
                                None => st.next_generation(),
                            };
                            let lease = ActiveLease {
                                app_epoch: st.app_epoch.clone(),
                                generation: gen,
                                backend_variant: variant,
                                model_contract_hash: model_contract_hash(variant),
                                sidecar_instance_id: format!("{}-{}", instance.pid, instance.creation_time_ms),
                            };
                            let to = match variant {
                                BackendVariant::Cpu => RuntimePhase::CpuReady(gen),
                                BackendVariant::Cuda => RuntimePhase::CudaReady(gen),
                            };
                            if !runtime_transition(&st.runtime, &to) {
                                eprintln!("supervisor: abgelehnte Transition {:?} -> {:?}", st.runtime, to);
                                return;
                            }
                            // Nur das aktive Backend hält eine Live-Instanz (C).
                            match variant {
                                BackendVariant::Cpu => {
                                    st.cpu_instance = Some(instance);
                                    st.cuda_instance = None;
                                }
                                BackendVariant::Cuda => {
                                    st.cuda_instance = Some(instance);
                                    st.cpu_instance = None;
                                }
                            }
                            st.active_lease = Some(lease);
                            st.operation = None; // Switch abgeschlossen.
                            st.runtime = to;
                            // JFW-12 Block (h): Das Journal schreibt terminal der
                            // Switch-Blocking-Task (Evidenz entlang der echten
                            // Prozessschritte, atomar auf das echte Laufwerk).
                        });
                    }
                    Command::SwitchFailed { op_id, reason } => {
                        apply(&app, &actor_state, |st| {
                            let is_ours = matches!(
                                st.runtime,
                                RuntimePhase::DrainingCpu(ref o)
                                    | RuntimePhase::DrainingCuda(ref o)
                                    | RuntimePhase::PreparingCuda(ref o)
                                    | RuntimePhase::PreparingCpu(ref o)
                                    if *o == op_id
                            );
                            if !is_ours {
                                eprintln!("supervisor: SwitchFailed für fremde/abgeschlossene Operation ignoriert ({op_id})");
                                return;
                            }
                            // Fail-closed (B2): jeder Fehler → NoBackendReady, sichtbar.
                            st.runtime = RuntimePhase::NoBackendReady(reason.clone());
                            st.active_lease = None;
                            st.operation = None;
                            // JFW-12 Block (h): Auch das Fehlerjournal schreibt der
                            // Switch-Blocking-Task terminal (Grund inhaltsfrei).
                        });
                    }
                    Command::Shutdown => {
                        apply(&app, &actor_state, |st| {
                            if matches!(st.runtime.active_variant(), Some(BackendVariant::Cuda)) {
                                let op = Operation::new(OperationKind::SwitchToCpu);
                                st.operation = Some(op.clone());
                                st.active_lease = None;
                                st.runtime = RuntimePhase::StoppingCuda(op.operation_id.clone());
                            } else {
                                st.runtime = RuntimePhase::NoBackendReady("app_shutdown".into());
                            }
                        });
                    }
                    // ── Block (d): Addon-Lifecycle, ausschließlich nutzerinitiiert ──
                    Command::InstallAddon | Command::RepairAddon => {
                        let kind = match cmd {
                            Command::InstallAddon => OperationKind::InstallAddon,
                            _ => OperationKind::RepairAddon,
                        };
                        // Konfliktmatrix (B2): genau eine Lifecycle-Operation.
                        let conflict = {
                            let st = actor_state.lock().unwrap();
                            operation_conflict(&st.operation, kind)
                                .err()
                                .or_else(|| {
                                    if !matches!(st.artifact, ArtifactPhase::NotInstalled | ArtifactPhase::RepairRequired | ArtifactPhase::Installed) {
                                        Some(format!("Artefakt in Phase {} — Operation nicht möglich", st.artifact.as_str()))
                                    } else {
                                        None
                                    }
                                })
                        };
                        if let Some(err) = conflict {
                            eprintln!("supervisor: Addon-Operation abgelehnt: {err}");
                            continue;
                        }
                        apply(&app, &actor_state, |st| {
                            st.operation = Some(Operation::new(kind));
                            st.artifact_error = None;
                            st.artifact = ArtifactPhase::Downloading;
                        });
                        // Blocking-Pipeline im eigenen Task; Phasenupdates laufen
                        // zurück durch die Mailbox (Actor bleibt seriell).
                        let manager = Manager::new(
                            mgr_base_dir.clone(),
                            mgr_app_version.clone(),
                            mgr_build_id.clone(),
                        );
                        let tx2 = tx_for_ops.clone();
                        tauri::async_runtime::spawn_blocking(move || {
                            let progress = |phase: &str| {
                                let p = match phase {
                                    "downloading" => ArtifactPhase::Downloading,
                                    "verifying_manifest" | "verifying_signature" => ArtifactPhase::Verifying,
                                    _ => return, // extracting/committing → Staged folgt unten
                                };
                                send_with_timeout(&tx2, Command::ArtifactPhaseUpdate { phase: p });
                            };
                            let result = if kind == OperationKind::InstallAddon {
                                manager.install_release(progress)
                            } else {
                                manager.repair(progress)
                            };
                match result {
                    Ok(build) => {
                        send_with_timeout(&tx2, Command::ArtifactPhaseUpdate { phase: ArtifactPhase::Staged });
                        eprintln!("supervisor: Addon-Operation {kind:?} fertig — Build {}", build.build_id);
                        send_with_timeout(&tx2, Command::ArtifactOperationDone { kind });
                    }
                    Err(reason) => {
                        send_with_timeout(&tx2, Command::ArtifactOperationFailed { kind, reason });
                    }
                }
            });
                    }
                    Command::RemoveAddon => {
                        let conflict = {
                            let st = actor_state.lock().unwrap();
                            operation_conflict(&st.operation, OperationKind::RemoveAddon)
                                .err()
                                .or_else(|| {
                                    if !matches!(st.artifact, ArtifactPhase::NotInstalled | ArtifactPhase::RepairRequired | ArtifactPhase::Installed) {
                                        Some(format!("Artefakt in Phase {} — Remove nicht möglich", st.artifact.as_str()))
                                    } else {
                                        None
                                    }
                                })
                        };
                        if let Some(err) = conflict {
                            eprintln!("supervisor: Addon-Remove abgelehnt: {err}");
                            continue;
                        }
                        apply(&app, &actor_state, |st| {
                            st.operation = Some(Operation::new(OperationKind::RemoveAddon));
                            st.artifact = ArtifactPhase::Removing;
                        });
                        let manager = Manager::new(
                            mgr_base_dir.clone(),
                            mgr_app_version.clone(),
                            mgr_build_id.clone(),
                        );
                        let tx2 = tx_for_ops.clone();
                        tauri::async_runtime::spawn_blocking(move || {
                            match manager.remove() {
                                Ok(()) => {
                                    send_with_timeout(&tx2, Command::ArtifactOperationDone { kind: OperationKind::RemoveAddon });
                                }
                                Err(reason) => {
                                    send_with_timeout(&tx2, Command::ArtifactOperationFailed { kind: OperationKind::RemoveAddon, reason });
                                }
                            }
                        });
                    }
                    Command::ArtifactPhaseUpdate { phase } => {
                        apply(&app, &actor_state, |st| {
                            if st.artifact == phase {
                                return; // gleiche Phase (z. B. zweites "downloading") — No-op
                            }
                            if artifact_transition(&st.artifact, &phase) {
                                st.artifact = phase;
                            } else {
                                eprintln!(
                                    "supervisor: abgelehnte Artefakt-Transition {} -> {}",
                                    st.artifact.as_str(),
                                    phase.as_str()
                                );
                            }
                        });
                    }
                    Command::ArtifactOperationDone { kind } => {
                        apply(&app, &actor_state, |st| {
                            let to = match kind {
                                OperationKind::RemoveAddon => ArtifactPhase::NotInstalled,
                                _ => ArtifactPhase::Installed,
                            };
                            if artifact_transition(&st.artifact, &to) {
                                st.artifact = to;
                            } else {
                                eprintln!(
                                    "supervisor: abgelehnte Artefakt-Transition {} -> {} (Operation {kind:?} fertig)",
                                    st.artifact.as_str(),
                                    to.as_str()
                                );
                            }
                            st.operation = None;
                            st.artifact_error = None; // Erfolg: Fehlergrund geklärt
                        });
                    }
                    Command::ArtifactOperationFailed { kind, reason } => {
                        apply(&app, &actor_state, |st| {
                            // Fail-closed (B9): die bisher bestätigte Version bleibt
                            // bis zum grünen Commit erhalten → RepairRequired; ohne
                            // installierte Version zurück auf NotInstalled.
                            let has_confirmed = Manager::new(
                                mgr_base_dir.clone(),
                                mgr_app_version.clone(),
                                mgr_build_id.clone(),
                            )
                            .current()
                            .map(|c| c.is_some())
                            .unwrap_or(false);
                            let to = if has_confirmed {
                                ArtifactPhase::RepairRequired
                            } else {
                                ArtifactPhase::NotInstalled
                            };
                            eprintln!("supervisor: Addon-Operation {kind:?} fehlgeschlagen: {reason}");
                            st.artifact_error = Some(reason.clone());
                            if artifact_transition(&st.artifact, &to) {
                                st.artifact = to;
                            }
                            st.operation = None;
                        });
                    }
                }
            }
        });

        Self { tx, state, manager, switch_steps }
    }

    /// Vor dem CPU-Spawn aufrufen (bestehender Pfad in main.rs).
    pub fn boot_started(&self) {
        let _ = self.tx.try_send(Command::BootStarted);
    }

    /// Boot fehlgeschlagen — B2: Fehlerphase → NoBackendReady(reason).
    pub fn boot_failed(&self, reason: String) {
        let _ = self.tx.try_send(Command::BootFailed(reason));
    }

    /// Manueller Stopp über `stop_server` — Runtime zurück nach NoBackendReady.
    pub fn stopped(&self) {
        let _ = self.tx.try_send(Command::Stopped);
    }

    /// Nach grünem Handshake: Prozessidentität + Variante binden, Generation
    /// erhöhen, Lease aktivieren, Admission öffnen.
    pub fn mark_cpu_ready(&self, instance: SidecarInstance) {
        let _ = self.tx.try_send(Command::MarkCpuReady { instance });
    }

    /// Typisierter Switch-Request (GPU-Seite). Die Entscheidung läuft im Actor;
    /// bei Annahme wird die Operation angelegt und der Runtime in die Drain-Phase
    /// überführt. Prozessschritte werden via `switch_drained` / `switch_target_ready`
    /// / `switch_failed` zurückgemeldet.
    pub fn request_switch(&self, target: BackendVariant) {
        let _ = self.tx.try_send(Command::RequestSwitch(target));
    }

    // ── JFW-12 Block (g): Switch-Prozessschritte aus main.rs zurück ──

    /// Drain + Teardown des Ausgangs-Backends bestätigt → Ziel-Vorbereitung.
    pub fn switch_drained(&self, op_id: String) {
        let _ = self.tx.try_send(Command::SwitchDrained { op_id });
    }

    /// Ziel-Backend gestartet + Handshake grün → Ready mit neuer Generation.
    pub fn switch_target_ready(&self, op_id: String, instance: SidecarInstance) {
        let _ = self.tx.try_send(Command::SwitchTargetReady { op_id, instance });
    }

    /// Switch fehlgeschlagen → fail-closed NoBackendReady (sichtbar).
    pub fn switch_failed(&self, op_id: String, reason: String) {
        let _ = self.tx.try_send(Command::SwitchFailed { op_id, reason });
    }

    // ── JFW-12 Block (d): Addon-Lifecycle, ausschließlich nutzerinitiiert ──

    /// Nutzerstart: signiertes CUDA-Addon aus dem eingebetteten Releasepfad
    /// installieren (B9). Die Pipeline läuft im Blocking-Task; Phasenupdates
    /// kommen über die Mailbox zurück.
    pub fn install_addon(&self) {
        let _ = self.tx.try_send(Command::InstallAddon);
    }

    /// Nutzerstart: defektes Build neu installieren (dieselbe Pipeline wie Install).
    pub fn repair_addon(&self) {
        let _ = self.tx.try_send(Command::RepairAddon);
    }

    /// Nutzerstart: installierte Builds + Current-Pointer entfernen.
    pub fn remove_addon(&self) {
        let _ = self.tx.try_send(Command::RemoveAddon);
    }

    /// App-Ende / Suspend: Admission schließen. Wird in Block (c) an den
    /// Prozess-Lifecycle (Job-Object-Schließung, B8) angehängt.
    #[allow(dead_code)] // JFW-12 Block (c): Process-Shutdown-Hook
    pub fn shutdown(&self) {
        let _ = self.tx.try_send(Command::Shutdown);
    }

    /// Typisierter Read (Command `supervisor_snapshot`): keine Tokens, keine
    /// freien Ports jenseits des aktiven Endpunkts, keine Kommandozeilen (C).
    pub fn snapshot(&self) -> SupervisorSnapshot {
        self.state.lock().unwrap().snapshot()
    }

    /// Admission-Entscheidung für einen neuen Job (B5 Schritt 1): wird vom
    /// Frontend-Client vor produktiven Requests abgefragt.
    pub fn admit(&self, requested_generation: Option<u64>) -> AdmissionOutcome {
        let st = self.state.lock().unwrap();
        match admission::admit(&st, requested_generation) {
            admission::AdmissionDecision::Admitted(permit) => AdmissionOutcome {
                admitted: true,
                generation: Some(permit.generation),
                backend_variant: Some(permit.backend_variant.as_str()),
                reason: String::new(),
            },
            admission::AdmissionDecision::Closed(reason) => AdmissionOutcome {
                admitted: false,
                generation: None,
                backend_variant: None,
                reason,
            },
            admission::AdmissionDecision::StaleGeneration { requested, active } => AdmissionOutcome {
                admitted: false,
                generation: Some(active),
                backend_variant: None,
                reason: format!("stale_generation:{requested}!={active}"),
            },
        }
    }

    /// Generationenfence für eingehende Responses (B7). Wird in Block (c) an
    /// die Sidecar-Response-Pfade angehängt; bis dahin Teil der API-Fläche.
    #[allow(dead_code)] // JFW-12 Block (c): Response-Fence-Hook
    pub fn response_fence(&self, epoch: &str, generation: u64) -> bool {
        admission::response_fence(&self.state.lock().unwrap(), epoch, generation)
    }
}

/// Mutation + Event in einem Schritt; der Guard wird vor `emit` fallen gelassen
/// (nicht-reentrants Mutex — Doppellock wäre ein Deadlock).
fn apply(
    app: &AppHandle,
    state: &Mutex<BackendSupervisorState>,
    f: impl FnOnce(&mut BackendSupervisorState),
) {
    let mut st = state.lock().unwrap();
    f(&mut st);
    drop(st);
    emit(app, state);
}

/// Modellvertrag-Hash des aktiven Builds. Block (b): deterministisch aus der
/// Schema-ID; Block (d) bindet den vollständigen Manifest-Vertrag.
fn model_contract_hash(variant: BackendVariant) -> String {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    "jfwhisper-v2".hash(&mut h);
    variant.as_str().hash(&mut h);
    format!("{:016x}", h.finish())
}

fn emit(app: &AppHandle, state: &Mutex<BackendSupervisorState>) {
    let snap = state.lock().unwrap().snapshot();
    if let Err(e) = app.emit(SUPERVISOR_EVENT, &snap) {
        eprintln!("supervisor: Event-Emission fehlgeschlagen: {e}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn contract_hash_is_deterministic_and_variant_bound() {
        assert_eq!(model_contract_hash(BackendVariant::Cpu), model_contract_hash(BackendVariant::Cpu));
        assert_ne!(model_contract_hash(BackendVariant::Cpu), model_contract_hash(BackendVariant::Cuda));
    }

    #[test]
    fn mailbox_capacity_is_bounded() {
        // Begrenzte Mailbox (B2): Inferenz-/Audiobytes laufen nicht durch sie.
        assert!(MAILBOX_CAPACITY > 0 && MAILBOX_CAPACITY <= 1024);
    }
}
