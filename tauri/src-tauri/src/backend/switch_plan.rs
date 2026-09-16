//! JFW-12 Block (g): Switch-Entscheidung und -Planung (Spec B5/B6, AC-G).
//!
//! Reine, I/O-freie Entscheidungsfunktion des Idle-Switches: sie prüft gegen die
//! aktuelle Runtimewahrheit, ob ein Backendwechsel angenommen werden darf, und
//! liefert einen typisierten Plan. Der serielle Actor führt den Plan aus; diese
//! Funktion selbst berührt weder Netzwerk noch Prozesse — dadurch ist sie hier
//! unit-testbar, während der eigentliche Wechsel (Drain/Teardown/Start/Readiness/
//! VRAM) im Blocking-Task des Actors läuft und auf dem Zielsystem E2E geprüft wird.

use crate::backend::state::{
    operation_conflict, BackendSupervisorState, BackendVariant, OperationKind, RuntimePhase,
};
use crate::backend::switch_evidence::SwitchDirection;

/// Typisierte Switch-Entscheidung (B5 Schritt 1–2).
#[derive(Debug, Clone, PartialEq)]
pub enum SwitchDecision {
    /// Wechsel darf angenommen werden; Richtung ist bindend.
    Admit(SwitchDirection),
    /// Wechsel wird sichtbar abgelehnt (fail-closed). Grund inhaltsfrei.
    Reject(String),
}

impl SwitchDecision {
    pub fn is_admitted(&self) -> bool {
        matches!(self, SwitchDecision::Admit(_))
    }
}

/// Operationstyp für eine Zielvariante.
fn kind_for(target: BackendVariant) -> OperationKind {
    match target {
        BackendVariant::Cuda => OperationKind::SwitchToCuda,
        BackendVariant::Cpu => OperationKind::SwitchToCpu,
    }
}

/// Reine Switch-Entscheidung (B5 Schritt 1–2). Die Prüfungsreihenfolge ist bindend:
/// Konflikt → Zielgleichheit → Readiness → Artefakt. Jede Ablehnung liefert einen
/// inhaltsfreien Grund (kein Audio/Transkript, C).
pub fn decide_switch(st: &BackendSupervisorState, target: BackendVariant) -> SwitchDecision {
    // 1) Konfliktmatrix (B2): genau eine Lifecycle-Operation darf laufen.
    if let Err(reason) = operation_conflict(&st.operation, kind_for(target)) {
        return SwitchDecision::Reject(reason);
    }

    // 2) Bereits auf Zielvariante → no-op (kein Wechsel nötig).
    if st.runtime.active_variant() == Some(target) {
        return SwitchDecision::Reject(format!("bereits aktiv: {}", target.as_str()));
    }

    // 3) Readiness: nur aus einem Ready-Zustand darf geschaltet werden.
    match &st.runtime {
        RuntimePhase::CpuReady(_) | RuntimePhase::CudaReady(_) => {}
        other => return SwitchDecision::Reject(format!("kein Ready-Zustand: {:?}", other)),
    }

    // 4) Richtung + Artefakt-Prüfung (B9).
    match target {
        BackendVariant::Cuda => {
            if !st.artifact.is_installed() {
                return SwitchDecision::Reject(format!(
                    "CUDA-Artefakt nicht installiert (Phase {})",
                    st.artifact.as_str()
                ));
            }
            SwitchDecision::Admit(SwitchDirection::CpuToCuda)
        }
        // CPU ist immer Zielmodus (B3): kein Artefakt nötig.
        BackendVariant::Cpu => SwitchDecision::Admit(SwitchDirection::CudaToCpu),
    }
}

/// Drain-Abbruchbedingung: der Wechsel darf in die Teardown-Phase, sobald keine
/// aktive Arbeit mehr läuft (`/tasks/active` liefert 0). Reine Funktion.
pub fn drain_complete(active_tasks: usize) -> bool {
    active_tasks == 0
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backend::state::{ArtifactPhase, Operation};

    fn ready_cpu() -> BackendSupervisorState {
        let mut st = BackendSupervisorState::new("epoch-1".into());
        st.runtime = RuntimePhase::CpuReady(1);
        st.artifact = ArtifactPhase::Installed;
        st
    }

    #[test]
    fn cpu_to_cuda_admitted_when_artifact_installed() {
        let st = ready_cpu();
        assert_eq!(
            decide_switch(&st, BackendVariant::Cuda),
            SwitchDecision::Admit(SwitchDirection::CpuToCuda)
        );
    }

    #[test]
    fn cpu_to_cuda_rejected_when_artifact_missing() {
        let mut st = ready_cpu();
        st.artifact = ArtifactPhase::NotInstalled;
        let d = decide_switch(&st, BackendVariant::Cuda);
        assert!(!d.is_admitted());
        if let SwitchDecision::Reject(r) = &d {
            assert!(r.contains("nicht installiert"));
        } else {
            panic!("erwartete Ablehnung");
        }
    }

    #[test]
    fn cuda_to_cpu_always_admitted_from_cuda_ready() {
        let mut st = ready_cpu();
        st.runtime = RuntimePhase::CudaReady(2);
        assert_eq!(
            decide_switch(&st, BackendVariant::Cpu),
            SwitchDecision::Admit(SwitchDirection::CudaToCpu)
        );
    }

    #[test]
    fn same_target_is_noop_reject() {
        let st = ready_cpu();
        if let SwitchDecision::Reject(r) = decide_switch(&st, BackendVariant::Cpu) {
            assert!(r.contains("bereits aktiv"));
        } else {
            panic!("erwartete No-op-Ablehnung");
        }
    }

    #[test]
    fn non_ready_state_rejected() {
        let mut st = ready_cpu();
        st.runtime = RuntimePhase::BootingCpu;
        assert!(!decide_switch(&st, BackendVariant::Cuda).is_admitted());
    }

    #[test]
    fn identical_running_op_is_conflict() {
        let mut st = ready_cpu();
        st.operation = Some(Operation::new(OperationKind::SwitchToCuda));
        if let SwitchDecision::Reject(r) = decide_switch(&st, BackendVariant::Cuda) {
            assert!(r.contains("identische Operation"));
        } else {
            panic!("erwartete Konflikt-Ablehnung");
        }
    }

    #[test]
    fn different_running_op_is_conflict() {
        let mut st = ready_cpu();
        st.operation = Some(Operation::new(OperationKind::InstallAddon));
        assert!(!decide_switch(&st, BackendVariant::Cuda).is_admitted());
    }

    #[test]
    fn drain_complete_only_at_zero() {
        assert!(drain_complete(0));
        assert!(!drain_complete(3));
    }
}
