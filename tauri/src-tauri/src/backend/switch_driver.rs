//! JFW-12 Block (g): Switch-Prozessschritte — Orchestrierung (Spec B5/B6).
//!
//! Führt die Prozessschritte eines *angenommenen* Idle-Switches aus:
//! Drain → Teardown des Ausgangs-Backends → Zielstart + Handshake →
//! Readiness-Smoke → (CudaToCpu: VRAM-Evidence) → Ready. Jeder Schritt ist als
//! Closure injizierbar, damit die **Sequenzierung und Rollback-Logik** hier
//! unit-testbar sind; die echten Schritte (HTTP-Drain, Job-Close-Teardown,
//! Spawn + Handshake, `/health`, NVML) werden in `main.rs` als Closures
//! übergeben und laufen im Blocking-Task. Bei jedem Fehler gilt fail-closed:
//! das Ziel wird beendet, der Ausgang nach erneuter Prüfung wieder freigegeben
//! (B5.8/B6.8), und der Actor wird via `on_failed` informiert → NoBackendReady.

use crate::backend::state::{BackendVariant, SidecarInstance};
use crate::backend::switch_evidence::SwitchDirection;

/// Kontext einer angenommenen Switch-Operation (aus dem Actor).
#[derive(Debug, Clone)]
#[allow(dead_code)] // JFW-12 Block (g): main.rs-Wiring folgt (Zielsystem-Seam)
pub struct SwitchContext {
    pub op_id: String,
    pub direction: SwitchDirection,
}

impl SwitchContext {
    /// Zielvariante der Operation.
    #[allow(dead_code)] // JFW-12 Block (g): main.rs-Wiring folgt (Zielsystem-Seam)
    pub fn target_variant(&self) -> BackendVariant {
        match self.direction {
            SwitchDirection::CpuToCuda => BackendVariant::Cuda,
            SwitchDirection::CudaToCpu => BackendVariant::Cpu,
        }
    }
}

/// Terminaler Ausgang der Orchestrierung.
#[derive(Debug, PartialEq)]
#[allow(dead_code)] // JFW-12 Block (g): main.rs-Wiring folgt (Zielsystem-Seam)
pub enum SwitchOutcome {
    /// Ziel-Backend ist Ready; der Actor hat die Generation aktiviert + Admission geöffnet.
    Completed,
    /// Fail-closed: NoBackendReady gesetzt (sichtbar). Grund inhaltsfrei (C).
    Failed(String),
}

/// Führt die Prozessschritte eines angenommenen Idle-Switches aus (B5/B6).
///
/// Reihenfolge ist bindend und wird hier unit-geprüft; die Closures liefern
/// `Err` bei jedem Schrittfehler. Nach einem Fehler **nach** Zielstart wird das
/// Ziel beendet (`teardown_target`) — B5.8/B6.8. Der Actor erfährt den terminalen
/// Zustand ausschließlich über `on_drained` / `on_target_ready` / `on_failed`.
#[allow(dead_code)] // JFW-12 Block (g): main.rs-Wiring folgt (Zielsystem-Seam)
pub fn run_switch(
    _ctx: &SwitchContext,
    drain_source: impl Fn() -> Result<(), String>,
    teardown_source: impl Fn() -> Result<(), String>,
    start_target: impl Fn() -> Result<SidecarInstance, String>,
    readiness_smoke: impl Fn(&SidecarInstance) -> Result<(), String>,
    vram_evidence: Option<Box<dyn Fn(&SidecarInstance) -> Result<(), String>>>,
    teardown_target: impl Fn(&SidecarInstance),
    on_drained: impl Fn(),
    on_target_ready: impl Fn(SidecarInstance),
    on_failed: impl Fn(String),
) -> SwitchOutcome {
    // 1) Drain (B5.2 / B6.1): aktive Jobs drainieren, bis null aktiv.
    if let Err(e) = drain_source() {
        return fail_closed(&on_failed, format!("drain fehlgeschlagen: {e}"));
    }
    on_drained();

    // 2) Teardown des Ausgangs-Backends (B5/B6): graceful Shutdown + Job-Close.
    if let Err(e) = teardown_source() {
        return fail_closed(&on_failed, format!("teardown fehlgeschlagen: {e}"));
    }

    // 3) Zielstart mit inaktivem Lease + Handshake (B5.4 / B6.2).
    let instance = match start_target() {
        Ok(i) => i,
        Err(e) => return fail_closed(&on_failed, format!("Zielstart fehlgeschlagen: {e}")),
    };

    // 4) Readiness-Smoke (B5.6 / B6.2): `/health` grün + Vertrag geprüft.
    if let Err(e) = readiness_smoke(&instance) {
        return fail_after_start(&on_failed, &teardown_target, &instance, format!("Readiness fehlgeschlagen: {e}"));
    }

    // 5) VRAM-Evidence (B6.6–7): nur CudaToCpu — NVML doppelte negative Probe.
    if let Some(ev) = &vram_evidence {
        if let Err(e) = ev(&instance) {
            return fail_after_start(
                &on_failed,
                &teardown_target,
                &instance,
                format!("VRAM-Evidence fehlgeschlagen: {e}"),
            );
        }
    }

    // 6) Ready (B5.7 / B6.3): Actor widerruft Ausgangs-Lease, aktiviert die
    //    Ziel-Generation und öffnet Admission; das Journal wird im Actor
    //    abgeschlossen (SwitchTargetReady).
    on_target_ready(instance);
    SwitchOutcome::Completed
}

/// Fail-closed **vor** Zielstart: kein Ziel zu beenden, nur den Actor informieren.
fn fail_closed(on_failed: &impl Fn(String), reason: String) -> SwitchOutcome {
    on_failed(reason.clone());
    SwitchOutcome::Failed(reason)
}

/// Fail-closed **nach** Zielstart (B5.8/B6.8): Ziel wird beendet, dann Actor.
fn fail_after_start(
    on_failed: &impl Fn(String),
    teardown_target: &impl Fn(&SidecarInstance),
    instance: &SidecarInstance,
    reason: String,
) -> SwitchOutcome {
    teardown_target(instance);
    on_failed(reason.clone());
    SwitchOutcome::Failed(reason)
}

/// Echte Prozessschritte eines angenommenen Idle-Switches (B5/B6) — injiziert
/// von `main.rs` als Factory in den Supervisor. Alle Methoden sind **blocking**
/// und laufen im Switch-Blocking-Task; `Err` = Schrittfehler → fail-closed.
pub trait SwitchStepFactory: Send + Sync {
    /// Schritt 1 (B5.2/B6.1): aktive Jobs drainieren, bis null aktiv.
    fn drain(&self, direction: SwitchDirection) -> Result<(), String>;
    /// Schritt 2 (B5/B6): Ausgangs-Backend graceful beenden + Prozessbaum schließen.
    fn teardown_source(&self, direction: SwitchDirection) -> Result<(), String>;
    /// Schritt 3 (B5.4/B6.2): Ziel-Backend starten + Ready-Handshake abwarten.
    fn start_target(&self, direction: SwitchDirection) -> Result<SidecarInstance, String>;
    /// Schritt 4 (B5.6/B6.2): `/health` grün + Modellbereitschaft bestätigt.
    fn readiness_smoke(&self, instance: &SidecarInstance) -> Result<(), String>;
    /// Schritt 5 (B6.6–7, nur CudaToCpu): doppelte negative VRAM-Probe für den
    /// Ausgangs-PID; `None` = kein bekannter PID → fail-closed.
    fn vram_evidence(&self, old_cuda_pid: Option<u32>) -> Result<(), String>;
    /// Fail-closed nach Zielstart (B5.8/B6.8): frisch gestartetes Ziel beenden.
    fn teardown_target(&self, instance: &SidecarInstance);
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    /// Test-Doppel: protokolliert die Reihenfolge der Actor-Callbacks.
    #[derive(Default)]
    struct Log(Arc<Mutex<Vec<String>>>);
    impl Log {
        fn new() -> (Self, Arc<Mutex<Vec<String>>>) {
            let v = Arc::new(Mutex::new(Vec::new()));
            (Log(v.clone()), v)
        }
        fn rec(&self, s: &str) {
            self.0.lock().unwrap().push(s.to_string());
        }
    }

    fn inst() -> SidecarInstance {
        SidecarInstance::new(4242, "cuda.exe".into(), BackendVariant::Cuda, 51000, "build-1".into())
    }

    fn ctx_cuda_to_cpu() -> SwitchContext {
        SwitchContext { op_id: "op-1".into(), direction: SwitchDirection::CudaToCpu }
    }
    fn ctx_cpu_to_cuda() -> SwitchContext {
        SwitchContext { op_id: "op-2".into(), direction: SwitchDirection::CpuToCuda }
    }

    /// Happy Path CudaToCpu: volle Sequenz inkl. VRAM-Evidence, kein Failure.
    #[test]
    fn happy_path_cuda_to_cpu_full_sequence() {
        let (log, v) = Log::new();
        let out = run_switch(
            &ctx_cuda_to_cpu(),
            || Ok(()), // drain
            || Ok(()), // teardown_source
            || Ok(inst()), // start_target
            |_| Ok(()), // readiness
            Some(Box::new(|_| Ok(()))), // vram_evidence (CudaToCpu)
            |i| log.rec(&format!("teardown_target:{}", i.pid)),
            || log.rec("drained"),
            |i| log.rec(&format!("ready:{}", i.pid)),
            |r| log.rec(&format!("failed:{r}")),
        );
        assert_eq!(out, SwitchOutcome::Completed);
        let seq = v.lock().unwrap().clone();
        assert_eq!(seq, vec!["drained", "ready:4242"], "Sequenz + Callback-Reihenfolge");
    }

    /// CpuToCuda: keine VRAM-Evidence (nur CudaToCpu), aber sonst identisch.
    #[test]
    fn cpu_to_cuda_skips_vram_evidence() {
        let ready_pid = Arc::new(Mutex::new(0u32));
        let out = run_switch(
            &ctx_cpu_to_cuda(),
            || Ok(()),
            || Ok(()),
            || Ok(inst()),
            |_| Ok(()),
            None, // CpuToCuda: keine VRAM-Evidence
            |_| {},
            || {},
            |i| { *ready_pid.lock().unwrap() = i.pid; },
            |_| panic!("darf nicht fehlschlagen"),
        );
        assert_eq!(out, SwitchOutcome::Completed);
        assert_eq!(*ready_pid.lock().unwrap(), 4242, "on_target_ready muss die Instanz liefern");
    }

    /// Drain-Fehler → fail-closed VOR Teardown/Start; kein Ziel-Teardown.
    #[test]
    fn drain_failure_is_fail_closed_before_any_teardown() {
        let (log, v) = Log::new();
        let out = run_switch(
            &ctx_cuda_to_cpu(),
            || Err("timeout".into()), // drain schlägt fehl
            || panic!("teardown_source darf nicht laufen"),
            || panic!("start_target darf nicht laufen"),
            |_| panic!("readiness darf nicht laufen"),
            Some(Box::new(|_| panic!("vram darf nicht laufen"))),
            |i| log.rec(&format!("teardown_target:{}", i.pid)),
            || log.rec("drained"),
            |_| log.rec("ready"),
            |r| log.rec(&format!("failed:{r}")),
        );
        assert!(matches!(out, SwitchOutcome::Failed(_)));
        let seq = v.lock().unwrap().clone();
        assert_eq!(seq.len(), 1, "nur on_failed, kein drained/ready/target-teardown");
        assert!(seq[0].starts_with("failed:drain fehlgeschlagen"));
    }

    /// Readiness-Fehler NACH Zielstart → Ziel wird beendet + fail-closed.
    #[test]
    fn readiness_failure_tears_down_target_then_fails() {
        let (log, v) = Log::new();
        let out = run_switch(
            &ctx_cuda_to_cpu(),
            || Ok(()),
            || Ok(()),
            || Ok(inst()),
            |_| Err("health rot".into()), // readiness schlägt fehl
            Some(Box::new(|_| panic!("vram darf nicht laufen"))),
            |i| log.rec(&format!("teardown_target:{}", i.pid)),
            || log.rec("drained"),
            |_| log.rec("ready"),
            |r| log.rec(&format!("failed:{r}")),
        );
        assert!(matches!(out, SwitchOutcome::Failed(_)));
        let seq = v.lock().unwrap().clone();
        // drained kam (vor Start), dann Ziel-Teardown, dann failed — kein ready.
        assert_eq!(seq[0], "drained");
        assert_eq!(seq[1], "teardown_target:4242", "Ziel muss bei Readiness-Fehler beendet werden");
        assert!(seq[2].starts_with("failed:Readiness fehlgeschlagen"));
        assert!(!seq.iter().any(|s| s == "ready"), "kein ready nach Fehler");
    }

    /// VRAM-Evidence-Fehler (CudaToCpu) → Ziel wird beendet + fail-closed.
    #[test]
    fn vram_evidence_failure_tears_down_target_then_fails() {
        let (log, v) = Log::new();
        let out = run_switch(
            &ctx_cuda_to_cpu(),
            || Ok(()),
            || Ok(()),
            || Ok(inst()),
            |_| Ok(()), // readiness grün
            Some(Box::new(|_| Err("nvml rot".into()))), // vram schlägt fehl
            |i| log.rec(&format!("teardown_target:{}", i.pid)),
            || log.rec("drained"),
            |_| log.rec("ready"),
            |r| log.rec(&format!("failed:{r}")),
        );
        assert!(matches!(out, SwitchOutcome::Failed(_)));
        let seq = v.lock().unwrap().clone();
        assert_eq!(seq[1], "teardown_target:4242");
        assert!(seq[2].starts_with("failed:VRAM-Evidence fehlgeschlagen"));
    }

    /// Zielstart-Fehler → kein Ziel vorhanden, also kein Ziel-Teardown.
    #[test]
    fn start_failure_is_fail_closed_without_target_teardown() {
        let (log, v) = Log::new();
        let out = run_switch(
            &ctx_cpu_to_cuda(),
            || Ok(()),
            || Ok(()),
            || Err("spawn fehlgeschlagen".into()), // start schlägt fehl
            |_| panic!("readiness darf nicht laufen"),
            None,
            |i| log.rec(&format!("teardown_target:{}", i.pid)),
            || log.rec("drained"),
            |_| log.rec("ready"),
            |r| log.rec(&format!("failed:{r}")),
        );
        assert!(matches!(out, SwitchOutcome::Failed(_)));
        let seq = v.lock().unwrap().clone();
        // drained kam; dann failed — aber KEIN teardown_target (kein Ziel gestartet).
        assert_eq!(seq[0], "drained");
        assert!(seq[1].starts_with("failed:Zielstart fehlgeschlagen"));
        assert!(!seq.iter().any(|s| s.starts_with("teardown_target")));
    }

    /// target_variant leitet die Richtung korrekt ab.
    #[test]
    fn target_variant_maps_direction() {
        assert_eq!(ctx_cpu_to_cuda().target_variant(), BackendVariant::Cuda);
        assert_eq!(ctx_cuda_to_cpu().target_variant(), BackendVariant::Cpu);
    }
}
