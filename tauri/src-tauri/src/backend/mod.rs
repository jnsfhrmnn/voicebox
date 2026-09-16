//! JFW-12 B1: Backend-Supervisor-Modul (Rust).
//!
//! * `state`      — RuntimeState, ArtifactState, Lease, Operation (B2/B3)
//! * `admission`  — JobPermit und Generationenfence (B3/B5)
//! * `supervisor` — der serielle BackendSupervisor-Actor (B2)
//!
//! Prozessvertrag (`process.rs`) und Windows-Job-Objects (`process_windows.rs`)
//! folgen in Block (c); NVML-Evidence in Block (e).

pub mod admission;
pub mod state;
pub mod supervisor;
