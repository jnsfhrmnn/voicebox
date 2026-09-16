//! JFW-12 B1: Backend-Supervisor-Modul (Rust).
//!
//! * `state`            — RuntimeState, ArtifactState, Lease, Operation (B2/B3)
//! * `admission`        — JobPermit und Generationenfence (B3/B5)
//! * `supervisor`       — der serielle BackendSupervisor-Actor (B2)
//! * `process_windows`  — CreateProcess/Job Object/Handle-Waits (B8, Windows)
//! * `artifact`         — CUDA-Artefaktmanager: signiertes Addon, Staging,
//!                         atomarer Current-Pointer (B9)
//! * `gpu_evidence`     — NVML-Kontextproben und Receipts (Block e, B5/B6)
//! * `switch_evidence`  — Switch-Journal/-Receipt-Persistierung (Block g, AC-F)
//! * `switch_plan`      — Reine Switch-Entscheidung/Planung (Block g, B5/B6)

pub mod admission;
#[cfg(windows)]
pub mod process_windows;
pub mod artifact;
pub mod gpu_evidence;
pub mod state;
pub mod supervisor;
pub mod switch_evidence;
pub mod switch_plan;
