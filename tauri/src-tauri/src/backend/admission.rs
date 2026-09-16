//! JFW-12 B3/B5: Admission — JobPermit und Generationenfence.
//!
//! Admission ist nur in einem Ready-Zustand offen (B2). Ein Permit bindet die
//! aktuelle `(app_epoch, generation)`; ein Sidecar prüft den Lease selbst (B7),
//! Rust prüft hier zusätzlich, dass der Request zur aktiven Generation gehört.
//! Späte Responses alter Generationen werden protokolliert und verworfen.

use crate::backend::state::{BackendSupervisorState, BackendVariant};

/// Ergebnis einer Admission-Entscheidung.
#[derive(Debug, Clone, PartialEq)]
pub enum AdmissionDecision {
    /// Request wird angenommen; der Permit trägt die bindende Generation.
    Admitted(JobPermit),
    /// Admission ist geschlossen (kein Ready-Zustand oder Operation läuft).
    Closed(String),
    /// Request gehört zu einer alten Generation — verworfen, nicht Fehler.
    StaleGeneration { requested: u64, active: u64 },
}

/// Einmaliger Permit für einen angenommenen Job (B5 Schritt 1).
#[derive(Debug, Clone, PartialEq)]
pub struct JobPermit {
    pub app_epoch: String,
    pub generation: u64,
    pub backend_variant: BackendVariant,
    /// Bindet den Modellvertrag des aktiven Leases (B3).
    pub model_contract_hash: String,
}

/// Prüft Admission für einen neuen Job-Request.
///
/// `requested_generation` ist die Generation, auf die der Client referenziert
/// (z. B. aus dem letzten Snapshot). Fehlt sie (`None`), wird die aktuelle
/// Generation verwendet — das ist der normale Pfad nach einem Ready-Signal.
pub fn admit(state: &BackendSupervisorState, requested_generation: Option<u64>) -> AdmissionDecision {
    let active = match state.runtime.active_variant() {
        Some(v) => v,
        None => return AdmissionDecision::Closed(format!(
            "Admission geschlossen: {:?} (kein Ready-Zustand)",
            state.runtime
        )),
    };

    if let Some(op) = &state.operation {
        // Während einer Lifecycle-Operation bleibt Admission zu (B5/B6).
        return AdmissionDecision::Closed(format!(
            "Admission geschlossen: Operation {} läuft",
            op.kind.as_str()
        ));
    }

    let active_gen = state.runtime.active_generation().unwrap_or(0);
    match requested_generation {
        Some(req) if req != active_gen => AdmissionDecision::StaleGeneration {
            requested: req,
            active: active_gen,
        },
        _ => {
            let lease = state
                .active_lease
                .as_ref()
                .expect("Ready-Zustand ohne Lease ist ein Invariantenbruch");
            AdmissionDecision::Admitted(JobPermit {
                app_epoch: state.app_epoch.clone(),
                generation: active_gen,
                backend_variant: active,
                model_contract_hash: lease.model_contract_hash.clone(),
            })
        }
    }
}

/// Generationenfence für eingehende Responses (B7): eine Response ist nur dann
/// gültig, wenn sie zur aktiven Epoche UND Generation gehört.
pub fn response_fence(
    state: &BackendSupervisorState,
    epoch: &str,
    generation: u64,
) -> bool {
    if epoch != state.app_epoch {
        return false; // Alte App-Sitzung — niemals gültig (B3).
    }
    match state.runtime.active_generation() {
        Some(active) => generation == active,
        None => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backend::state::{ActiveLease, OperationKind, RuntimePhase};

    fn ready_state(gen: u64) -> BackendSupervisorState {
        let mut st = BackendSupervisorState::new("epoch-1".into());
        st.runtime = RuntimePhase::CpuReady(gen);
        st.active_lease = Some(ActiveLease {
            app_epoch: "epoch-1".into(),
            generation: gen,
            backend_variant: BackendVariant::Cpu,
            model_contract_hash: "hash-1".into(),
            sidecar_instance_id: "inst-1".into(),
        });
        st
    }

    #[test]
    fn admits_in_ready_state() {
        let st = ready_state(3);
        match admit(&st, None) {
            AdmissionDecision::Admitted(p) => {
                assert_eq!(p.generation, 3);
                assert_eq!(p.backend_variant, BackendVariant::Cpu);
                assert_eq!(p.model_contract_hash, "hash-1");
            }
            other => panic!("erwartet Admitted, bekam {:?}", other),
        }
    }

    #[test]
    fn closed_while_operation_runs() {
        let mut st = ready_state(3);
        st.operation = Some(crate::backend::state::Operation::new(OperationKind::SwitchToCuda));
        assert!(matches!(admit(&st, None), AdmissionDecision::Closed(_)));
    }

    #[test]
    fn stale_generation_is_rejected_not_error() {
        let st = ready_state(5);
        match admit(&st, Some(4)) {
            AdmissionDecision::StaleGeneration { requested, active } => {
                assert_eq!((requested, active), (4, 5));
            }
            other => panic!("erwartet StaleGeneration, bekam {:?}", other),
        }
    }

    #[test]
    fn fence_rejects_old_epoch_and_generation() {
        let st = ready_state(7);
        assert!(response_fence(&st, "epoch-1", 7));
        assert!(!response_fence(&st, "alte-epoche", 7)); // B3: alte Sitzung
        assert!(!response_fence(&st, "epoch-1", 6)); // späte Response
    }

    #[test]
    fn closed_when_not_ready() {
        let mut st = BackendSupervisorState::new("e".into());
        st.runtime = RuntimePhase::BootingCpu;
        assert!(matches!(admit(&st, None), AdmissionDecision::Closed(_)));
    }
}
