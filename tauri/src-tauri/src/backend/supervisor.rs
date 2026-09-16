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
use crate::backend::state::*;

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
    /// App-Ende: Admission schließen; aktive CUDA-Generation invalidieren (B8).
    Shutdown,
}

/// Tauri-State: Mailbox-Handle + geteilter Read-Snapshot. Alle Mutationen laufen
/// ausschließlich im Actor; `state` dient nur typisierten Reads (Snapshot).
pub struct Supervisor {
    tx: tokio::sync::mpsc::Sender<Command>,
    state: Arc<Mutex<BackendSupervisorState>>,
}

impl Clone for Supervisor {
    fn clone(&self) -> Self {
        Self {
            tx: self.tx.clone(),
            state: Arc::clone(&self.state),
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
    /// Window/Command verfügbar sein (Tauri `setup`).
    pub fn start(app: AppHandle) -> Self {
        let app_epoch = new_app_epoch();
        let state = Arc::new(Mutex::new(BackendSupervisorState::new(app_epoch.clone())));
        let (tx, mut rx) = tokio::sync::mpsc::channel::<Command>(MAILBOX_CAPACITY);

        let actor_state = Arc::clone(&state);
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
                        apply(&app, &actor_state, |st| {
                            match target {
                                // Fail-closed (B2/B9): In Block (b) gibt es keinen
                                // produktiven CUDA-Pfad — weder installiertes Addon
                                // noch Operation-Lifecycle. Der Request wird sichtbar
                                // abgelehnt; Block (d) liefert Artefakt + Switch.
                                BackendVariant::Cuda => {
                                    eprintln!(
                                        "supervisor: SwitchToCuda fail-closed — Artefakt {} (Block d ausstehend)",
                                        st.artifact.as_str()
                                    );
                                }
                                // CPU ist immer Zielmodus (B3). Bereits auf CPU → no-op.
                                BackendVariant::Cpu => {
                                    if !matches!(st.runtime, RuntimePhase::CpuReady(_)) {
                                        eprintln!(
                                            "supervisor: SwitchToCpu ignoriert — kein aktiver CUDA-Drain in Block (b) (Zustand {:?})",
                                            st.runtime
                                        );
                                    }
                                }
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
                }
            }
        });

        Self { tx, state }
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

    /// Typisierter Switch-Request (GPU-Seite). CUDA ist fail-closed bis Block (d).
    pub fn request_switch(&self, target: BackendVariant) {
        let _ = self.tx.try_send(Command::RequestSwitch(target));
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
