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
        let state = Arc::new(Mutex::new(BackendSupervisorState::new(app_epoch.clone())));
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
                            let to = RuntimePhase::CpuReady(gen);
                            if !runtime_transition(&st.runtime, &to) {
                                eprintln!(
                                    "supervisor: abgelehnte Transition {:?} -> CpuReady({gen})",
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
                                        let op = Operation::new(kind);
                                        // Switch-Evidenz eröffnen (Spec C, AC-F): inhaltsfrei;
                                        // wird beim terminalen Schritt abgeschlossen + Journal.
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
                                    // op_id aus dem State lesen (Operation ist gesetzt).
                                    actor_state.lock().unwrap().operation.as_ref().map(|o| o.operation_id.clone())
                                };
                                let Some(op_id) = op_id else { return; };

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
                                        return;
                                    }
                                };
                                let tx2 = tx_for_ops.clone();
                                tauri::async_runtime::spawn_blocking(move || {
                                    use crate::backend::switch_driver::{run_switch, SwitchContext};
                                    let ctx = SwitchContext { op_id: op_id.clone(), direction };
                                    run_switch(
                                        &ctx,
                                        || steps.drain(direction),
                                        || steps.teardown_source(direction),
                                        || steps.start_target(direction),
                                        |i| steps.readiness_smoke(i),
                                        if matches!(direction, crate::backend::switch_evidence::SwitchDirection::CudaToCpu) {
                                            // Eigener Arc-Klon: die Box-Closure ist 'static und
                                            // darf den von den anderen Closures geborrowten `steps`
                                            // nicht weg-moven.
                                            let vram_steps = Arc::clone(&steps);
                                            Some(Box::new(move |_: &SidecarInstance| {
                                                vram_steps.vram_evidence(old_cuda_pid)
                                            }))
                                        } else {
                                            None
                                        },
                                        |i| steps.teardown_target(i),
                                        || { let _ = tx2.try_send(Command::SwitchDrained { op_id: op_id.clone() }); },
                                        |instance| { let _ = tx2.try_send(Command::SwitchTargetReady { op_id: op_id.clone(), instance }); },
                                        |reason| { let _ = tx2.try_send(Command::SwitchFailed { op_id: op_id.clone(), reason }); },
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
                            let gen = st.next_generation();
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
                            // Evidenz abschließen: Ziel-Generation binden + Journal atomar.
                            if let Some(mut ev) = st.evidence.take() {
                                ev.set_to_generation(gen);
                                let path = ev.finish(
                                    crate::backend::switch_evidence::SwitchResult::Success,
                                );
                                eprintln!("supervisor: Switch-Journal geschrieben ({})", path.display());
                            }
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
                            // Evidenz abschließen: Journal mit Fehlergrund (inhaltsfrei).
                            if let Some(ev) = st.evidence.take() {
                                let path = ev.finish(
                                    crate::backend::switch_evidence::SwitchResult::Failure(reason),
                                );
                                eprintln!("supervisor: Switch-Journal geschrieben ({})", path.display());
                            }
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
                                let _ = tx2.try_send(Command::ArtifactPhaseUpdate { phase: p });
                            };
                            let result = if kind == OperationKind::InstallAddon {
                                manager.install_release(progress)
                            } else {
                                manager.repair(progress)
                            };
                match result {
                    Ok(build) => {
                        let _ = tx2.try_send(Command::ArtifactPhaseUpdate { phase: ArtifactPhase::Staged });
                        eprintln!("supervisor: Addon-Operation {kind:?} fertig — Build {}", build.build_id);
                        let _ = tx2.try_send(Command::ArtifactOperationDone { kind });
                    }
                    Err(reason) => {
                        let _ = tx2.try_send(Command::ArtifactOperationFailed { kind, reason });
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
                                    let _ = tx2.try_send(Command::ArtifactOperationDone { kind: OperationKind::RemoveAddon });
                                }
                                Err(reason) => {
                                    let _ = tx2.try_send(Command::ArtifactOperationFailed { kind: OperationKind::RemoveAddon, reason });
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
