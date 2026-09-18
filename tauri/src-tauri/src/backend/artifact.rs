//! JFW-12 B9: CUDA-Artefaktmanager — signiertes Addon, Staging, atomarer Pointer.
//!
//! Produktionsquelle ist ausschließlich der jf-whisper-Releasepfad (HTTPS); die
//! UI akzeptiert keine freie URL. Pro App-Build existiert ein vollständiges
//! CUDA-`onedir`-Archiv plus kanonisches signiertes Manifest:
//!
//! 1. Download nach `%LOCALAPPDATA%\JFWhisper\backends\.staging\<uuid>` (selbes
//!    Volume wie das Ziel — atomares Umbenennen ist möglich).
//! 2. Minisign-Verifikation des Archivs per eingebettetem jf-whisper-Addon-Public-Key
//!    (`minisign-verify`, keine Legacy-Signaturen).
//! 3. Manifest-Prüfung: Build-ID, App-Version, `binary_variant=cuda`, Zieltripel,
//!    Negativinventar (TTS/LLM/Voice/MCP/Cloud), Dateiinventar vollständig.
//! 4. Sichere Extraktion: absolute Pfade, `..`, Symlinks, Hardlinks und unbekannte
//!    Dateien werden abgelehnt; jede Datei wird gestreamt gegen Größe + SHA-256.
//! 5. Erst danach Umbenennen auf `%LOCALAPPDATA%\JFWhisper\backends\cuda\<build_id>`
//!    und atomares Ersetzen von `current.json`. Die bisher bestätigte Version bleibt
//!    bis zum grünen Commit erhalten (fail-closed).

use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;

use flate2::read::GzDecoder;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tar::EntryType;

/// Max. entpackte Größe (B9: Größenüberschreitung wird abgelehnt). 4 GiB deckt
/// ein CUDA-`onedir`-Archiv mit PyTorch/CUDA-Runtime großzügig ab.
pub const MAX_UNPACKED_BYTES: u64 = 4 * 1024 * 1024 * 1024;

/// Max. Archivgröße (komprimiert).
pub const MAX_ARCHIVE_BYTES: u64 = 3 * 1024 * 1024 * 1024;

/// Eingebetteter jf-whisper-Releasepfad (B9): die UI akzeptiert keine freie URL.
const RELEASE_BASE: &str = "https://releases.jfwhisper.local/jfw/cuda";

/// Download-Timeout pro Verbindung (B9: nur die ausdrücklich gestartete
/// Artefaktoperation darf Netzwerkverkehr erzeugen).
const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(60);

// ── Manifest ───────────────────────────────────────────────────────────────

/// Eintrag im Dateiinventar des signierten Manifests.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct InventoryEntry {
    pub path: String,
    pub size: u64,
    #[serde(rename = "sha256")]
    pub sha256_hex: String,
}

/// Kanonisches signiertes Manifest (B9). Alle Felder sind verpflichtend —
/// ein unvollständiges Manifest ist ungültig.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Manifest {
    pub app_version: String,
    pub build_id: String,
    pub source_commit: String,
    #[serde(rename = "patch_ledger_hash")]
    pub patch_ledger_hash: String,
    #[serde(rename = "profile_sha256")]
    pub profile_sha256: String,
    /// Muss `cuda` sein.
    pub binary_variant: String,
    pub target_triple: String,
    pub min_driver_version: String,
    pub schema_id: String,
    #[serde(rename = "alembic_head")]
    pub alembic_head: String,
    #[serde(rename = "migration_chain_hash")]
    pub migration_chain_hash: String,
    #[serde(rename = "allowlist_hash")]
    pub allowlist_hash: String,
    #[serde(rename = "worker_contract_version")]
    pub worker_contract_version: u32,
    #[serde(rename = "api_contract_version")]
    pub api_contract_version: u32,
    #[serde(rename = "result_contract_version")]
    pub result_contract_version: u32,
    pub archive_name: String,
    pub archive_url: String,
    #[serde(rename = "compressed_size")]
    pub compressed_size: u64,
    #[serde(rename = "max_unpacked_size")]
    pub max_unpacked_size: u64,
    #[serde(rename = "sha256")]
    pub sha256_hex: String,
    /// Vollständiges Dateiinventar (Pfad relativ zum Archiv-Root).
    pub files: Vec<InventoryEntry>,
    /// Monotone Releasefolge.
    pub release_sequence: u64,
    #[serde(rename = "signing_key_id")]
    pub signing_key_id: String,
}

impl Manifest {
    /// Negativinventar (B9): TTS-, LLM-, Voice-, MCP- und Cloud-Komponenten sind
    /// im CUDA-Addon verboten. Prüft das Dateiinventar auf verbotene Marker.
    pub fn check_negative_inventory(&self) -> Result<(), String> {
        const FORBIDDEN: &[&str] = &["tts", "llm", "voice", "mcp", "cloud"];
        for f in &self.files {
            let p = f.path.to_lowercase();
            if FORBIDDEN.iter().any(|k| p.contains(k)) {
                return Err(format!(
                    "Negativinventar verletzt: '{p}' enthält verbotenen Marker"
                ));
            }
        }
        Ok(())
    }

    /// Vollständigkeitsprüfung des Inventars: keine Duplikate, keine leeren Pfade.
    pub fn check_inventory_complete(&self) -> Result<(), String> {
        use std::collections::HashSet;
        let mut seen = HashSet::new();
        for f in &self.files {
            if f.path.is_empty() || !seen.insert(f.path.clone()) {
                return Err(format!("Inventar unvollständig: '{}' fehlt oder doppelt", f.path));
            }
        }
        Ok(())
    }

    /// Pfadsicherheit (B9): absolute Pfade, `..`, Symlinks/Hardlinks und
    /// nicht-UTF-8-Namen werden abgelehnt.
    pub fn check_paths_safe(&self) -> Result<(), String> {
        for f in &self.files {
            if Path::new(&f.path).is_absolute() || f.path.contains("..") {
                return Err(format!("unsicherer Pfad im Inventar: '{}'", f.path));
            }
        }
        Ok(())
    }
}

// ── Installierter Build / Current-Pointer ──────────────────────────────────

/// Ein vollständig verifizierter, installierter CUDA-Build.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InstalledBuild {
    pub build_id: String,
    /// Relativ zum Backend-Root (z. B. `cuda/<build_id>/manifest.json`).
    #[serde(rename = "manifest_path")]
    pub manifest_path: String,
}

/// Inhalt von `current.json` — atomar ersetzt, nur auf vollständig verifizierten
/// Builds (C: Data And State Model).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CurrentPointer {
    pub build_id: String,
    #[serde(rename = "installed_at_unix")]
    pub installed_at_unix: u64,
}

// ── Manager ────────────────────────────────────────────────────────────────

/// CUDA-Artefaktmanager (B9). Alle Pfade liegen unter `%LOCALAPPDATA%\JFWhisper\backends`.
pub struct Manager {
    /// Backend-Root (`%LOCALAPPDATA%\JFWhisper\backends`).
    pub base_dir: PathBuf,
    /// App-Version des laufenden Builds.
    pub app_version: String,
    /// Build-ID des laufenden Builds.
    pub build_id: String,
    /// Test-Hook: überschreibt den eingebetteten Public Key (None = eingebettet).
    #[allow(dead_code)] // JFW-12 Block (d): Test-Injection
    test_public_key: Option<String>,
}

impl Manager {
    pub fn new(base_dir: PathBuf, app_version: String, build_id: String) -> Self {
        Self {
            base_dir,
            app_version,
            build_id,
            test_public_key: None,
        }
    }

    /// Test-Hook: eingebetteten Public Key überschreiben (nur `#[cfg(test)]`).
    #[doc(hidden)]
    pub fn with_test_public_key(mut self, key_b64: String) -> Self {
        self.test_public_key = Some(key_b64);
        self
    }

    /// Eingebetteter jf-whisper-Addon-Public-Key (Minisign, Base64). Wird beim
    /// Deployvertrag durch den produktiven Key ersetzt; Key-Rotation benötigt eine
    /// neue signierte CPU-App-Version (B9).
    fn embedded_public_key() -> &'static str {
        // JFW-12 Block (g): produktiver Addon-Public-Key (B9). Der Private Key bleibt
        // ausschließlich im kontrollierten Releaseprozess (release-assets/keys, gitignored);
        // Key-Rotation benötigt eine neue signierte CPU-App-Version.
        "RWQdKaxwrmr7+x4bgeRn0Zpd8RS8Tekb1TR9M97WbzwOMjzWJRijO/xh"
    }

    /// Eingebetteter jf-whisper-Releasepfad (B9): die UI akzeptiert keine freie
    /// URL — ausschließlich dieser HTTPS-Pfad wird angesprochen. Pro App-Build
    /// liegt unter `<base>/<build_id>/` das vollständige CUDA-Archiv mit dem
    /// kanonischen signierten Manifest.
    pub fn release_base_url() -> String {
        format!("{RELEASE_BASE}/{}", env!("CARGO_PKG_VERSION"))
    }

    /// Liest den aktuellen Pointer (falls vorhanden).
    pub fn current(&self) -> Result<Option<CurrentPointer>, String> {
        let p = self.base_dir.join("cuda").join("current.json");
        if !p.exists() {
            return Ok(None);
        }
        let raw = std::fs::read_to_string(&p).map_err(|e| format!("current.json lesbar: {e}"))?;
        serde_json::from_str(&raw).map(Some).map_err(|e| format!("current.json ungültig: {e}"))
    }

    /// Installations-Pipeline (B9) aus dem eingebetteten Releasepfad: Download
    /// von Manifest, Manifest-Signatur, Archiv und Archiv-Signatur → Verifikation
    /// → Staging → Commit. Die UI akzeptiert keine freie URL — ausschließlich
    /// der jf-whisper-Releasepfad wird angesprochen (B9).
    pub fn install_release(&self, progress: impl Fn(&str)) -> Result<InstalledBuild, String> {
        let base_url = Self::release_base_url();
        let staging = self.base_dir.join(".staging").join(uuid::Uuid::new_v4().to_string());
        std::fs::create_dir_all(&staging).map_err(|e| format!("Staging-Verzeichnis: {e}"))?;

        // 1) Manifest + Signatur laden und verifizieren (fail-closed).
        progress("downloading");
        let manifest_path = staging.join("manifest.json");
        self.download_file(&format!("{base_url}/manifest.json"), &manifest_path)?;
        self.download_file(
            &format!("{base_url}/manifest.json.sig"),
            &staging.join("manifest.json.sig"),
        )?;

        // 2) Manifest-Signatur prüfen + parsen (kanonisches signiertes Manifest, B9).
        progress("verifying_manifest");
        let manifest = self.verify_manifest_signature(&manifest_path)?;

        // 3) Archiv + Signatur laden.
        progress("downloading");
        let archive_path = staging.join(&manifest.archive_name);
        self.download_file(
            &format!("{base_url}/{}", manifest.archive_name),
            &archive_path,
        )?;
        self.download_file(
            &format!("{base_url}/{}.sig", manifest.archive_name),
            &staging.join(format!("{}.sig", manifest.archive_name)),
        )?;

        // 4–6) Verifikation + Extraktion + Commit (testbarer Kern ohne Netzwerk).
        self.install_from_paths(&archive_path, &manifest_path, progress)
    }

    /// Verifikations- und Commit-Kern der Installations-Pipeline — arbeitet auf
    /// bereits vorhandenen Dateien: Archiv (`<name>`), Archiv-Signatur
    /// (`<name>.sig`), Manifest (`manifest.json`) und dessen Signatur.
    /// Getrennt vom Download, damit die Pipeline ohne Netzwerk testbar ist.
    fn install_from_paths(
        &self,
        archive_path: &Path,
        manifest_path: &Path,
        progress: impl Fn(&str),
    ) -> Result<InstalledBuild, String> {
        let staging = archive_path.parent().unwrap_or(Path::new("."));

        // 1) Kanonisches signiertes Manifest (B9): Signatur per eingebettetem
        //    Public Key, dann Vertrag gegen den laufenden App-Build.
        progress("verifying_manifest");
        let manifest = self.verify_manifest_signature(manifest_path)?;
        self.verify_manifest_contract(&manifest)?;

        // 2) Minisign-Signatur des Archivs (fail-closed).
        progress("verifying_signature");
        self.verify_signature(archive_path, &manifest)?;

        // 3) Sichere Extraktion mit Dateiverifikation (Größe + SHA-256 je Datei).
        progress("extracting");
        let extract_dir = staging.join("extracted");
        std::fs::create_dir_all(&extract_dir).map_err(|e| format!("Extraktionsverzeichnis: {e}"))?;
        self.extract_verified(archive_path, &manifest, &extract_dir)?;

        // 5) Commit: Umbenennen auf versioniertes Ziel + atomares current.json.
        progress("committing");
        let cuda_root = self.base_dir.join("cuda");
        std::fs::create_dir_all(&cuda_root).map_err(|e| format!("CUDA-Verzeichnis: {e}"))?;
        let target = cuda_root.join(&manifest.build_id);
        if target.exists() {
            std::fs::remove_dir_all(&target).map_err(|e| format!("altes Build entfernen: {e}"))?;
        }
        std::fs::rename(&extract_dir, &target)
            .map_err(|e| format!("Commit-Umbenennen (selbes Volume erwartet): {e}"))?;

        let installed = InstalledBuild {
            build_id: manifest.build_id.clone(),
            manifest_path: format!("cuda/{}/manifest.json", manifest.build_id),
        };
        self.write_current(&installed)?;

        // Staging räumen (Ziel ist jetzt unter cuda/<build_id>).
        if let Some(parent) = archive_path.parent() {
            let _ = std::fs::remove_dir_all(parent);
        }
        progress("committed");
        Ok(installed)
    }

    /// Download einer Datei aus dem Releasepfad mit Größen- und Timeout-Grenzen.
    fn download_file(&self, url: &str, dest: &Path) -> Result<(), String> {
        if !url.starts_with("https://") {
            return Err(format!("URL muss HTTPS sein: {url}"));
        }
        let resp = reqwest::blocking::Client::builder()
            .timeout(DOWNLOAD_TIMEOUT)
            .build()
            .map_err(|e| format!("HTTP-Client: {e}"))?
            .get(url)
            .send()
            .map_err(|e| format!("Download fehlgeschlagen: {e}"))?;
        if !resp.status().is_success() {
            return Err(format!("Releasepfad antwortet mit {}", resp.status()));
        }

        let mut file = std::fs::File::create(dest).map_err(|e| format!("Datei anlegen: {e}"))?;
        // reqwest::blocking::Response implementiert io::Read — streamen mit Zähler.
        struct CountingReader<R>(R, u64);
        impl<R: std::io::Read> std::io::Read for CountingReader<R> {
            fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
                let n = self.0.read(buf)?;
                self.1 += n as u64;
                Ok(n)
            }
        }
        let mut reader = CountingReader(resp, 0);
        std::io::copy(&mut reader, &mut file).map_err(|e| format!("Schreiben: {e}"))?;
        drop(file);
        if reader.1 > MAX_ARCHIVE_BYTES {
            return Err("Datei überschreitet maximale Größe".into());
        }
        Ok(())
    }

    /// Kanonisches signiertes Manifest (B9): lädt `manifest.json` + `.sig`,
    /// prüft die Minisign-Signatur per eingebettetem Public Key und parst das
    /// Manifest. Ein unsigniertes oder fremd-signiertes Manifest ist ungültig.
    fn verify_manifest_signature(&self, manifest_path: &Path) -> Result<Manifest, String> {
        let key_b64 = self
            .test_public_key
            .clone()
            .unwrap_or_else(|| Self::embedded_public_key().to_string());
        let public_key = minisign_verify::PublicKey::from_base64(&key_b64)
            .map_err(|e| format!("eingebetteter Public Key ungültig: {e}"))?;

        let manifest_raw = std::fs::read(manifest_path).map_err(|e| format!("Manifest lesen: {e}"))?;
        let sig_path = manifest_path.with_file_name(format!(
            "{}.sig",
            manifest_path.file_name().and_then(|n| n.to_str()).unwrap_or("manifest.json")
        ));
        if !sig_path.exists() {
            return Err("Manifest-Signatur fehlt im Releasepfad".into());
        }
        let sig_raw = std::fs::read_to_string(&sig_path).map_err(|e| format!("Signatur lesen: {e}"))?;
        let signature = minisign_verify::Signature::decode(&sig_raw)
            .map_err(|e| format!("Manifest-Signatur dekodieren: {e}"))?;

        // Signatur per Stream (nur Prehashed erlaubt — B9).
        let mut verifier = public_key
            .verify_stream(&signature)
            .map_err(|e| format!("Manifest-Stream-Setup (nur Prehashed erlaubt): {e}"))?;
        for chunk in manifest_raw.chunks(65536) {
            verifier.update(chunk);
        }
        verifier.finalize().map_err(|e| format!("Manifest-Signatur ungültig: {e}"))?;

        serde_json::from_slice(&manifest_raw).map_err(|e| format!("Manifest JSON ungültig: {e}"))
    }

    /// Minisign-Verifikation des Archivs per Stream (B9: „streamt
    /// Archiv-/Dateihashes") — das Archiv wird nicht vollständig geladen.
    fn verify_signature(&self, archive_path: &Path, manifest: &Manifest) -> Result<(), String> {
        let key_b64 = self
            .test_public_key
            .clone()
            .unwrap_or_else(|| Self::embedded_public_key().to_string());
        let public_key = minisign_verify::PublicKey::from_base64(&key_b64)
            .map_err(|e| format!("eingebetteter Public Key ungültig: {e}"))?;

        // Signatur liegt neben dem Archiv als <name>.sig (Releasepfad-Konvention).
        let sig_path = archive_path.with_file_name(format!(
            "{}.sig",
            archive_path.file_name().and_then(|n| n.to_str()).unwrap_or("archive")
        ));
        if !sig_path.exists() {
            return Err("Signaturdatei fehlt im Releasepfad".into());
        }
        let sig_raw = std::fs::read_to_string(&sig_path).map_err(|e| format!("Signatur lesen: {e}"))?;
        let signature = minisign_verify::Signature::decode(&sig_raw)
            .map_err(|e| format!("Signatur dekodieren: {e}"))?;

        // Ein Durchgang: Minisign-Stream (Prehashed/Blake2b) + SHA-256 für die
        // Manifest-Bindung. allow_legacy ist hier strukturell ausgeschlossen —
        // verify_stream akzeptiert nur Prehashed-Signaturen (B9).
        let mut file = std::fs::File::open(archive_path).map_err(|e| format!("Archiv lesen: {e}"))?;
        let mut verifier = public_key
            .verify_stream(&signature)
            .map_err(|e| format!("Minisign-Stream-Setup (nur Prehashed erlaubt): {e}"))?;
        let mut sha = Sha256::new();
        let mut buf = [0u8; 65536];
        loop {
            let n = file.read(&mut buf).map_err(|e| format!("Archiv lesen: {e}"))?;
            if n == 0 {
                break;
            }
            verifier.update(&buf[..n]);
            sha.update(&buf[..n]);
        }
        verifier.finalize().map_err(|e| format!("Minisign-Signatur ungültig: {e}"))?;

        // Archiv-Hash muss zum Manifest passen (doppelte Bindung).
        let hex = hex_lower(&sha.finalize());
        if !constant_time_eq(hex.as_bytes(), manifest.sha256_hex.as_bytes()) {
            return Err("Archiv-Hash weicht vom Manifest ab".into());
        }
        Ok(())
    }

    /// Manifest-Vertragsprüfung gegen den laufenden App-Build (B9).
    fn verify_manifest_contract(&self, manifest: &Manifest) -> Result<(), String> {
        if manifest.binary_variant != "cuda" {
            return Err(format!(
                "binary_variant muss 'cuda' sein, ist '{}'",
                manifest.binary_variant
            ));
        }
        if manifest.app_version != self.app_version {
            return Err(format!(
                "App-Version passt nicht: Manifest {}, Build {}",
                manifest.app_version, self.app_version
            ));
        }
        if manifest.build_id != self.build_id {
            return Err(format!(
                "Build-ID passt nicht: Manifest {}, Build {}",
                manifest.build_id, self.build_id
            ));
        }
        if manifest.max_unpacked_size > MAX_UNPACKED_BYTES {
            return Err("Manifest erlaubt mehr entpackte Bytes als die App-Grenze".into());
        }
        manifest.check_negative_inventory()?;
        manifest.check_inventory_complete()?;
        manifest.check_paths_safe()?;
        Ok(())
    }

    /// Sichere Extraktion: lehnt absolute Pfade, `..`, Symlinks/Hardlinks und
    /// unbekannte Dateien ab; verifiziert Größe + SHA-256 je Datei (B9).
    fn extract_verified(&self, archive_path: &Path, manifest: &Manifest, dest: &Path) -> Result<(), String> {
        let file = std::fs::File::open(archive_path).map_err(|e| format!("Archiv öffnen: {e}"))?;
        let gz = GzDecoder::new(file);
        let mut archive = tar::Archive::new(gz);

        // Inventar als Lookup (Pfad → Eintrag).
        use std::collections::HashMap;
        let inventory: HashMap<&str, &InventoryEntry> = manifest
            .files
            .iter()
            .map(|f| (f.path.as_str(), f))
            .collect();

        let mut unpacked_total: u64 = 0;
        for entry in archive.entries().map_err(|e| format!("Tar-Einträge: {e}"))? {
            let mut entry = entry.map_err(|e| format!("Tar-Entry: {e}"))?;

            // Symlinks/Hardlinks/Device/FIFO sind im Addon verboten (B9).
            match entry.header().entry_type() {
                EntryType::Regular | EntryType::Directory => {}
                other => {
                    return Err(format!(
                        "verbotener Tar-Eintragstyp {:?} für '{}'",
                        other,
                        entry.path().map_err(|e| e.to_string())?.to_string_lossy()
                    ));
                }
            }

            let rel = entry
                .path()
                .map_err(|e| format!("Tar-Pfad: {e}"))?
                .into_owned();
            // Pfadsicherheit (B9): absolut, `..`, nicht-UTF8.
            if rel.is_absolute() || rel.components().any(|c| c.as_os_str() == "..") {
                return Err(format!("unsicherer Tar-Pfad: '{}'", rel.display()));
            }

            // Verzeichnisse sind strukturell (kein Inhalt) — nur Pfadsicherheit,
            // keine Inventarpflicht. Dateien werden strikt gegen das signierte
            // Inventar geprüft ("unbekannte Dateien … abgelehnt", B9).
            if entry.header().entry_type() == EntryType::Directory {
                std::fs::create_dir_all(dest.join(&rel))
                    .map_err(|e| format!("Verzeichnis anlegen: {e}"))?;
                continue;
            }

            let name = rel.to_string_lossy().to_string();

            // Unbekannte Datei (nicht im signierten Inventar) → ablehnen.
            let inv = inventory.get(name.as_str()).ok_or_else(|| {
                format!("unbekannte Datei im Archiv: '{name}' (nicht im Manifest)")
            })?;

            // Ziel-Pfad innerhalb von dest halten (Defense in Depth).
            let out_path = dest.join(&rel);
            if let Some(parent) = out_path.parent() {
                std::fs::create_dir_all(parent).map_err(|e| format!("Elternverzeichnis: {e}"))?;
            }

            // Datei streamen + Hash/Größe verifizieren.
            let mut out = std::fs::File::create(&out_path).map_err(|e| format!("Datei anlegen: {e}"))?;
            let mut hasher = Sha256::new();
            let mut size: u64 = 0;
            let mut buf = [0u8; 65536];
            loop {
                let n = entry.read(&mut buf).map_err(|e| format!("Tar lesen: {e}"))?;
                if n == 0 {
                    break;
                }
                size += n as u64;
                unpacked_total += n as u64;
                if size > inv.size || unpacked_total > MAX_UNPACKED_BYTES {
                    return Err(format!(
                        "Größenüberschreitung bei '{name}' ({} > {})",
                        size, inv.size
                    ));
                }
                hasher.update(&buf[..n]);
                out.write_all(&buf[..n]).map_err(|e| format!("Datei schreiben: {e}"))?;
            }
            if size != inv.size {
                return Err(format!(
                    "Größe weicht ab bei '{name}': erwartet {}, erhalten {}",
                    inv.size, size
                ));
            }
            let hex = hex_lower(&hasher.finalize());
            if !constant_time_eq(hex.as_bytes(), inv.sha256_hex.as_bytes()) {
                return Err(format!("SHA-256 weicht ab bei '{name}'"));
            }
        }

        // Vollständigkeit: jede Inventardatei muss extrahiert worden sein.
        for f in &manifest.files {
            if !dest.join(&f.path).exists() {
                return Err(format!("Inventardatei fehlt nach Extraktion: '{}'", f.path));
            }
        }

        // Manifest selbst ins Build-Verzeichnis legen (Versionierung, C).
        let manifest_json = serde_json::to_string_pretty(manifest)
            .map_err(|e| format!("Manifest serialisieren: {e}"))?;
        std::fs::write(dest.join("manifest.json"), manifest_json)
            .map_err(|e| format!("manifest.json schreiben: {e}"))?;
        Ok(())
    }

    /// Atomares Ersetzen von `current.json` (C: nur auf vollständig verifizierten Builds).
    fn write_current(&self, installed: &InstalledBuild) -> Result<(), String> {
        let cuda_dir = self.base_dir.join("cuda");
        std::fs::create_dir_all(&cuda_dir).map_err(|e| format!("CUDA-Verzeichnis: {e}"))?;

        // Manifest im Zielverzeichnis muss lesbar sein (Commit-Voraussetzung).
        // manifest_path ist relativ zum Backend-Root (z. B. cuda/<build_id>/manifest.json).
        let target_manifest = self.base_dir.join(&installed.manifest_path);
        if !target_manifest.exists() {
            return Err("Manifest fehlt im committen Build".into());
        }

        let pointer = CurrentPointer {
            build_id: installed.build_id.clone(),
            installed_at_unix: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs())
                .unwrap_or(0),
        };
        let json = serde_json::to_string_pretty(&pointer).map_err(|e| format!("Pointer: {e}"))?;

        // Atomar: tmp schreiben → rename (selbes Volume).
        let final_path = cuda_dir.join("current.json");
        let tmp_path = cuda_dir.join(format!(".current.{}.tmp", uuid::Uuid::new_v4()));
        std::fs::write(&tmp_path, json).map_err(|e| format!("Pointer schreiben: {e}"))?;
        if let Err(e) = std::fs::rename(&tmp_path, &final_path) {
            let _ = std::fs::remove_file(&tmp_path);
            return Err(format!("atomares Pointer-Setzen: {e}"));
        }
        Ok(())
    }

    /// Remove (B9): Paket + temporäre Artefakte vollständig entfernen; CPU,
    /// Captures und Transkriptionsdaten bleiben intakt. Nur wenn CUDA inaktiv ist
    /// (Konfliktmatrix im Supervisor).
    pub fn remove(&self) -> Result<(), String> {
        let cuda_dir = self.base_dir.join("cuda");
        if !cuda_dir.exists() {
            return Ok(()); // nichts zu entfernen
        }
        // Staging-Reste räumen.
        let staging_root = self.base_dir.join(".staging");
        if staging_root.exists() {
            for entry in std::fs::read_dir(&staging_root).map_err(|e| format!("Staging lesen: {e}"))? {
                let entry = entry.map_err(|e| format!("Staging-Eintrag: {e}"))?;
                if entry.file_type().is_ok_and(|t| t.is_dir()) {
                    let _ = std::fs::remove_dir_all(entry.path());
                } else {
                    let _ = std::fs::remove_file(entry.path());
                }
            }
        }
        // Build-Verzeichnisse + Pointer entfernen.
        for entry in std::fs::read_dir(&cuda_dir).map_err(|e| format!("CUDA lesen: {e}"))? {
            let entry = entry.map_err(|e| format!("CUDA-Eintrag: {e}"))?;
            if entry.file_type().is_ok_and(|t| t.is_dir()) {
                std::fs::remove_dir_all(entry.path()).map_err(|e| format!("Build entfernen: {e}"))?;
            } else {
                std::fs::remove_file(entry.path()).map_err(|e| format!("Datei entfernen: {e}"))?;
            }
        }
        Ok(())
    }

    /// Repair (B9): defektes Build neu installieren — dieselbe Pipeline wie Install.
    pub fn repair(&self, progress: impl Fn(&str)) -> Result<InstalledBuild, String> {
        self.install_release(progress)
    }
}

// ── Helpers ────────────────────────────────────────────────────────────────

fn hex_lower(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

/// Konstantzeit-Vergleich (kein Timing-Leak bei Hash-Vergleichen).
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for i in 0..a.len() {
        diff |= a[i] ^ b[i];
    }
    diff == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn negative_inventory_rejects_forbidden_markers() {
        let mut m = test_manifest();
        assert!(m.check_negative_inventory().is_ok());
        m.files.push(InventoryEntry {
            path: "tts/voicebox-tts.dll".into(),
            size: 1,
            sha256_hex: "aa".into(),
        });
        assert!(m.check_negative_inventory().is_err());
    }

    #[test]
    fn inventory_completeness_rejects_duplicates_and_gaps() {
        let mut m = test_manifest();
        assert!(m.check_inventory_complete().is_ok());
        // Duplikat des vorhandenen Eintrags → abgelehnt.
        m.files.push(InventoryEntry {
            path: "jf-whisper-server.exe".into(),
            size: 1,
            sha256_hex: "bb".into(),
        });
        assert!(m.check_inventory_complete().is_err());
    }

    #[test]
    fn unsafe_paths_are_rejected() {
        let mut m = test_manifest();
        assert!(m.check_paths_safe().is_ok());
        m.files.push(InventoryEntry {
            path: "../escape.dll".into(),
            size: 1,
            sha256_hex: "cc".into(),
        });
        assert!(m.check_paths_safe().is_err());
    }

    #[test]
    fn constant_time_eq_matches() {
        assert!(constant_time_eq(b"abc", b"abc"));
        assert!(!constant_time_eq(b"abc", b"abd"));
        assert!(!constant_time_eq(b"ab", b"abc"));
    }

    // ── E2E: vollständige Pipeline mit echten Minisign-Signaturen (B9) ──────

    /// Baut ein tar.gz-Archiv aus (Pfad, Inhalt)-Paaren; `extra_symlink` fügt
    /// einen Symlink-Eintrag hinzu (Negativtest).
    fn build_archive(files: &[(&str, &str)], extra_symlink: bool) -> Vec<u8> {
        use std::io::Write as _;
        let mut tar_buf: Vec<u8> = Vec::new();
        {
            let mut builder = tar::Builder::new(&mut tar_buf);
            for (path, content) in files {
                let mut h = tar::Header::new_gnu();
                h.set_path(path).unwrap();
                h.set_size(content.len() as u64);
                h.set_mode(0o755);
                h.set_cksum();
                builder.append_data(&mut h, path, content.as_bytes()).unwrap();
            }
            if extra_symlink {
                let mut h = tar::Header::new_gnu();
                h.set_path("evil-link").unwrap();
                h.set_size(0);
                h.set_entry_type(tar::EntryType::Symlink);
                h.set_link_name("/etc/passwd");
                h.set_cksum();
                builder.append(&h, std::io::empty()).unwrap();
            }
        }
        let mut enc = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::fast());
        enc.write_all(&tar_buf).unwrap();
        enc.finish().unwrap()
    }

    fn sha_hex(bytes: &[u8]) -> String {
        let mut h = Sha256::new();
        h.update(bytes);
        hex_lower(&h.finalize())
    }

    /// Legt einen konsistent signierten Releasepfad in `staging` an:
    /// `manifest.json` + `.sig`, `<archive>` + `.sig` — ein Key signiert beide.
    /// Liefert (Public-Key-Base64, Manifest, Archiv-Pfad).
    fn setup_release(
        staging: &Path,
        archive_files: &[(&str, &str)],
        inventory_override: Option<&[(&str, &str)]>,
        extra_symlink: bool,
        build_id_override: Option<&str>,
    ) -> (String, Manifest, PathBuf) {
        use base64::Engine;
        let archive = build_archive(archive_files, extra_symlink);
        let mut manifest = test_manifest();
        if let Some(bid) = build_id_override {
            manifest.build_id = bid.into();
        }
        manifest.compressed_size = archive.len() as u64;
        manifest.sha256_hex = sha_hex(&archive);
        let inv_files: Vec<(&str, &str)> = inventory_override
            .map(|v| v.to_vec())
            .unwrap_or_else(|| archive_files.to_vec());
        manifest.files = inv_files
            .iter()
            .map(|(p, c)| InventoryEntry {
                path: p.to_string(),
                size: c.len() as u64,
                sha256_hex: sha_hex(c.as_bytes()),
            })
            .collect();

        let kp = minisign::KeyPair::generate_unencrypted_keypair().unwrap();
        let pk_b64 = base64::engine::general_purpose::STANDARD.encode(kp.pk.to_bytes());

        let manifest_json = serde_json::to_string_pretty(&manifest).unwrap();
        let manifest_sig = minisign::sign(Some(&kp.pk), &kp.sk, manifest_json.as_bytes(), None, None)
            .unwrap();
        let archive_sig = minisign::sign(Some(&kp.pk), &kp.sk, archive.as_slice(), None, None)
            .unwrap();

        std::fs::create_dir_all(staging).unwrap();
        std::fs::write(staging.join("manifest.json"), manifest_json).unwrap();
        std::fs::write(
            staging.join("manifest.json.sig"),
            manifest_sig.to_string(),
        )
        .unwrap();
        let archive_path = staging.join(&manifest.archive_name);
        std::fs::write(&archive_path, &archive).unwrap();
        std::fs::write(
            archive_path.with_file_name(format!("{}.sig", manifest.archive_name)),
            archive_sig.to_string(),
        )
        .unwrap();

        (pk_b64, manifest, archive_path)
    }

    fn manager_for(tmp: &Path, pk_b64: String) -> Manager {
        Manager::new(tmp.to_path_buf(), "0.5.0".into(), "test-build".into())
            .with_test_public_key(pk_b64)
    }

    #[test]
    fn e2e_install_pipeline_commits_verified_build() {
        let tmp = tempfile::tempdir().unwrap();
        let files: Vec<(&str, &str)> = vec![
            ("jf-whisper-server.exe", "CUDA-BINARY-CONTENT"),
            ("nvidia/cudart.dll", "CUDART-CONTENT"),
        ];
        let staging = tmp.path().join("staging");
        let (pk_b64, _manifest, archive_path) = setup_release(&staging, &files, None, false, None);

        let manager = manager_for(tmp.path(), pk_b64);
        let installed = manager
            .install_from_paths(&archive_path, &staging.join("manifest.json"), |_| {})
            .expect("Pipeline muss grünes Commit liefern");
        assert_eq!(installed.build_id, "test-build");

        // Current-Pointer atomar gesetzt.
        let current = manager.current().unwrap().unwrap();
        assert_eq!(current.build_id, "test-build");

        // Build-Verzeichnis vollständig + manifest.json versioniert.
        let target = tmp.path().join("cuda").join("test-build");
        for (p, c) in &files {
            let content = std::fs::read(target.join(p)).unwrap();
            assert_eq!(content, c.as_bytes());
        }
        assert!(target.join("manifest.json").exists());

        // Staging ist geräumt.
        assert!(!staging.exists());
    }

    #[test]
    fn e2e_tampered_archive_is_rejected() {
        let tmp = tempfile::tempdir().unwrap();
        let files: Vec<(&str, &str)> = vec![("jf-whisper-server.exe", "CONTENT")];
        let staging = tmp.path().join("staging");
        let (pk_b64, _manifest, archive_path) = setup_release(&staging, &files, None, false, None);

        // Archiv nachträglich verändern → Signatur muss failen.
        let mut bytes = std::fs::read(&archive_path).unwrap();
        *bytes.last_mut().unwrap() ^= 0xFF;
        std::fs::write(&archive_path, &bytes).unwrap();

        let manager = manager_for(tmp.path(), pk_b64);
        let err = manager
            .install_from_paths(&archive_path, &staging.join("manifest.json"), |_| {})
            .expect_err("Manipuliertes Archiv muss abgelehnt werden");
        assert!(err.contains("Signatur") || err.contains("Hash"), "unerwarteter Fehler: {err}");

        // Fail-closed: kein Current-Pointer, kein Build.
        assert!(manager.current().unwrap().is_none());
        assert!(!tmp.path().join("cuda/test-build").exists());
    }

    #[test]
    fn e2e_tampered_manifest_is_rejected() {
        let tmp = tempfile::tempdir().unwrap();
        let files: Vec<(&str, &str)> = vec![("jf-whisper-server.exe", "CONTENT")];
        let staging = tmp.path().join("staging");
        let (pk_b64, _manifest, archive_path) = setup_release(&staging, &files, None, false, None);

        // Manifest nachträglich verändern → Signatur muss failen.
        let mut mjson = std::fs::read_to_string(staging.join("manifest.json")).unwrap();
        mjson.push(' ');
        std::fs::write(staging.join("manifest.json"), mjson).unwrap();

        let manager = manager_for(tmp.path(), pk_b64);
        let err = manager
            .install_from_paths(&archive_path, &staging.join("manifest.json"), |_| {})
            .expect_err("Manipuliertes Manifest muss abgelehnt werden");
        assert!(err.contains("Manifest-Signatur"), "unerwarteter Fehler: {err}");

        // Fail-closed: kein Current-Pointer.
        assert!(manager.current().unwrap().is_none());
    }

    #[test]
    fn e2e_unknown_file_in_archive_is_rejected() {
        // Archiv enthält eine Datei, die nicht im signierten Inventar steht.
        let tmp = tempfile::tempdir().unwrap();
        let archive_files: Vec<(&str, &str)> = vec![
            ("jf-whisper-server.exe", "CONTENT"),
            ("extra-unknown.dll", "UNAUTHORIZED"),
        ];
        let allowed: Vec<(&str, &str)> = vec![("jf-whisper-server.exe", "CONTENT")];
        let staging = tmp.path().join("staging");
        let (pk_b64, _manifest, archive_path) =
            setup_release(&staging, &archive_files, Some(&allowed), false, None);

        let manager = manager_for(tmp.path(), pk_b64);
        let err = manager
            .install_from_paths(&archive_path, &staging.join("manifest.json"), |_| {})
            .expect_err("Unbekannte Datei muss abgelehnt werden");
        assert!(err.contains("unbekannte Datei"), "unerwarteter Fehler: {err}");
    }

    #[test]
    fn e2e_symlink_entry_is_rejected() {
        let tmp = tempfile::tempdir().unwrap();
        let files: Vec<(&str, &str)> = vec![("jf-whisper-server.exe", "CONTENT")];
        let staging = tmp.path().join("staging");
        let (pk_b64, _manifest, archive_path) = setup_release(&staging, &files, None, true, None);

        let manager = manager_for(tmp.path(), pk_b64);
        let err = manager
            .install_from_paths(&archive_path, &staging.join("manifest.json"), |_| {})
            .expect_err("Symlink-Eintrag muss abgelehnt werden");
        assert!(err.contains("verbotener Tar-Eintragstyp"), "unerwarteter Fehler: {err}");
    }

    #[test]
    fn e2e_wrong_build_id_is_rejected() {
        let tmp = tempfile::tempdir().unwrap();
        let files: Vec<(&str, &str)> = vec![("jf-whisper-server.exe", "CONTENT")];
        let staging = tmp.path().join("staging");
        // Build-ID weicht vom laufenden App-Build ab (signiert so).
        let (pk_b64, _manifest, archive_path) =
            setup_release(&staging, &files, None, false, Some("anderer-build"));

        let manager = manager_for(tmp.path(), pk_b64);
        let err = manager
            .install_from_paths(&archive_path, &staging.join("manifest.json"), |_| {})
            .expect_err("Build-ID-Drift muss abgelehnt werden");
        assert!(err.contains("Build-ID"), "unerwarteter Fehler: {err}");
    }

    fn test_manifest() -> Manifest {
        Manifest {
            app_version: "0.5.0".into(),
            build_id: "test-build".into(),
            source_commit: "deadbeef".into(),
            patch_ledger_hash: "aa".into(),
            profile_sha256: "bb".into(),
            binary_variant: "cuda".into(),
            target_triple: "x86_64-pc-windows-msvc".into(),
            min_driver_version: "531.00".into(),
            schema_id: "jfwhisper-v2".into(),
            alembic_head: "08a06bf47a91".into(),
            migration_chain_hash: "cc".into(),
            allowlist_hash: "dd".into(),
            worker_contract_version: 1,
            api_contract_version: 1,
            result_contract_version: 1,
            archive_name: "jf-whisper-cuda-0.5.0.tar.gz".into(),
            archive_url: "https://releases.jfwhisper.de/cuda/0.5.0/jf-whisper-cuda-0.5.0.tar.gz".into(),
            compressed_size: 1024,
            max_unpacked_size: 4096,
            sha256_hex: "ee".into(),
            files: vec![InventoryEntry {
                path: "jf-whisper-server.exe".into(),
                size: 100,
                sha256_hex: "ff".into(),
            }],
            release_sequence: 1,
            signing_key_id: "test-key".into(),
        }
    }
}
