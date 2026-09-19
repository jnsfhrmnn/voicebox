//! JFW-12 Block (e) — NVML-Evidence und VRAM-Nachweis (Spec B5 Schritt 6, B6 Schritt 6–7).
//!
//! Der Supervisor fragt pro Wechsel eine [`GpuContextProbe`] ab: Geräte-,
//! Treiber- und Speicherfakten plus die aktuell als Compute-Prozesse
//! geführten PIDs. Während des CUDA-Smokes wird ein [`GpuReceipt`] eröffnet,
//! der genau die während des Smokes sichtbaren jf-whisper-CUDA-PIDs
//! aufzeichnet (B5 Schritt 6). Nach dem Shutdown muss eine Probe zeigen, dass
//! kein aufgezeichneter PID mehr als Compute-Prozess geführt wird; erst nach
//! zwei grünen Proben darf `0 MiB zurechenbar` abgeleitet und das Receipt
//! abgeschlossen werden (B6 Schritt 6–7).
//!
//! WDDM-Realität: Unter Windows Display Driver Model meldet NVML pro Prozess
//! `NVML_VALUE_NOT_AVAILABLE` für den VRAM-Anteil — die zurechenbare MiB-Zahl
//! ist dann nicht aus NVML ableitbar. Das Receipt speichert deshalb die
//! PID-Evidenz als Primärnachweis und leitet `attributable_mib: 0` nur aus der
//! Abwesenheit der PIDs ab, nie aus einer Speicher-Summe (B6 Schritt 7).

use std::time::{Duration, Instant};

use nvml_wrapper::Nvml;

/// Fehler bei NVML-Zugriff — fail-closed, kein Panic.
#[derive(Debug)]
pub enum GpuEvidenceError {
    /// NVML lässt sich nicht initialisieren (kein Treiber / kein Gerät).
    NvmlInit(String),
    /// Kein GPU-Gerät gefunden.
    NoDevice,
    /// Einzelne Abfrage fehlgeschlagen.
    Query(String),
}

impl std::fmt::Display for GpuEvidenceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NvmlInit(m) => write!(f, "NVML-Initialisierung fehlgeschlagen: {m}"),
            Self::NoDevice => write!(f, "kein GPU-Gerät gefunden"),
            Self::Query(m) => write!(f, "NVML-Abfrage fehlgeschlagen: {m}"),
        }
    }
}

impl std::error::Error for GpuEvidenceError {}

/// Ein als Compute-Prozess geführter PID mit seinem (ggf. nicht verfügbaren)
/// VRAM-Anteil.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ComputeProcess {
    pub pid: u32,
    /// `None` = WDDM meldet `NVML_VALUE_NOT_AVAILABLE`.
    pub used_gpu_memory_mib: Option<u64>,
}

/// Kontextprobe eines GPU-Geräts (B5 Schritt 6, B6 Schritt 6).
#[derive(Debug, Clone)]
pub struct GpuContextProbe {
    pub device_name: String,
    pub driver_version: String,
    /// Installierter VRAM in MiB.
    pub total_vram_mib: u64,
    /// Frei gemeldeter VRAM in MiB (WDDM: Systemweite Sicht).
    pub free_vram_mib: u64,
    /// Alle als Compute-Prozesse geführten PIDs zum Probe-Zeitpunkt.
    pub compute_processes: Vec<ComputeProcess>,
}

impl GpuContextProbe {
    /// NVML initialisieren und das erste Gerät proben. Fail-closed.
    pub fn capture() -> Result<Self, GpuEvidenceError> {
        let nvml = Nvml::init().map_err(|e| GpuEvidenceError::NvmlInit(e.to_string()))?;
        let count = nvml.device_count().map_err(|e| GpuEvidenceError::Query(e.to_string()))?;
        if count == 0 {
            return Err(GpuEvidenceError::NoDevice);
        }
        let device = nvml
            .device_by_index(0)
            .map_err(|e| GpuEvidenceError::Query(e.to_string()))?;
        let name = device.name().map_err(|e| GpuEvidenceError::Query(e.to_string()))?;
        let mem = device.memory_info().map_err(|e| GpuEvidenceError::Query(e.to_string()))?;
        let procs = device
            .running_compute_processes()
            .map_err(|e| GpuEvidenceError::Query(e.to_string()))?;
        Ok(Self {
            driver_version: nvml
                .sys_driver_version()
                .unwrap_or_else(|_| "unbekannt".to_string()),
            device_name: name,
            total_vram_mib: mem.total / 1024 / 1024,
            free_vram_mib: mem.free / 1024 / 1024,
            compute_processes: procs
                .into_iter()
                .map(|p| ComputeProcess {
                    pid: p.pid,
                    used_gpu_memory_mib: match p.used_gpu_memory {
                        nvml_wrapper::enums::device::UsedGpuMemory::Used(bytes) => Some(bytes / 1024 / 1024),
                        nvml_wrapper::enums::device::UsedGpuMemory::Unavailable => None,
                    },
                })
                .collect(),
        })
    }

    /// Enthält die Probe den gegebenen PID als Compute-Prozess?
    pub fn contains_pid(&self, pid: u32) -> bool {
        self.compute_processes.iter().any(|p| p.pid == pid)
    }
}

/// Ergebnis der Freigabe-Verifikation (B6 Schritt 6–7).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReleaseVerification {
    /// Kein aufgezeichneter PID mehr als Compute-Prozess — grüne Probe.
    Green,
    /// Mindestens ein aufgezeichneter PID ist noch aktiv; mit den PIDs.
    Red(Vec<u32>),
}

/// Freigabe-Nachweis eines CUDA-Wechsels (B5 Schritt 6, B6 Schritt 7).
#[derive(Debug)]
pub struct GpuReceipt {
    /// PIDs, die während des Smokes als jf-whisper-CUDA-Compute-Prozesse
    /// sichtbar waren.
    recorded_pids: Vec<u32>,
    /// Anzahl aufeinanderfolgender grüner Proben seit Öffnung.
    green_streak: u32,
    /// Zeitpunkt der letzten Probe — Mindestabstand zwischen Wiederholungen.
    last_probe_at: Option<Instant>,
    closed: bool,
}

impl GpuReceipt {
    /// Receipt aus der Smoke-Probe eröffnen (B5 Schritt 6).
    pub fn from_smoke_probe(probe: &GpuContextProbe) -> Self {
        Self {
            recorded_pids: probe.compute_processes.iter().map(|p| p.pid).collect(),
            green_streak: 0,
            last_probe_at: None,
            closed: false,
        }
    }

    /// Receipt für eine explizite PID-Menge eröffnen — z. B. nur der bekannte
    /// jf-whisper-CUDA-PID (B6: „keinen aufgezeichneten jf-whisper-CUDA-PID").
    /// Unbeachtete fremde Compute-Prozesse dürfen die Freigabe nicht rot machen.
    pub fn for_pids(pids: impl IntoIterator<Item = u32>) -> Self {
        Self {
            recorded_pids: pids.into_iter().collect(),
            green_streak: 0,
            last_probe_at: None,
            closed: false,
        }
    }

    /// Aufgezeichnete PIDs (Kopie) — für Logs und das Operationsjournal.
    pub fn recorded_pids(&self) -> &[u32] {
        &self.recorded_pids
    }

    /// Ist der Receipt bereits nach zwei grünen Proben abgeschlossen?
    pub fn is_closed(&self) -> bool {
        self.closed
    }

    /// Eine Probe gegen das Receipt prüfen (B6 Schritt 6).
    ///
    /// `min_interval` ist die geforderte Mindestabstand zwischen den Proben —
    /// der Supervisor ruft mit ≥1 s auf, damit die zweite Probe eine echte
    /// Wiederholung ist (B6: „nach mindestens einer Sekunde wiederholt").
    pub fn verify(
        &mut self,
        probe: &GpuContextProbe,
        min_interval: Duration,
    ) -> Result<ReleaseVerification, GpuEvidenceError> {
        if self.closed {
            return Ok(ReleaseVerification::Green);
        }
        // Mindestabstand zwischen den Wiederholungsproben (B6: „nach mindestens
        // einer Sekunde wiederholt"). Die erste Probe ist immer erlaubt.
        if let Some(last) = self.last_probe_at {
            if last.elapsed() < min_interval {
                return Err(GpuEvidenceError::Query(
                    "Probe zu früh (Mindestabstand noch nicht erreicht)".to_string(),
                ));
            }
        }
        self.last_probe_at = Some(Instant::now());
        let still_active: Vec<u32> = self
            .recorded_pids
            .iter()
            .filter(|pid| probe.contains_pid(**pid))
            .copied()
            .collect();
        if still_active.is_empty() {
            self.green_streak += 1;
            if self.green_streak >= 2 {
                self.closed = true;
            }
            Ok(ReleaseVerification::Green)
        } else {
            // Rote Probe bricht die Serie — der Supervisor muss erneut von
            // null grünen Proben ansetzen.
            self.green_streak = 0;
            Ok(ReleaseVerification::Red(still_active))
        }
    }

    /// `0 MiB zurechenbar` ableiten (B6 Schritt 7) — nur nach Abschluss.
    ///
    /// Die Ableitung ist PID-basiert, nicht Speicher-Summe: Unter WDDM sind
    /// prozessweise VRAM-Anteile ohnehin nicht verfügbar (`None`).
    pub fn attributable_mib(&self) -> Result<u64, GpuEvidenceError> {
        if !self.closed {
            return Err(GpuEvidenceError::Query(
                "Receipt nicht abgeschlossen — Ableitung verweigert".to_string(),
            ));
        }
        Ok(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn probe_with(pids: &[u32]) -> GpuContextProbe {
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

    #[test]
    fn receipt_gruene_probe_zweimal_abschliesst() {
        let mut r = GpuReceipt::from_smoke_probe(&probe_with(&[4242]));
        assert!(!r.is_closed());
        // Erste Probe: PID weg → grün, aber noch nicht abgeschlossen.
        assert_eq!(
            r.verify(&probe_with(&[]), Duration::ZERO).unwrap(),
            ReleaseVerification::Green
        );
        assert!(!r.is_closed());
        // Zweite grüne Probe → abgeschlossen.
        assert_eq!(
            r.verify(&probe_with(&[]), Duration::ZERO).unwrap(),
            ReleaseVerification::Green
        );
        assert!(r.is_closed());
        assert_eq!(r.attributable_mib().unwrap(), 0);
    }

    #[test]
    fn receipt_roter_pid_bricht_serie_ab() {
        let mut r = GpuReceipt::from_smoke_probe(&probe_with(&[4242]));
        // PID noch aktiv → rot.
        assert_eq!(
            r.verify(&probe_with(&[4242]), Duration::ZERO).unwrap(),
            ReleaseVerification::Red(vec![4242])
        );
        // Grün, dann wieder rot — Serie zurückgesetzt.
        assert_eq!(r.verify(&probe_with(&[]), Duration::ZERO).unwrap(), ReleaseVerification::Green);
        assert!(!r.is_closed());
        assert_eq!(
            r.verify(&probe_with(&[4242]), Duration::ZERO).unwrap(),
            ReleaseVerification::Red(vec![4242])
        );
        // Erst jetzt zwei grüne Proben in Folge.
        assert_eq!(r.verify(&probe_with(&[]), Duration::ZERO).unwrap(), ReleaseVerification::Green);
        assert!(!r.is_closed());
        assert_eq!(r.verify(&probe_with(&[]), Duration::ZERO).unwrap(), ReleaseVerification::Green);
        assert!(r.is_closed());
    }

    #[test]
    fn receipt_ohne_pids_braucht_keine_freigabe() {
        // Smoke hat keinen Compute-PID gezeigt (z. B. Smoke zu früh beendet):
        // zwei grüne Proben genügen, aber der Nachweis ist leer — der Supervisor
        // muss den fehlenden PID als Evidenzlücke behandeln, nicht als Erfolg.
        let mut r = GpuReceipt::from_smoke_probe(&probe_with(&[]));
        assert!(r.recorded_pids().is_empty());
        assert_eq!(r.verify(&probe_with(&[]), Duration::ZERO).unwrap(), ReleaseVerification::Green);
        assert_eq!(r.verify(&probe_with(&[]), Duration::ZERO).unwrap(), ReleaseVerification::Green);
        assert!(r.is_closed());
    }

    #[test]
    fn attributable_verweigert_vor_abschluss() {
        let mut r = GpuReceipt::from_smoke_probe(&probe_with(&[1]));
        assert_eq!(
            r.verify(&probe_with(&[]), Duration::ZERO).unwrap(),
            ReleaseVerification::Green
        );
        // Noch nicht abgeschlossen → Ableitung verweigert.
        assert!(r.attributable_mib().is_err());
    }

    #[test]
    fn probe_contains_pid_prueft_membership() {
        let p = probe_with(&[7, 8]);
        assert!(p.contains_pid(7));
        assert!(!p.contains_pid(9));
    }

    #[test]
    fn probe_zu_frueh_wird_verweigert() {
        let mut r = GpuReceipt::from_smoke_probe(&probe_with(&[4242]));
        // Erste Probe ist immer erlaubt.
        assert_eq!(r.verify(&probe_with(&[]), Duration::from_secs(1)).unwrap(), ReleaseVerification::Green);
        // Direkt danach: zu früh → Fehler, Serie bleibt unangetastet.
        assert!(r.verify(&probe_with(&[]), Duration::from_secs(1)).is_err());
        assert!(!r.is_closed());
    }

    /// Echte Hardware: NVML muss initialisieren und das erste Gerät proben.
    /// Auf Maschinen ohne NVIDIA-Treiber wird der Fail-closed-Pfad geprüft.
    #[test]
    fn capture_auf_realer_hardware_oder_fail_closed() {
        match GpuContextProbe::capture() {
            Ok(probe) => {
                assert!(!probe.device_name.is_empty());
                assert!(probe.total_vram_mib > 0);
                // Driver-Version ist frei formatiert — nur Nicht-leer prüfen.
                assert!(!probe.driver_version.is_empty());
            }
            Err(e) => {
                // Fail-closed ist erlaubt, aber muss ein sauberer Fehler sein.
                let msg = e.to_string();
                assert!(!msg.is_empty());
            }
        }
    }
}
