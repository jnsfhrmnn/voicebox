//! JFW-12 Block (g) — Switch-Journal und -Receipt (Spec C, AC-F).
//!
//! Jeder Backendwechsel hinterlässt ein inhaltsfreies Journal unter
//! `%LOCALAPPDATA%\JFWhisper\runtime\backend-operations\<operation_id>.json`.
//! Es enthält Operation, Richtung, Generationen, Zeitpunkte, Jobentscheidungen,
//! Phasen-Timing, Prozessende und VRAM-Proben — aber weder Audio noch
//! Transkripttext oder andere Inhaltsdaten (AC-F). Das Journal ist atomar
//! ersetzt (temp + rename), crashrecoverbar und niemals Erfolgsautorität allein.
//!
//! Die getrennten Zeitanteile (Annahme, Drain, Backendende, Zielstart,
//! Modellbereitschaft, VRAM-Prüfung, Erfolg/Fehler/Rollback) sind die Basis für
//! den 30×-Idle-Switch-Benchmark (AC-F): Richtungswechsel und Phasen werden
//! einzeln ausgewiesen, nicht in ein Gesamtbudget gemischt.

use std::path::PathBuf;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::backend::gpu_evidence::{GpuContextProbe, GpuEvidenceError, GpuReceipt, ReleaseVerification};

/// Richtung eines Backendwechsels.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SwitchDirection {
    CpuToCuda,
    CudaToCpu,
}

impl SwitchDirection {
    pub const fn as_str(self) -> &'static str {
        match self {
            SwitchDirection::CpuToCuda => "cpu_to_cuda",
            SwitchDirection::CudaToCpu => "cuda_to_cpu",
        }
    }
}

impl serde::Serialize for SwitchDirection {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(self.as_str())
    }
}

impl<'de> serde::Deserialize<'de> for SwitchDirection {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let v = String::deserialize(d)?;
        match v.as_str() {
            "cpu_to_cuda" => Ok(SwitchDirection::CpuToCuda),
            "cuda_to_cpu" => Ok(SwitchDirection::CudaToCpu),
            other => Err(serde::de::Error::unknown_variant(other, &["cpu_to_cuda", "cuda_to_cpu"])),
        }
    }
}

/// Getrennte Phasen eines Wechsels (AC-F: „als getrennte Zeitanteile ausgewiesen").
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PhaseName {
    /// Admission geschlossen / Request angenommen.
    Admitted,
    /// Bereits angenommene Jobs drainen (oder Cancel).
    Drain,
    /// Altes Backend beendet (Prozessende bestätigt).
    BackendEnd,
    /// Ziel-Backend gestartet.
    TargetStart,
    /// Modell geladen / Readiness-Smoke bestanden.
    ModelReady,
    /// VRAM-/Compute-Proben geführt.
    VramCheck,
}

impl PhaseName {
    pub const fn as_str(self) -> &'static str {
        match self {
            PhaseName::Admitted => "admitted",
            PhaseName::Drain => "drain",
            PhaseName::BackendEnd => "backend_end",
            PhaseName::TargetStart => "target_start",
            PhaseName::ModelReady => "model_ready",
            PhaseName::VramCheck => "vram_check",
        }
    }
}

impl serde::Serialize for PhaseName {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(self.as_str())
    }
}

impl<'de> serde::Deserialize<'de> for PhaseName {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let v = String::deserialize(d)?;
        match v.as_str() {
            "admitted" => Ok(PhaseName::Admitted),
            "drain" => Ok(PhaseName::Drain),
            "backend_end" => Ok(PhaseName::BackendEnd),
            "target_start" => Ok(PhaseName::TargetStart),
            "model_ready" => Ok(PhaseName::ModelReady),
            "vram_check" => Ok(PhaseName::VramCheck),
            other => Err(serde::de::Error::unknown_variant(other, &[
                "admitted", "drain", "backend_end", "target_start", "model_ready", "vram_check",
            ])),
        }
    }
}

/// Ein gemessener Phasenzeitanteil.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct PhaseTiming {
    pub phase: PhaseName,
    /// Zeitstempel in ms seit UNIX-Epoch (inhaltsfrei).
    pub started_at_ms: u128,
    /// Dauer der Phase in Millisekunden.
    pub duration_ms: u64,
}

/// Ergebnis eines Wechsels — terminal im Journal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SwitchResult {
    Success,
    Failure(String),
    Rollback,
}

impl SwitchResult {
    pub const fn as_str(&self) -> &str {
        match self {
            SwitchResult::Success => "success",
            SwitchResult::Failure(_) => "failure",
            SwitchResult::Rollback => "rollback",
        }
    }
}

impl serde::Serialize for SwitchResult {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        match self {
            SwitchResult::Success => s.serialize_str("success"),
            SwitchResult::Rollback => s.serialize_str("rollback"),
            SwitchResult::Failure(reason) => {
                // Fehlergrund bleibt inhaltsfrei (technischer Grund, kein Audio/Text).
                s.collect_str(&format!("failure: {reason}"))
            }
        }
    }
}

impl<'de> serde::Deserialize<'de> for SwitchResult {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let v = String::deserialize(d)?;
        if v == "success" {
            Ok(SwitchResult::Success)
        } else if v == "rollback" {
            Ok(SwitchResult::Rollback)
        } else if let Some(reason) = v.strip_prefix("failure: ") {
            Ok(SwitchResult::Failure(reason.to_string()))
        } else {
            Err(serde::de::Error::custom(format!("unbekanntes Switch-Ergebnis: {v}")))
        }
    }
}

/// Eine einzelne VRAM-/Compute-Probe (B6 Schritt 6–7). Inhaltsfrei: nur PIDs,
/// kein Speicher-Summen-Wert als Autorität.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct VramProbeRecord {
    /// Index der Probe innerhalb des Wechsels (0-basiert).
    pub probe_index: u32,
    /// `true` = kein aufgezeichneter PID mehr als Compute-Prozess.
    pub green: bool,
    /// Noch aktive jf-whisper-CUDA-PIDs (leer bei grün).
    pub active_pids: Vec<u32>,
    /// Abgeleitete zurechenbare MiB — nur nach Abschluss `0`, sonst `None`.
    pub attributable_mib: Option<u64>,
}

/// Das vollständige, inhaltsfreie Switch-Journal (Spec C).
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SwitchJournal {
    pub operation_id: String,
    pub direction: SwitchDirection,
    /// App-Epoche des Tauri-Prozesses.
    pub app_epoch: String,
    /// Generation des Ausgangs-Backends.
    pub from_generation: u64,
    /// Generation des Ziel-Backends (nach Abschluss).
    pub to_generation: Option<u64>,
    /// Job-Object-Entscheidung: wurde der alte Baum beendet?
    pub job_tree_terminated: bool,
    /// Prozessende des alten Backends bestätigt.
    pub process_end_confirmed: bool,
    /// Getrennte Phasenzeitanteile (AC-F).
    pub phases: Vec<PhaseTiming>,
    /// VRAM-/Compute-Proben (B6).
    pub vram_probes: Vec<VramProbeRecord>,
    /// Terminalergebnis.
    pub result: SwitchResult,
    /// Erstellungszeitpunkt (ms seit UNIX-Epoch).
    pub created_at_ms: u128,
    /// Abschlusszeitpunkt; `None`, solange der Wechsel läuft.
    pub terminal_at_ms: Option<u128>,
}

fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

/// Laufender Phasen-Timer: misst getrennte Zeitanteile für das Journal.
#[derive(Debug, Default)]
pub struct PhaseTimer {
    phases: Vec<PhaseTiming>,
    current_phase: Option<PhaseName>,
    current_start_ms: u128,
}

impl PhaseTimer {
    pub fn new() -> Self {
        Self::default()
    }

    /// Neue Phase beginnen; die vorherige wird mit ihrer Dauer abgeschlossen.
    pub fn begin(&mut self, phase: PhaseName) {
        if let Some(prev) = self.current_phase.take() {
            let started = self.current_start_ms;
            let dur = now_ms().saturating_sub(started);
            self.phases.push(PhaseTiming {
                phase: prev,
                started_at_ms: started,
                duration_ms: dur as u64,
            });
        }
        self.current_phase = Some(phase);
        self.current_start_ms = now_ms();
    }

    /// Aktuelle Phase abschließen (ohne neue zu beginnen).
    pub fn end(&mut self) {
        if let Some(prev) = self.current_phase.take() {
            let started = self.current_start_ms;
            let dur = now_ms().saturating_sub(started);
            self.phases.push(PhaseTiming {
                phase: prev,
                started_at_ms: started,
                duration_ms: dur as u64,
            });
        }
    }

    /// Gesamtdauer aller abgeschlossenen Phasen in ms.
    pub fn total_duration_ms(&self) -> u64 {
        self.phases.iter().map(|p| p.duration_ms).sum()
    }

    /// Abgeschlossene Phasen (Kopie) — für das Journal.
    pub fn into_phases(mut self) -> Vec<PhaseTiming> {
        self.end();
        self.phases
    }
}

/// Basisverzeichnis der Switch-Journale: `%LOCALAPPDATA%\JFWhisper\runtime\backend-operations`.
pub fn journal_dir() -> PathBuf {
    let local = std::env::var("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."));
    local.join("JFWhisper").join("runtime").join("backend-operations")
}

/// Laufender Switch-Evidenz-Sammler (Spec C, AC-F).
///
/// Der Supervisor-Aktor hält genau einen aktiven Sammler pro Wechsel. Er ist
/// inhaltsfrei: nur Phasen, PIDs und Zeitstempel — kein Audio-, Transkript- oder
/// Modellinhalt. `finish` schreibt das Journal atomar und liefert den Pfad.
#[derive(Debug)]
pub struct SwitchEvidence {
    operation_id: String,
    direction: SwitchDirection,
    app_epoch: String,
    from_generation: u64,
    to_generation: Option<u64>,
    job_tree_terminated: bool,
    process_end_confirmed: bool,
    timer: PhaseTimer,
    vram_probes: Vec<VramProbeRecord>,
    created_at_ms: u128,
}

impl SwitchEvidence {
    pub fn new(
        operation_id: String,
        direction: SwitchDirection,
        app_epoch: String,
        from_generation: u64,
    ) -> Self {
        let mut timer = PhaseTimer::new();
        // Der Wechsel beginnt mit Admission (B5 Schritt 1).
        timer.begin(PhaseName::Admitted);
        Self {
            operation_id,
            direction,
            app_epoch,
            from_generation,
            to_generation: None,
            job_tree_terminated: false,
            process_end_confirmed: false,
            timer,
            vram_probes: Vec::new(),
            created_at_ms: now_ms(),
        }
    }

    /// Neue Phase beginnen (B5/B6-Schritte).
    pub fn begin_phase(&mut self, phase: PhaseName) {
        self.timer.begin(phase);
    }

    /// VRAM-/Compute-Probe aufzeichnen (B6 Schritt 6–7).
    pub fn record_vram_probe(
        &mut self,
        green: bool,
        active_pids: Vec<u32>,
        attributable_mib: Option<u64>,
    ) {
        let probe_index = self.vram_probes.len() as u32;
        self.vram_probes.push(VramProbeRecord {
            probe_index,
            green,
            active_pids,
            attributable_mib,
        });
    }

    pub fn set_job_tree_terminated(&mut self) {
        self.job_tree_terminated = true;
    }

    pub fn set_process_end_confirmed(&mut self) {
        self.process_end_confirmed = true;
    }

    /// Wechsel terminal abschließen: Journal atomar nach `dir` schreiben
    /// (Temp-Verzeichnis in Tests, echtes Laufwerk über `finish`).
    pub fn finish_in(
        mut self,
        dir: &std::path::Path,
        result: SwitchResult,
    ) -> Result<PathBuf, String> {
        self.timer.end();
        let journal = SwitchJournal {
            operation_id: self.operation_id.clone(),
            direction: self.direction,
            app_epoch: self.app_epoch,
            from_generation: self.from_generation,
            to_generation: self.to_generation,
            job_tree_terminated: self.job_tree_terminated,
            process_end_confirmed: self.process_end_confirmed,
            phases: self.timer.into_phases(),
            vram_probes: self.vram_probes,
            result,
            created_at_ms: self.created_at_ms,
            terminal_at_ms: Some(now_ms()),
        };
        write_journal_in(dir, &journal)
    }

    /// Wechsel abschließen: Journal atomar ins echte Laufwerk schreiben, Pfad
    /// zurückgeben. Ein Schreibfehler wird geloggt — das Journal ist nie
    /// Erfolgsautorität allein (Spec C).
    pub fn finish(self, result: SwitchResult) -> PathBuf {
        let expected = journal_path(self.operation_id());
        match self.finish_in(&journal_dir(), result) {
            Ok(path) => path,
            Err(e) => {
                eprintln!("switch_evidence: Journal konnte nicht geschrieben werden: {e}");
                expected
            }
        }
    }

    /// Ziel-Generation setzen (nach erfolgreichem Start des neuen Backends).
    pub fn set_to_generation(&mut self, generation: u64) {
        self.to_generation = Some(generation);
    }

    pub fn operation_id(&self) -> &str {
        &self.operation_id
    }

    /// Aufgezeichnete VRAM-/Compute-Proben (Kopie) — für Snapshot/Diagnose.
    pub fn vram_probes(&self) -> &[VramProbeRecord] {
        &self.vram_probes
    }
}

/// Journal-Pfad für eine Operation in `dir` (windows-sicher über `Path::join`).
pub fn journal_path_in(dir: &std::path::Path, operation_id: &str) -> PathBuf {
    dir.join(format!("{operation_id}.json"))
}

/// Journal-Pfad für eine Operation im echten Journal-Laufwerk.
pub fn journal_path(operation_id: &str) -> PathBuf {
    journal_path_in(&journal_dir(), operation_id)
}

/// Atomares Schreiben des Journals nach `dir` (temp + rename). Inhaltsfrei,
/// crashrecoverbar — Tests laufen gegen ein Temp-Verzeichnis.
pub fn write_journal_in(dir: &std::path::Path, journal: &SwitchJournal) -> Result<PathBuf, String> {
    std::fs::create_dir_all(dir)
        .map_err(|e| format!("Journaldirectory nicht anlegbar: {e}"))?;
    let final_path = journal_path_in(dir, &journal.operation_id);
    // Temp-Datei im selben Verzeichnis → atomares Rename auf demselben Volume.
    let tmp_path = dir.join(format!(".{}.tmp", journal.operation_id));
    let json = serde_json::to_string_pretty(journal)
        .map_err(|e| format!("Journal-Serialisierung fehlgeschlagen: {e}"))?;
    std::fs::write(&tmp_path, json.as_bytes())
        .map_err(|e| format!("Journaltmp nicht schreibbar: {e}"))?;
    std::fs::rename(&tmp_path, &final_path)
        .map_err(|e| format!("Journal-Rename fehlgeschlagen: {e}"))?;
    Ok(final_path)
}

/// Atomares Schreiben ins echte Journal-Laufwerk: `write_journal_in` mit
/// `journal_dir()` (`%LOCALAPPDATA%/JFWhisper/runtime/backend-operations`).

/// Journal aus `dir` lesen (Recovery-Hinweis nach Crash). `None`, wenn nicht
/// vorhanden oder unlesbar.
pub fn read_journal_in(dir: &std::path::Path, operation_id: &str) -> Option<SwitchJournal> {
    let path = journal_path_in(dir, operation_id);
    if !path.exists() {
        return None;
    }
    let content = std::fs::read_to_string(&path).ok()?;
    serde_json::from_str(&content).ok()
}

/// Journal aus dem echten Journal-Laufwerk lesen (Recovery-Diagnose beim
/// App-Start — Spec C/E: unvollständige Operationen werden erkannt).
pub fn read_journal(operation_id: &str) -> Option<SwitchJournal> {
    read_journal_in(&journal_dir(), operation_id)
}

/// Alle Journal-Dateien in `dir` (Diagnose/Recovery). Leerer Vec, wenn leer.
pub fn list_journals_in(dir: &std::path::Path) -> Vec<PathBuf> {
    if !dir.exists() {
        return Vec::new();
    }
    std::fs::read_dir(dir)
        .into_iter()
        .flatten()
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|s| s.to_str()) == Some("json"))
        .collect()
}

/// Alle Journal-Dateien des echten Journal-Laufwerks (Recovery-Scan beim Start).
pub fn list_journals() -> Vec<PathBuf> {
    list_journals_in(&journal_dir())
}

/// Ergebnis der Freigabe-Verifikation (B6 Schritt 5–7).
#[derive(Debug, Clone, PartialEq)]
pub enum ReleaseOutcome {
    /// Zwei grüne Proben in Folge → `0 MiB zurechenbar`, Receipt abgeschlossen.
    Released { attributable_mib: u64 },
    /// Grüne Probe, aber das Receipt ist noch offen (erste grüne Probe) — der
    /// Aufrufer muss nach `min_interval` erneut proben (B6 Schritt 6).
    Pending,
    /// Mindestabstand/Probe-Fehler — der Supervisor muss erneut proben.
    ProbeError(String),
}

/// Freigabe-Verifikation eines CUDA-Wechsels (B6 Schritt 5–7).
///
/// Der Actor ruft dies im Blocking-Task auf, nachdem der CUDA-Job beendet und
/// die Prozessenden bestätigt wurden. Es führt den NVML-Probe-Zyklus aus:
/// `probe_fn` liefert eine frische [`GpuContextProbe`] (live von NVML), jede
/// Probe wird gegen das [`GpuReceipt`] geprüft und in das Journal aufgenommen.
/// Erst nach **zwei grünen Proben** mit ≥[`min_interval`] Abstand darf
/// `0 MiB zurechenbar` abgeleitet werden (B6 Schritt 7).
///
/// `probe_fn` ist injizierbar, damit der Zyklus hier unit-testbar bleibt; im
/// Produkt ruft main.rs/Actor [`GpuContextProbe::capture`] auf. Fail-closed:
/// ein NVML-Fehler wird als [`ReleaseOutcome::ProbeError`] zurückgegeben, nie
/// als Freigabe interpretiert.
pub fn run_release_verification(
    receipt: &mut GpuReceipt,
    evidence: &mut SwitchEvidence,
    min_interval: Duration,
    probe_fn: impl Fn() -> Result<GpuContextProbe, GpuEvidenceError>,
) -> ReleaseOutcome {
    // Phase VramCheck beginnt (AC-F: getrennter Zeitanteil).
    evidence.begin_phase(PhaseName::VramCheck);
    let probe = match probe_fn() {
        Ok(p) => p,
        Err(e) => return ReleaseOutcome::ProbeError(e.to_string()),
    };
    // Mindestabstand zwischen Wiederholungsproben (B6: „nach mindestens einer
    // Sekunde wiederholt"). Die erste Probe ist immer erlaubt.
    match receipt.verify(&probe, min_interval) {
        Ok(ReleaseVerification::Green) => {
            let attributable = if receipt.is_closed() {
                Some(receipt.attributable_mib().unwrap_or(0))
            } else {
                None // Noch nicht abgeschlossen → keine Ableitung (B6 Schritt 7).
            };
            evidence.record_vram_probe(true, Vec::new(), attributable);
            if receipt.is_closed() {
                ReleaseOutcome::Released {
                    attributable_mib: attributable.unwrap_or(0),
                }
            } else {
                // Erste grüne Probe — der Aufrufer muss erneut proben.
                ReleaseOutcome::Pending
            }
        }
        Ok(ReleaseVerification::Red(pids)) => {
            evidence.record_vram_probe(false, pids.clone(), None);
            // Rote Probe: Serie zurückgesetzt — erneut proben.
            ReleaseOutcome::ProbeError(format!("rote_probe:pids={}", pids.len()))
        }
        Err(e) => ReleaseOutcome::ProbeError(e.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backend::gpu_evidence::ComputeProcess;

    fn sample_journal() -> SwitchJournal {
        SwitchJournal {
            operation_id: "op-test".into(),
            direction: SwitchDirection::CudaToCpu,
            app_epoch: "epoch-1".into(),
            from_generation: 2,
            to_generation: Some(3),
            job_tree_terminated: true,
            process_end_confirmed: true,
            phases: vec![PhaseTiming {
                phase: PhaseName::Drain,
                started_at_ms: 1000,
                duration_ms: 250,
            }],
            vram_probes: vec![VramProbeRecord {
                probe_index: 0,
                green: true,
                active_pids: vec![],
                attributable_mib: Some(0),
            }],
            result: SwitchResult::Success,
            created_at_ms: 900,
            terminal_at_ms: Some(1300),
        }
    }

    #[test]
    fn journal_roundtrip_serialisiert_inhaltsfrei() {
        let j = sample_journal();
        let json = serde_json::to_string(&j).unwrap();
        // Inhaltsfreiheit: keine Audio-/Transkriptfelder.
        assert!(json.contains("cuda_to_cpu"));
        assert!(json.contains("drain"));
        let back: SwitchJournal = serde_json::from_str(&json).unwrap();
        assert_eq!(back.direction, SwitchDirection::CudaToCpu);
        assert_eq!(back.phases.len(), 1);
        assert_eq!(back.result, SwitchResult::Success);
    }

    #[test]
    fn phase_timer_misst_getrennte_anteile() {
        let mut t = PhaseTimer::new();
        t.begin(PhaseName::Admitted);
        std::thread::sleep(std::time::Duration::from_millis(5));
        t.begin(PhaseName::Drain);
        std::thread::sleep(std::time::Duration::from_millis(5));
        let phases = t.into_phases();
        assert_eq!(phases.len(), 2);
        assert_eq!(phases[0].phase, PhaseName::Admitted);
        assert_eq!(phases[1].phase, PhaseName::Drain);
        // Beide Phasen haben eine nicht-negative Dauer.
        assert!(phases.iter().all(|p| p.duration_ms >= 0));
    }

    #[test]
    fn phase_timer_end_ohne_neue_phase() {
        let mut t = PhaseTimer::new();
        t.begin(PhaseName::VramCheck);
        std::thread::sleep(std::time::Duration::from_millis(2));
        t.end();
        let phases = t.into_phases();
        assert_eq!(phases.len(), 1);
        assert_eq!(phases[0].phase, PhaseName::VramCheck);
    }

    #[test]
    fn journal_pfad_enthalt_operation_id() {
        let p = journal_path("op-abc");
        assert!(p.file_name().unwrap().to_string_lossy().contains("op-abc"));
        assert!(p.to_string_lossy().ends_with(".json"));
    }

    #[test]
    fn result_as_str_ist_stabil() {
        assert_eq!(SwitchResult::Success.as_str(), "success");
        assert_eq!(SwitchResult::Failure("x".into()).as_str(), "failure");
        assert_eq!(SwitchResult::Rollback.as_str(), "rollback");
    }

    #[test]
    fn direction_as_str_ist_stabil() {
        assert_eq!(SwitchDirection::CpuToCuda.as_str(), "cpu_to_cuda");
        assert_eq!(SwitchDirection::CudaToCpu.as_str(), "cuda_to_cpu");
    }

    // ── run_release_verification (B6 Schritt 5–7) ────────────────────────

    fn fake_probe(pids: &[u32]) -> GpuContextProbe {
        GpuContextProbe {
            device_name: "Test-GPU".to_string(),
            driver_version: "0.0-test".to_string(),
            total_vram_mib: 32_000,
            free_vram_mib: 30_000,
            compute_processes: pids
                .iter()
                .map(|pid| ComputeProcess {
                    pid: *pid,
                    used_gpu_memory_mib: None, // WDDM
                })
                .collect(),
        }
    }

    fn fresh_evidence() -> SwitchEvidence {
        SwitchEvidence::new("op-rel".into(), SwitchDirection::CudaToCpu, "epoch-1".into(), 2)
    }

    #[test]
    fn release_zwei_gruene_proben_leitet_null_mib_ab() {
        let mut receipt = GpuReceipt::from_smoke_probe(&fake_probe(&[4242]));
        let mut ev = fresh_evidence();
        // Erste Probe: PID weg → grün, aber noch nicht abgeschlossen (Pending).
        let r1 = run_release_verification(&mut receipt, &mut ev, Duration::ZERO, || Ok(fake_probe(&[])));
        assert!(matches!(r1, ReleaseOutcome::Pending), "erste Probe darf nicht freigegeben");
        // Zweite grüne Probe → abgeschlossen + 0 MiB.
        let r2 = run_release_verification(&mut receipt, &mut ev, Duration::ZERO, || Ok(fake_probe(&[])));
        assert_eq!(r2, ReleaseOutcome::Released { attributable_mib: 0 });
        // Beide Proben sind im Journal aufgezeichnet (inhaltsfrei).
        assert_eq!(ev.vram_probes().len(), 2);
        assert!(ev.vram_probes().iter().all(|p| p.green));
    }

    #[test]
    fn release_roter_pid_wird_aufgezeichnet_und_serie_bricht_ab() {
        let mut receipt = GpuReceipt::from_smoke_probe(&fake_probe(&[4242]));
        let mut ev = fresh_evidence();
        // PID noch aktiv → rote Probe, Serie zurückgesetzt.
        let r1 = run_release_verification(&mut receipt, &mut ev, Duration::ZERO, || Ok(fake_probe(&[4242])));
        assert!(matches!(r1, ReleaseOutcome::ProbeError(_)));
        assert_eq!(ev.vram_probes().len(), 1);
        assert!(!ev.vram_probes()[0].green);
        assert_eq!(ev.vram_probes()[0].active_pids, vec![4242]);
        // Danach zwei grüne Proben → Freigabe.
        run_release_verification(&mut receipt, &mut ev, Duration::ZERO, || Ok(fake_probe(&[])));
        let r3 = run_release_verification(&mut receipt, &mut ev, Duration::ZERO, || Ok(fake_probe(&[])));
        assert_eq!(r3, ReleaseOutcome::Released { attributable_mib: 0 });
    }

    #[test]
    fn release_nvml_fehler_wird_fail_closed() {
        let mut receipt = GpuReceipt::from_smoke_probe(&fake_probe(&[4242]));
        let mut ev = fresh_evidence();
        // NVML schlägt fehl → ProbeError, keine Freigabe.
        let r = run_release_verification(
            &mut receipt,
            &mut ev,
            Duration::ZERO,
            || Err(GpuEvidenceError::NoDevice),
        );
        assert!(matches!(r, ReleaseOutcome::ProbeError(_)));
        // Keine Probe wurde aufgezeichnet (die Abfrage selbst fehlgeschlagen).
        assert_eq!(ev.vram_probes().len(), 0);
    }

    #[test]
    fn release_mindestabstand_verweigert_zu_fruehe_wiederholung() {
        let mut receipt = GpuReceipt::from_smoke_probe(&fake_probe(&[4242]));
        let mut ev = fresh_evidence();
        // Erste Probe (immer erlaubt) → grün, nicht abgeschlossen.
        run_release_verification(&mut receipt, &mut ev, Duration::from_secs(1), || Ok(fake_probe(&[])));
        // Direkt danach: Mindestabstand noch nicht erreicht → ProbeError.
        let r2 = run_release_verification(&mut receipt, &mut ev, Duration::from_secs(1), || Ok(fake_probe(&[])));
        assert!(matches!(r2, ReleaseOutcome::ProbeError(_)));
    }

    /// Vollständiger Lifecycle wie im Actor: Admit → Phasen → zwei grüne Proben
    /// → `finish(Success)` → Journal atomar geschrieben und lesbar (Spec C).
    #[test]
    fn full_lifecycle_schreibt_lesbares_journal() {
        let op_id = "op-lifecycle-test";
        let mut ev = SwitchEvidence::new(op_id.into(), SwitchDirection::CudaToCpu, "epoch-1".into(), 2);
        // Phasen wie im Actor (B6): Drain → BackendEnd → VramCheck.
        ev.begin_phase(PhaseName::Drain);
        ev.set_process_end_confirmed();
        ev.set_job_tree_terminated();
        ev.begin_phase(PhaseName::VramCheck);
        // Zwei grüne Proben gegen das Receipt (fake NVML, kein CUDA nötig).
        let mut receipt = GpuReceipt::from_smoke_probe(&fake_probe(&[4242]));
        run_release_verification(&mut receipt, &mut ev, Duration::ZERO, || Ok(fake_probe(&[])));
        assert_eq!(run_release_verification(&mut receipt, &mut ev, Duration::ZERO, || Ok(fake_probe(&[]))), ReleaseOutcome::Released { attributable_mib: 0 });
        // Terminal abschließen → Journal atomar geschrieben.
        // JFW-12 Block (h): Journal-Lifecycle gegen ein Temp-Verzeichnis — kein
        // Test-Artefakt in den echten App-Daten (%LOCALAPPDATA%).
        let dir = temp_dir("full-lifecycle");
        let path = ev.finish_in(&dir, SwitchResult::Success).expect("Journal schreibbar");
        assert!(path.exists(), "Journal-Datei muss existieren");
        // Zurücklesen und prüfen (Recovery-Hinweis, Spec C).
        let j = read_journal_in(&dir, op_id).expect("Journal muss lesbar sein");
        let _ = std::fs::remove_dir_all(&dir);
        assert_eq!(j.operation_id, op_id);
        assert_eq!(j.direction, SwitchDirection::CudaToCpu);
        assert_eq!(j.result, SwitchResult::Success);
        assert!(j.process_end_confirmed);
        assert!(j.job_tree_terminated);
        // Getrennte Phasen ausgewiesen (AC-F): Admitted + Drain + VramCheck.
        let names: Vec<PhaseName> = j.phases.iter().map(|p| p.phase).collect();
        assert!(names.contains(&PhaseName::Admitted));
        assert!(names.contains(&PhaseName::Drain));
        assert!(names.contains(&PhaseName::VramCheck));
        // Zwei grüne VRAM-Proben aufgezeichnet; erst die abschließende Probe
        // leitet 0 MiB ab (B6 Schritt 7).
        assert_eq!(j.vram_probes.len(), 2);
        assert!(j.vram_probes.iter().all(|p| p.green));
        assert_eq!(j.vram_probes[1].attributable_mib, Some(0));
        // Terminalzeitpunkt gesetzt.
        assert!(j.terminal_at_ms.is_some());
    }

    // ── JFW-12 Block (h): Journal-I/O an das echte Laufwerk gebunden ──────
    // Produktion liest/schreibt das Journal-Verzeichnis unter LOCALAPPDATA (JFWhisper,
    // runtime, backend-operations) via `journal_dir()`/`Path::join` (windows-sicher);
    // die Tests laufen gegen ein Temp-Verzeichnis und hinterlassen nichts.

    fn temp_dir(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("jfw12-blockh-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).expect("Temp-Verzeichnis anlegbar");
        d
    }

    #[test]
    fn journal_io_temp_roundtrip_und_auflistung() {
        let dir = temp_dir("journal-io");
        let mut ev = SwitchEvidence::new("op-temp-1".into(), SwitchDirection::CudaToCpu, "epoch-1".into(), 2);
        ev.begin_phase(PhaseName::Drain);
        ev.set_job_tree_terminated();
        ev.set_process_end_confirmed();
        let path = ev.finish_in(&dir, SwitchResult::Success).expect("Journal schreibbar");
        assert!(path.exists());

        // read/list sind an dieselbe SwitchJournal-Ablage gebunden (Spec C).
        let j = read_journal_in(&dir, "op-temp-1").expect("Journal lesbar");
        assert_eq!(j.operation_id, "op-temp-1");
        assert!(j.job_tree_terminated && j.process_end_confirmed);
        assert!(j.terminal_at_ms.is_some(), "terminale Operation ist nicht unvollständig");
        assert!(read_journal_in(&dir, "gibts-nicht").is_none());

        // Nur *.json wird aufgelistet (kein Fremd-Müll im Verzeichnis).
        std::fs::write(dir.join("not-a-journal.txt"), b"x").unwrap();
        let listed = list_journals_in(&dir);
        assert_eq!(listed.len(), 1, "nur Journal-JSONs werden gelistet");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn unvollstaendiges_journal_ist_als_recovery_hinweis_erkennbar() {
        let dir = temp_dir("incomplete");
        // Crash-Abbild: Journal eröffnet, aber nie terminal abgeschlossen.
        let open = SwitchJournal {
            operation_id: "op-crash".into(),
            direction: SwitchDirection::CpuToCuda,
            app_epoch: "epoch-2".into(),
            from_generation: 3,
            to_generation: None,
            job_tree_terminated: false,
            process_end_confirmed: false,
            phases: vec![],
            vram_probes: vec![],
            result: SwitchResult::Failure("abgestürzt".into()),
            created_at_ms: 100,
            terminal_at_ms: None,
        };
        write_journal_in(&dir, &open).expect("Crash-Journal schreibbar");
        let j = read_journal_in(&dir, "op-crash").expect("lesbar");
        // Genau diese Bedingung nutzt der Recovery-Scan beim App-Start (Spec C/E).
        assert!(j.terminal_at_ms.is_none(), "unvollständige Operation muss erkennbar sein");
        let listed = list_journals_in(&dir);
        assert_eq!(listed.len(), 1);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
