//! JFW-12 B2/B3: Typisierte Zustände des BackendSupervisor.
//!
//! Zwei orthogonale Automaten, beide nur im RAM (Owner: der serielle Actor):
//! * RuntimeState  — BootingCpu → CpuReady(generation) → DrainingCpu(operation)
//!                    → PreparingCuda(operation) → CudaReady(generation) → …
//!                    Jede Fehlerphase → CpuReady(generation) oder NoBackendReady(reason).
//! * ArtifactState — NotInstalled → Downloading → Verifying → Staged → Installed
//!                    → RepairRequired / Removing.
//!
//! `app_epoch` ist pro Tauri-Prozess zufällig; der Generationszähler ist innerhalb
//! der Epoche monoton steigend (B3). Ein aktiver Lease bindet
//! `(app_epoch, generation, backend_variant, model_contract_hash, sidecar_instance_id)`.

use std::time::{SystemTime, UNIX_EPOCH};

/// Zufällige App-Epoche: grenzt alte Tauri-Prozesse und deren Responses ab (B3).
pub fn new_app_epoch() -> String {
    uuid::Uuid::new_v4().to_string()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackendVariant {
    Cpu,
    Cuda,
}

impl BackendVariant {
    pub const fn as_str(self) -> &'static str {
        match self {
            BackendVariant::Cpu => "cpu",
            BackendVariant::Cuda => "cuda",
        }
    }
}

/// Runtime-Zustandsautomat (B2). Jede Fehlerphase landet in `CpuReady` oder
/// `NoBackendReady`; es gibt keinen unsichtbaren Zwischenzustand.
#[derive(Debug, Clone, PartialEq)]
pub enum RuntimePhase {
    BootingCpu,
    CpuReady(u64),
    DrainingCpu(String),
    PreparingCuda(String),
    CudaReady(u64),
    DrainingCuda(String),
    PreparingCpu(String),
    StoppingCuda(String),
    VerifyingRelease(String),
    NoBackendReady(String),
}

impl RuntimePhase {
    /// Aktive Generation, falls eine Variante produktiv ist.
    pub fn active_generation(&self) -> Option<u64> {
        match self {
            RuntimePhase::CpuReady(g) | RuntimePhase::CudaReady(g) => Some(*g),
            _ => None,
        }
    }

    /// Produktive Variante des aktuellen Zustands.
    pub fn active_variant(&self) -> Option<BackendVariant> {
        match self {
            RuntimePhase::CpuReady(_) => Some(BackendVariant::Cpu),
            RuntimePhase::CudaReady(_) => Some(BackendVariant::Cuda),
            _ => None,
        }
    }

    /// Admission ist nur in einem Ready-Zustand offen (B2/B5).
    pub fn admission_open(&self) -> bool {
        matches!(self, RuntimePhase::CpuReady(_) | RuntimePhase::CudaReady(_))
    }
}

/// Artefakt-Zustandsautomat des CUDA-Addons (B2/B9). Block (b) hält ihn in
/// `NotInstalled`; Download/Verifizierung kommen mit dem Artefaktmanager.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArtifactPhase {
    NotInstalled,
    Downloading,
    Verifying,
    Staged,
    Installed,
    RepairRequired,
    Removing,
}

impl ArtifactPhase {
    pub const fn as_str(self) -> &'static str {
        match self {
            ArtifactPhase::NotInstalled => "not_installed",
            ArtifactPhase::Downloading => "downloading",
            ArtifactPhase::Verifying => "verifying",
            ArtifactPhase::Staged => "staged",
            ArtifactPhase::Installed => "installed",
            ArtifactPhase::RepairRequired => "repair_required",
            ArtifactPhase::Removing => "removing",
        }
    }

    pub const fn is_installed(self) -> bool {
        matches!(self, ArtifactPhase::Installed)
    }
}

/// Transition-Regelwerk des Artefakt-Automaten (B2/B9). Der Actor ruft dies vor
/// jeder Mutation auf; ungültige Sprünge sind hart abgelehnt. Fehlerziele:
/// `NotInstalled` (nichts war installiert) oder `RepairRequired` (eine
/// bestätigte Version bleibt nutzbar — AC 104).
pub fn artifact_transition(from: &ArtifactPhase, to: &ArtifactPhase) -> bool {
    use ArtifactPhase::*;
    match from {
        NotInstalled => matches!(to, Downloading),
        Downloading => matches!(to, Verifying | NotInstalled),
        Verifying => matches!(to, Staged | NotInstalled | RepairRequired),
        Staged => matches!(to, Installed | NotInstalled | RepairRequired),
        Installed => matches!(to, Removing | RepairRequired | Downloading),
        RepairRequired => matches!(to, Downloading | Removing),
        Removing => matches!(to, NotInstalled),
    }
}

/// Vollständige Prozessidentität einer Sidecarinstanz (B8): nicht nur PID.
#[derive(Debug, Clone)]
pub struct SidecarInstance {
    pub pid: u32,
    pub creation_time_ms: u128,
    /// Normalisierter Executable-Pfad (keine Tokens/Umgebung).
    pub executable_path: String,
    pub variant: BackendVariant,
    pub port: u16,
    /// Build-ID aus dem Handshake; fehlt im CPU-Handshake → leere Zeichenkette.
    pub build_id: String,
}

impl SidecarInstance {
    fn now_ms() -> u128 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0)
    }

    pub fn new(pid: u32, executable_path: String, variant: BackendVariant, port: u16, build_id: String) -> Self {
        Self {
            pid,
            creation_time_ms: Self::now_ms(),
            executable_path,
            variant,
            port,
            build_id,
        }
    }
}

/// Aktiver Lease (B3): bindet Epoche, Generation, Variante, Modellvertrag und
/// die konkrete Sidecarinstanz. Ein Standby besitzt keinen aktiven Lease.
#[derive(Debug, Clone)]
pub struct ActiveLease {
    pub app_epoch: String,
    pub generation: u64,
    pub backend_variant: BackendVariant,
    pub model_contract_hash: String,
    pub sidecar_instance_id: String,
}

/// Laufende Lifecycle-Operation (Switch/Install/Repair/Remove).
#[derive(Debug, Clone)]
pub struct Operation {
    pub operation_id: String,
    pub kind: OperationKind,
    /// Zielgeneration des Switches; `None`, bis sie reserviert ist.
    pub target_generation: Option<u64>,
}

impl Operation {
    pub fn new(kind: OperationKind) -> Self {
        Self {
            operation_id: uuid::Uuid::new_v4().to_string(),
            kind,
            target_generation: None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OperationKind {
    SwitchToCuda,
    SwitchToCpu,
    InstallAddon,
    RepairAddon,
    RemoveAddon,
}

impl OperationKind {
    pub const fn as_str(self) -> &'static str {
        match self {
            OperationKind::SwitchToCuda => "switch_to_cuda",
            OperationKind::SwitchToCpu => "switch_to_cpu",
            OperationKind::InstallAddon => "install_addon",
            OperationKind::RepairAddon => "repair_addon",
            OperationKind::RemoveAddon => "remove_addon",
        }
    }
}

/// Die einzige aktuelle Runtimewahrheit (C: nur RAM, kein Webview-Zugriff).
#[derive(Debug)]
pub struct BackendSupervisorState {
    pub app_epoch: String,
    /// Monotoner Generationszähler innerhalb der Epoche (B3).
    pub generation: u64,
    pub runtime: RuntimePhase,
    pub artifact: ArtifactPhase,
    /// Letzter Addon-Fehlergrund (fail-closed, B9); `None` nach Erfolg/Start.
    pub artifact_error: Option<String>,
    pub cpu_instance: Option<SidecarInstance>,
    pub cuda_instance: Option<SidecarInstance>,
    /// Nur in einem Ready-Zustand gesetzt; Standby/Drain → `None`.
    pub active_lease: Option<ActiveLease>,
    /// Genau eine Operation darf laufen (Konfliktmatrix, B2).
    pub operation: Option<Operation>,
    /// Laufender Switch-Evidenz-Sammler (Spec C, AC-F); `None`, wenn kein
    /// Wechsel läuft. Inhaltsfrei — wird beim Admit eröffnet und terminal
    /// abgeschlossen (Journal atomar geschrieben).
    pub evidence: Option<crate::backend::switch_evidence::SwitchEvidence>,
}

impl BackendSupervisorState {
    pub fn new(app_epoch: String) -> Self {
        Self {
            app_epoch,
            generation: 0,
            runtime: RuntimePhase::BootingCpu,
            artifact: ArtifactPhase::NotInstalled,
            artifact_error: None,
            cpu_instance: None,
            cuda_instance: None,
            active_lease: None,
            operation: None,
            evidence: None,
        }
    }

    /// Nächste Generation (monoton innerhalb der Epoche).
    pub fn next_generation(&mut self) -> u64 {
        self.generation += 1;
        self.generation
    }

    /// Typisierter Snapshot für Commands/Events — keine Tokens, keine Ports
    /// jenseits des aktiven Endpunkts, keine Kommandozeilen (C).
    pub fn snapshot(&self) -> SupervisorSnapshot {
        SupervisorSnapshot {
            app_epoch: self.app_epoch.clone(),
            generation: self.generation,
            runtime_phase: format!("{:?}", self.runtime),
            active_variant: self.runtime.active_variant().map(BackendVariant::as_str),
            admission_open: self.runtime.admission_open(),
            artifact_phase: self.artifact.as_str(),
            artifact_error: self.artifact_error.clone(),
            operation_id: self.operation.as_ref().map(|o| o.operation_id.clone()),
            operation_kind: self.operation.as_ref().map(|o| o.kind.as_str()),
        }
    }
}

/// Serialisierbarer Snapshot für Tauri-Commands und Events.
#[derive(Debug, Clone, serde::Serialize)]
pub struct SupervisorSnapshot {
    pub app_epoch: String,
    pub generation: u64,
    pub runtime_phase: String,
    pub active_variant: Option<&'static str>,
    pub admission_open: bool,
    pub artifact_phase: &'static str,
    /// Letzter Addon-Fehlergrund (fail-closed); `None` = kein Fehler aktiv.
    pub artifact_error: Option<String>,
    pub operation_id: Option<String>,
    pub operation_kind: Option<&'static str>,
}

/// Transition-Regelwerk des Runtime-Automaten (B2). Der Actor ruft dies vor
/// jeder Mutation auf; ungültige Sprünge sind hart abgelehnt.
pub fn runtime_transition(from: &RuntimePhase, to: &RuntimePhase) -> bool {
    use RuntimePhase::*;
    match from {
        BootingCpu => matches!(to, CpuReady(_) | CudaReady(_) | NoBackendReady(_)),
        CpuReady(_) => matches!(to, DrainingCpu(_) | StoppingCuda(_)),
        DrainingCpu(ref op) => {
            // CUDA-Pfad (Block d) oder Fehlerfall zurück auf CPU.
            matches!(to, PreparingCuda(_) | CpuReady(_) | NoBackendReady(_)) && same_op(op, to)
        }
        PreparingCuda(ref op) => {
            matches!(to, CudaReady(_) | DrainingCpu(_) | CpuReady(_) | NoBackendReady(_)) && same_op(op, to)
        }
        CudaReady(_) => matches!(to, DrainingCuda(_)),
        DrainingCuda(ref op) => {
            matches!(to, PreparingCpu(_) | CpuReady(_) | NoBackendReady(_)) && same_op(op, to)
        }
        PreparingCpu(ref op) => {
            matches!(to, VerifyingRelease(_) | CpuReady(_) | NoBackendReady(_)) && same_op(op, to)
        }
        StoppingCuda(ref op) => {
            matches!(to, VerifyingRelease(_) | CpuReady(_) | NoBackendReady(_)) && same_op(op, to)
        }
        VerifyingRelease(ref op) => {
            matches!(to, CpuReady(_) | NoBackendReady(_)) && same_op(op, to)
        }
        NoBackendReady(_) => matches!(to, BootingCpu),
    }
}

fn same_op(from_op: &str, to: &RuntimePhase) -> bool {
    match to {
        RuntimePhase::PreparingCuda(o)
        | RuntimePhase::DrainingCuda(o)
        | RuntimePhase::PreparingCpu(o)
        | RuntimePhase::StoppingCuda(o)
        | RuntimePhase::VerifyingRelease(o) => o == from_op,
        _ => true, // Terminalziele ohne Operation tragen keine Op weiter.
    }
}

/// Konfliktmatrix (B2): parallele Switch-/Install-/Repair-/Update-/Remove-
/// Operationen sind verboten; ein identischer Request erhält dieselbe
/// laufende `operation_id`.
pub fn operation_conflict(current: &Option<Operation>, requested: OperationKind) -> Result<(), String> {
    match current {
        None => Ok(()),
        Some(op) if op.kind == requested => Err(format!(
            "identische Operation bereits aktiv (operation_id={})",
            op.operation_id
        )),
        Some(_) => Err("Konfliktmatrix: eine Lifecycle-Operation läuft bereits".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn boot_to_ready_is_valid() {
        assert!(runtime_transition(&RuntimePhase::BootingCpu, &RuntimePhase::CpuReady(1)));
        assert!(!runtime_transition(&RuntimePhase::BootingCpu, &RuntimePhase::PreparingCuda("op".into())));
    }

    #[test]
    fn error_phases_land_on_cpu_or_none() {
        let op = "op-1";
        assert!(runtime_transition(
            &RuntimePhase::DrainingCpu(op.into()),
            &RuntimePhase::NoBackendReady("timeout".into())
        ));
        assert!(runtime_transition(&RuntimePhase::PreparingCuda(op.into()), &RuntimePhase::CpuReady(3)));
        // Op-Wechsel innerhalb einer Phase ist verboten.
        assert!(!runtime_transition(
            &RuntimePhase::DrainingCpu("op-a".into()),
            &RuntimePhase::PreparingCuda("op-b".into())
        ));
    }

    #[test]
    fn full_cuda_roundtrip() {
        let op = "sw-1";
        assert!(runtime_transition(&RuntimePhase::CpuReady(1), &RuntimePhase::DrainingCpu(op.into())));
        assert!(runtime_transition(&RuntimePhase::DrainingCpu(op.into()), &RuntimePhase::PreparingCuda(op.into())));
        assert!(runtime_transition(&RuntimePhase::PreparingCuda(op.into()), &RuntimePhase::CudaReady(2)));
        assert!(runtime_transition(&RuntimePhase::CudaReady(2), &RuntimePhase::DrainingCuda(op.into())));
        assert!(runtime_transition(&RuntimePhase::DrainingCuda(op.into()), &RuntimePhase::PreparingCpu(op.into())));
        assert!(runtime_transition(&RuntimePhase::PreparingCpu(op.into()), &RuntimePhase::VerifyingRelease(op.into())));
        assert!(runtime_transition(&RuntimePhase::VerifyingRelease(op.into()), &RuntimePhase::CpuReady(3)));
    }

    #[test]
    fn admission_only_in_ready() {
        assert!(RuntimePhase::CpuReady(1).admission_open());
        assert!(RuntimePhase::CudaReady(2).admission_open());
        assert!(!RuntimePhase::BootingCpu.admission_open());
        assert!(!RuntimePhase::DrainingCpu("op".into()).admission_open());
    }

    #[test]
    fn conflict_matrix_rejects_parallel_ops() {
        let running = Operation::new(OperationKind::SwitchToCuda);
        assert!(operation_conflict(&Some(running.clone()), OperationKind::InstallAddon).is_err());
        // Identischer Request → dieselbe laufende operation_id (abgelehnt mit Hinweis).
        let err = operation_conflict(&Some(running), OperationKind::SwitchToCuda).unwrap_err();
        assert!(err.contains("identische Operation"));
        assert!(operation_conflict(&None, OperationKind::SwitchToCpu).is_ok());
    }

    #[test]
    fn artifact_lifecycle_transitions() {
        use ArtifactPhase::*;
        // Happy Path: NotInstalled → … → Installed.
        assert!(artifact_transition(&NotInstalled, &Downloading));
        assert!(artifact_transition(&Downloading, &Verifying));
        assert!(artifact_transition(&Verifying, &Staged));
        assert!(artifact_transition(&Staged, &Installed));
        // Fehlerziele: zurück auf NotInstalled (nichts war installiert) oder
        // RepairRequired (bestätigte Version bleibt nutzbar).
        assert!(artifact_transition(&Downloading, &NotInstalled));
        assert!(artifact_transition(&Verifying, &RepairRequired));
        assert!(artifact_transition(&Staged, &RepairRequired));
        // Update: Installed → Downloading (neue Version) oder Removing.
        assert!(artifact_transition(&Installed, &Downloading));
        assert!(artifact_transition(&Installed, &Removing));
        assert!(artifact_transition(&Removing, &NotInstalled));
        // Ungültige Sprünge sind hart abgelehnt.
        assert!(!artifact_transition(&NotInstalled, &Installed));
        assert!(!artifact_transition(&Verifying, &Installed));
        assert!(!artifact_transition(&Downloading, &Staged));
    }

    #[test]
    fn generation_is_monotonic_within_epoch() {
        let mut st = BackendSupervisorState::new(new_app_epoch());
        assert_eq!(st.next_generation(), 1);
        assert_eq!(st.next_generation(), 2);
        // Epoche ist pro Prozess eindeutig (UUIDv4).
        assert_ne!(new_app_epoch(), new_app_epoch());
    }

    #[test]
    fn snapshot_is_token_free() {
        let mut st = BackendSupervisorState::new("epoch-1".into());
        st.runtime = RuntimePhase::CpuReady(1);
        st.active_lease = Some(ActiveLease {
            app_epoch: "epoch-1".into(),
            generation: 1,
            backend_variant: BackendVariant::Cpu,
            model_contract_hash: "hash".into(),
            sidecar_instance_id: "inst-1".into(),
        });
        let snap = st.snapshot();
        assert_eq!(snap.active_variant, Some("cpu"));
        assert!(snap.admission_open);
        assert_eq!(snap.artifact_phase, "not_installed");
    }
}
