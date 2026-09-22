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
pub struct SwitchContext {
    pub op_id: String,
    pub direction: SwitchDirection,
}

impl SwitchContext {
    /// Zielvariante der Operation.
        pub fn target_variant(&self) -> BackendVariant {
        match self.direction {
            SwitchDirection::CpuToCuda => BackendVariant::Cuda,
            SwitchDirection::CudaToCpu => BackendVariant::Cpu,
        }
    }
}

/// Terminaler Ausgang der Orchestrierung.
#[derive(Debug, PartialEq)]
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
///
/// Beide Richtungen beenden den Ausgangs-Prozess **vor** dem Zielstart:
/// `ServerState` hält genau eine Live-Instanz (PID/Port/Job-Slot), und B6.8
/// verbietet einen CUDA-Rollback („wird CUDA trotzdem beendet") — ein
/// Coexistence-Fenster (B6 Schritt 2 vor 4) bräuchte Dual-Instance-Tracking, das
/// erst mit der Warm-Standby-Architektur (Decision Log 2026-09-09) folgt. Die
/// Garantien bleiben identisch: Admission geschlossen, kein stiller Fallback,
/// VRAM-Nachweis nach dem Prozessende, NoBackendReady bei Ziel-Fehler.
pub fn run_switch(
    ctx: &SwitchContext,
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
        return fail_closed(&on_failed, format!("drain fehlgeschlagen ({}): {e}", ctx.op_id));
    }
    on_drained();

    // 2) Teardown des Ausgangs-Backends (B5/B6): graceful Shutdown + Job-Close.
    if let Err(e) = teardown_source() {
        return fail_closed(&on_failed, format!("teardown fehlgeschlagen ({}): {e}", ctx.op_id));
    }

    // 3) Zielstart mit inaktivem Lease + Handshake (B5.4 / B6.2).
    let instance = match start_target() {
        Ok(i) => i,
        Err(e) => return fail_closed(&on_failed, format!("Zielstart fehlgeschlagen ({}): {e}", ctx.op_id)),
    };

    // 4) Readiness-Smoke (B5.5/B6.2): echte Inferenz + Modell-/Variant-Vertrag.
    if let Err(e) = readiness_smoke(&instance) {
        return fail_after_start(&on_failed, &teardown_target, &instance, format!("Readiness fehlgeschlagen ({}): {e}", ctx.op_id));
    }

    // 5) VRAM-Evidence (B6.6–7): nur CudaToCpu — NVML doppelte negative Probe,
    //    **nach** dem CUDA-Prozessende (Schritt 2).
    if let Some(ev) = &vram_evidence {
        if let Err(e) = ev(&instance) {
            return fail_after_start(
                &on_failed,
                &teardown_target,
                &instance,
                format!("VRAM-Evidence fehlgeschlagen ({}): {e}", ctx.op_id),
            );
        }
    }

    // 6) Ready (B5.7 / B6.3): Actor widerruft Ausgangs-Lease, aktiviert die
    //    Ziel-Generation und öffnet Admission; das Journal wird im Actor
    //    abgeschlossen (SwitchTargetReady).
    on_target_ready(instance);
    SwitchOutcome::Completed
}

/// Fährt einen angenommenen Idle-Switch über die **echten** Prozessschritte
/// eines [`SwitchStepFactory`] und bindet die Evidence-Journal-Aufzeichnung an
/// den Lauf (Spec C, AC-F): getrennte Phasenzeitanteile (Admitted → Drain →
/// BackendEnd → TargetStart → ModelReady → VramCheck), Job-Object-Kill und
/// Prozessende des Ausgangs sowie die VRAM-/Compute-Proben (B6.6–7).
///
/// Die VRAM-Freigabe läuft über `run_release_verification` mit injizierbarer
/// Probequelle (Produktionscaller: `GpuContextProbe::capture()`): erst zwei
/// grüne Proben mit mindestens `min_interval` Abstand leiten `0 MiB
/// zurechenbar` ab (AC-G). Fail-closed wie [`run_switch`]; die besessene
/// [`SwitchEvidence`] wird für den terminalen Journal-Write an den Aufrufer
/// zurückgegeben (finish ist verbrauchend).
pub fn run_switch_steps(
    ctx: &SwitchContext,
    steps: std::sync::Arc<dyn SwitchStepFactory>,
    evidence: crate::backend::switch_evidence::SwitchEvidence,
    to_generation: u64,
    old_cuda_pid: Option<u32>,
    min_interval: std::time::Duration,
    on_drained: impl Fn(),
    on_target_ready: impl Fn(SidecarInstance),
    on_failed: impl Fn(String),
) -> (SwitchOutcome, crate::backend::switch_evidence::SwitchEvidence) {
    use crate::backend::gpu_evidence::GpuReceipt;
    use crate::backend::switch_evidence::{run_release_verification, PhaseName, ReleaseOutcome};
    use std::sync::{Arc, Mutex};

    let direction = ctx.direction;
    let ev = Arc::new(Mutex::new(evidence));

    // 1) Drain (B5.2/B6.1) — Phase Drain ersetzt Admitted.
    let (ev_d, s_d) = (Arc::clone(&ev), Arc::clone(&steps));
    let drain = move || {
        ev_d.lock().unwrap().begin_phase(PhaseName::Drain);
        s_d.drain(direction)
    };

    // 2) Teardown (B5/B6): Job-Object-Kill über process_windows + Prozessende.
    //    Nur ein bestätigtes Ende setzt die Journal-Flags (kein erfundener
    //    Freigabenachweis, AC-A).
    let (ev_t, s_t) = (Arc::clone(&ev), Arc::clone(&steps));
    let teardown_source = move || {
        ev_t.lock().unwrap().begin_phase(PhaseName::BackendEnd);
        let r = s_t.teardown_source(direction);
        if r.is_ok() {
            let mut e = ev_t.lock().unwrap();
            e.set_job_tree_terminated();
            e.set_process_end_confirmed();
        }
        r
    };

    // 3) Zielstart mit Handshake (B5.4/B6.2).
    let (ev_s, s_s) = (Arc::clone(&ev), Arc::clone(&steps));
    let start_target = move || {
        ev_s.lock().unwrap().begin_phase(PhaseName::TargetStart);
        s_s.start_target(direction)
    };

    // 4) Readiness-Smoke (B5.5/B6.2).
    let (ev_r, s_r) = (Arc::clone(&ev), Arc::clone(&steps));
    let readiness_smoke = move |i: &SidecarInstance| {
        ev_r.lock().unwrap().begin_phase(PhaseName::ModelReady);
        s_r.readiness_smoke(i)
    };

    // 5) VRAM-Evidence (B6.6–7): nur Ziel CPU = CudaToCpu. Der Freigabe-Zyklus
    //    zeichnet jede Probe in das Journal und verlangt zwei grüne Proben.
    let vram: Option<Box<dyn Fn(&SidecarInstance) -> Result<(), String>>> =
        if ctx.target_variant() == BackendVariant::Cpu {
            let (ev_v, s_v) = (Arc::clone(&ev), Arc::clone(&steps));
            Some(Box::new(move |_i: &SidecarInstance| {
                let pid = old_cuda_pid
                    .ok_or_else(|| "VRAM-Evidence unmöglich: kein bekannter CUDA-PID".to_string())?;
                let mut receipt = GpuReceipt::for_pids([pid]);
                for _attempt in 0..3u32 {
                    let out = {
                        let mut e = ev_v.lock().unwrap();
                        run_release_verification(&mut receipt, &mut *e, min_interval, || {
                            s_v.vram_probe()
                        })
                    };
                    match out {
                        ReleaseOutcome::Released { attributable_mib } => {
                            return if attributable_mib == 0 {
                                Ok(())
                            } else {
                                Err(format!(
                                    "0-MiB-Ableitung verweigert: {attributable_mib} MiB zurechenbar"
                                ))
                            };
                        }
                        // Erste grüne Probe: Mindestabstand einhalten, erneut proben.
                        ReleaseOutcome::Pending => std::thread::sleep(min_interval),
                        ReleaseOutcome::ProbeError(reason) => {
                            return Err(format!("VRAM-Evidence fehlgeschlagen: {reason}"));
                        }
                    }
                }
                Err("VRAM-Freigabe nicht abgeschlossen (kein zweiter grüner Nachweis)".into())
            }))
        } else {
            None
        };

    // Fail-closed nach Zielstart (B5.8/B6.8): frisch gestartetes Ziel beenden.
    let s_tt = Arc::clone(&steps);
    let teardown_target = move |i: &SidecarInstance| s_tt.teardown_target(i);

    let outcome = run_switch(
        ctx,
        drain,
        teardown_source,
        start_target,
        readiness_smoke,
        vram,
        teardown_target,
        on_drained,
        on_target_ready,
        on_failed,
    );
    if matches!(outcome, SwitchOutcome::Completed) {
        ev.lock().unwrap().set_to_generation(to_generation);
    }
    // Alle Closure-Clones sind gefallen → Collector wieder exakt einmal besessen.
    let ev = Arc::try_unwrap(ev)
        .expect("Switch-Evidenz exakt einmal besessen")
        .into_inner()
        .expect("Evidenz-Mutex unvergiftet");
    (outcome, ev)
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
    /// Schritt 5 (B6.6–7): eine frische NVML-Kontextprobe. Der Produktions-
    /// caller ruft `GpuContextProbe::capture()` auf; der Freigabe-Zyklus (zwei
    /// grüne Proben, mindestens 1 s Abstand, PID-gebunden) läuft in
    /// [`run_switch_steps`] über `run_release_verification` — die Probequelle
    /// bleibt als Closure/Schnittstelle injizierbar (Tests ohne CUDA-Hardware).
    fn vram_probe(
        &self,
    ) -> Result<crate::backend::gpu_evidence::GpuContextProbe, crate::backend::gpu_evidence::GpuEvidenceError>;
    /// Fail-closed nach Zielstart (B5.8/B6.8): frisch gestartetes Ziel beenden.
    fn teardown_target(&self, instance: &SidecarInstance);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backend::gpu_evidence::{ComputeProcess, GpuContextProbe, GpuEvidenceError};
    use crate::backend::switch_evidence::{read_journal_in, PhaseName, SwitchEvidence, SwitchResult};
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

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

    // ── JFW-12 Block (h): Glue über den KOMPLETTEN Prozesszyklus ─────────
    // run_switch_steps treibt Drain → Teardown (Job-Object-Kill) → Zielstart →
    // Readiness-Smoke → VRAM-Evidence → Ready mit echtem SwitchStepFactory-
    // Vertrag und bindet die Journal-Evidenz (Phasen, Prozessende, VRAM-Proben)
    // an den Lauf — fail-closed Rollback bei jedem Schrittfehler (B5.8/B6.8).

    /// Fake-Factory: protokolliert die Schritt-Reihenfolge und liefert
    /// geskriptete NVML-Proben (Closure-Injection bleibt erhalten).
    struct FakeFactory {
        log: Arc<Mutex<Vec<String>>>,
        probes: Arc<Mutex<Vec<GpuContextProbe>>>,
        fail_readiness: bool,
    }

    impl FakeFactory {
        fn new(log: Arc<Mutex<Vec<String>>>, probes: Vec<GpuContextProbe>) -> Self {
            Self { log, probes: Arc::new(Mutex::new(probes)), fail_readiness: false }
        }
        fn rec(&self, s: &str) {
            self.log.lock().unwrap().push(s.to_string());
        }
    }

    impl SwitchStepFactory for FakeFactory {
        fn drain(&self, _direction: SwitchDirection) -> Result<(), String> {
            self.rec("drain");
            Ok(())
        }
        fn teardown_source(&self, _direction: SwitchDirection) -> Result<(), String> {
            self.rec("teardown_source");
            Ok(())
        }
        fn start_target(&self, _direction: SwitchDirection) -> Result<SidecarInstance, String> {
            self.rec("start_target");
            Ok(inst())
        }
        fn readiness_smoke(&self, _instance: &SidecarInstance) -> Result<(), String> {
            self.rec("readiness_smoke");
            if self.fail_readiness {
                Err("health rot".into())
            } else {
                Ok(())
            }
        }
        fn vram_probe(&self) -> Result<GpuContextProbe, GpuEvidenceError> {
            self.rec("vram_probe");
            let mut ps = self.probes.lock().unwrap();
            if ps.is_empty() {
                Err(GpuEvidenceError::Query("Probeskript erschoepft".into()))
            } else {
                Ok(ps.remove(0))
            }
        }
        fn teardown_target(&self, instance: &SidecarInstance) {
            self.rec(&format!("teardown_target:{}", instance.pid));
        }
    }

    /// Grüne NVML-Probe ohne aufgezeichnete PID (WDDM: keine MiB-Werte).
    fn green_probe() -> GpuContextProbe {
        GpuContextProbe {
            device_name: "Test-GPU".to_string(),
            driver_version: "0.0-test".to_string(),
            total_vram_mib: 32_000,
            free_vram_mib: 30_000,
            compute_processes: Vec::<ComputeProcess>::new(),
        }
    }

    fn glue_evidence(op_id: &str) -> SwitchEvidence {
        SwitchEvidence::new(op_id.into(), SwitchDirection::CudaToCpu, "epoch-1".into(), 5)
    }

    fn temp_dir(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("jfw12-driver-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).expect("Temp-Verzeichnis anlegbar");
        d
    }

    /// Happy Path CudaToCpu über die echte Factory: volle Sequenz + vollständige
    /// Journal-Evidenz (zwei grüne VRAM-Proben PID-gebunden, AC-G).
    #[test]
    fn glue_kompletter_zyklus_schreibt_vollstaendige_evidenz() {
        let log = Arc::new(Mutex::new(Vec::new()));
        let factory: Arc<dyn SwitchStepFactory> =
            Arc::new(FakeFactory::new(log.clone(), vec![green_probe(), green_probe()]));
        let ctx = SwitchContext { op_id: "op-glue-1".into(), direction: SwitchDirection::CudaToCpu };
        let (outcome, ev) = run_switch_steps(
            &ctx,
            factory,
            glue_evidence("op-glue-1"),
            6,
            Some(4242),
            Duration::ZERO,
            || {},
            |_| {},
            |_| panic!("darf nicht fehlschlagen"),
        );
        assert_eq!(outcome, SwitchOutcome::Completed);
        let seq = log.lock().unwrap().clone();
        assert_eq!(
            seq,
            vec!["drain", "teardown_source", "start_target", "readiness_smoke", "vram_probe", "vram_probe"],
            "Schritt-Reihenfolge des kompletten Prozesszyklus"
        );

        let dir = temp_dir("glue-full");
        let path = ev.finish_in(&dir, SwitchResult::Success).expect("Journal schreibbar");
        assert!(path.exists());
        let j = read_journal_in(&dir, "op-glue-1").expect("Journal lesbar");
        assert_eq!(j.result, SwitchResult::Success);
        assert_eq!(j.to_generation, Some(6), "Zielgeneration ist im Journal gebunden");
        assert!(j.job_tree_terminated, "Job-Object-Kill des Ausgangsbaums aufgezeichnet");
        assert!(j.process_end_confirmed, "Prozessende des Ausgangs bestaetigt");
        let names: Vec<PhaseName> = j.phases.iter().map(|p| p.phase).collect();
        for expected in [
            PhaseName::Admitted,
            PhaseName::Drain,
            PhaseName::BackendEnd,
            PhaseName::TargetStart,
            PhaseName::ModelReady,
            PhaseName::VramCheck,
        ] {
            assert!(names.contains(&expected), "Phase {expected:?} fehlt im Journal");
        }
        assert_eq!(j.vram_probes.len(), 2, "AC-G: zwei Proben, PID-gebunden");
        assert!(j.vram_probes.iter().all(|p| p.green));
        assert_eq!(j.vram_probes[1].attributable_mib, Some(0), "0 MiB erst nach Abschluss");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// CpuToCuda: kein VRAM-Schritt (B6), keine VramCheck-Phase, sonst gleich.
    #[test]
    fn glue_cpu_to_cuda_ohne_vram_schritt() {
        let log = Arc::new(Mutex::new(Vec::new()));
        let factory: Arc<dyn SwitchStepFactory> = Arc::new(FakeFactory::new(log.clone(), Vec::new()));
        let ctx = ctx_cpu_to_cuda();
        let (outcome, ev) = run_switch_steps(
            &ctx,
            factory,
            glue_evidence("op-glue-2"),
            7,
            None,
            Duration::ZERO,
            || {},
            |_| {},
            |_| panic!("darf nicht fehlschlagen"),
        );
        assert_eq!(outcome, SwitchOutcome::Completed);
        let seq = log.lock().unwrap().clone();
        assert_eq!(seq, vec!["drain", "teardown_source", "start_target", "readiness_smoke"]);
        let dir = temp_dir("glue-cpu2cuda");
        ev.finish_in(&dir, SwitchResult::Success).expect("Journal schreibbar");
        let j = read_journal_in(&dir, "op-glue-2").expect("Journal lesbar");
        let names: Vec<PhaseName> = j.phases.iter().map(|p| p.phase).collect();
        assert!(!names.contains(&PhaseName::VramCheck), "VramCheck nur bei CudaToCpu");
        assert!(j.vram_probes.is_empty());
        assert_eq!(j.to_generation, Some(7));
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Fail-closed Rollback (B5.8/B6.8): Readiness-Fehler nach Zielstart beendet
    /// das Ziel; das Journal haelt den Fehlergrund, keine Zielgeneration.
    #[test]
    fn glue_readiness_fehler_rollbacks_und_schreibt_fehlerjournal() {
        let log = Arc::new(Mutex::new(Vec::new()));
        let mut fake = FakeFactory::new(log.clone(), Vec::new());
        fake.fail_readiness = true;
        let factory: Arc<dyn SwitchStepFactory> = Arc::new(fake);
        let ctx = SwitchContext { op_id: "op-glue-3".into(), direction: SwitchDirection::CudaToCpu };
        let reason = Arc::new(Mutex::new(None));
        let reason_c = Arc::clone(&reason);
        let (outcome, ev) = run_switch_steps(
            &ctx,
            factory,
            glue_evidence("op-glue-3"),
            8,
            Some(4242),
            Duration::ZERO,
            || {},
            |_| {},
            move |r| {
                *reason_c.lock().unwrap() = Some(r);
            },
        );
        assert!(matches!(outcome, SwitchOutcome::Failed(_)));
        let seq = log.lock().unwrap().clone();
        assert_eq!(
            seq,
            vec!["drain", "teardown_source", "start_target", "readiness_smoke", "teardown_target:4242"],
            "Ziel wird bei Readiness-Fehler beendet (fail-closed)"
        );
        assert!(reason.lock().unwrap().as_deref().unwrap_or("").contains("Readiness fehlgeschlagen"));
        let dir = temp_dir("glue-fail");
        ev.finish_in(&dir, SwitchResult::Failure("Readiness fehlgeschlagen: health rot".into()))
            .expect("Fehlerjournal schreibbar");
        let j = read_journal_in(&dir, "op-glue-3").expect("Journal lesbar");
        assert_eq!(j.to_generation, None, "keine Zielgeneration bei Fehler");
        assert_eq!(j.result.as_str(), "failure");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// CudaToCpu ohne bekannten Ausgangs-PID ist fail-closed (kein erfundener
    /// 0-MiB-Nachweis) — das frisch gestartete Ziel wird zurueckgerollt.
    #[test]
    fn glue_ohne_knownen_cuda_pid_ist_fail_closed() {
        let log = Arc::new(Mutex::new(Vec::new()));
        let factory: Arc<dyn SwitchStepFactory> = Arc::new(FakeFactory::new(log.clone(), Vec::new()));
        let ctx = SwitchContext { op_id: "op-glue-4".into(), direction: SwitchDirection::CudaToCpu };
        let (outcome, _ev) = run_switch_steps(
            &ctx,
            factory,
            glue_evidence("op-glue-4"),
            9,
            None,
            Duration::ZERO,
            || {},
            |_| {},
            |_| {},
        );
        match outcome {
            SwitchOutcome::Failed(r) => assert!(r.contains("kein bekannter CUDA-PID"), "Grund: {r}"),
            other => panic!("erwarteter Fail-closed, bekam {other:?}"),
        }
        let seq = log.lock().unwrap().clone();
        assert_eq!(
            seq,
            vec!["drain", "teardown_source", "start_target", "readiness_smoke", "teardown_target:4242"]
        );
    }
}
