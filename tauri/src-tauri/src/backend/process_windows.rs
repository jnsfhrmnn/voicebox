//! JFW-12 B8: Windows-Prozessbaum-Kontrolle ueber Job Objects.
//!
//! Der Sidecar wird per `CreateProcessW` mit `CREATE_SUSPENDED` erzeugt und
//! **vor** dem Resume einem eigenen Job Object zugeordnet, das
//! `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` traegt. Damit ist der komplette
//! Prozessbaum (Sidecar + alle Kinder) an den Job-Handle gebunden: schließt
//! die App oder ruft sie `stop_server`, wird der Baum atomar beendet — es
//! gibt keinen Zustand, in dem ein Sidecar-Prozess ohne Job existiert.
//!
//! stdout/stderr werden auf zwei dedizierten OS-Threads blockierend gelesen und
//! als Zeilen über einen tokio-Kanal übergeben; die Handshake-Erkennung im
//! bestehenden Loop bleibt unverändert.

use std::ffi::{c_void, OsStr};
use std::os::windows::ffi::OsStrExt;
use std::path::Path;

use sha2::{Digest, Sha256};
use tokio::sync::mpsc;
use windows::core::{HSTRING, PWSTR};
use windows::Win32::Foundation::{CloseHandle, HANDLE};
use windows::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JobObjectExtendedLimitInformation, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, SetInformationJobObject,
};
use windows::Win32::System::Pipes::CreatePipe;
use windows::Win32::System::Threading::{
    CreateProcessW, CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT, PROCESS_INFORMATION, STARTUPINFOW,
    STARTF_USESTDHANDLES, ResumeThread,
};
use windows::Win32::Storage::FileSystem::ReadFile;

/// Vollständige Prozessidentität des Sidecar (B8: "vollständige
/// Prozessidentität" — PID + Exe-Pfad + Executable-Hash).
#[derive(Debug, Clone)]
pub struct ProcessIdentity {
    pub pid: u32,
    /// JFW-12 Block (d)/(e): Artefakt-Nachweis und Switch-Journal lesen diese
    /// Felder; bis dahin Teil der API-Fläche.
    #[allow(dead_code)] // JFW-12 Block (d)/(e)
    pub exe_path: String,
    /// SHA-256 des ausführbaren Binaries (hex) — Nachweis, welche Binary läuft.
    #[allow(dead_code)] // JFW-12 Block (d)/(e)
    pub exe_sha256: String,
}

/// Ein Job Object mit seiner vollständigen Prozessidentität. Der Drop des
/// `SidecarJob` schließt das Job → KILL_ON_JOB_CLOSE beendet den kompletten
/// Sidecar-Prozessbaum.
///
/// Der Handle wird als roher Wert (`isize`) gehalten: `HANDLE` ist kein
/// Send/Sync-Typ, der Tauri-State verlangt aber beides für `Mutex<Option<_>>`.
pub struct SidecarJob {
    job_handle_raw: isize,
    pub identity: ProcessIdentity,
}

impl Drop for SidecarJob {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(HANDLE(self.job_handle_raw as *mut c_void));
        }
    }
}

/// Zeilen vom Sidecar (stdout/stderr), zeichenkodiert.
#[derive(Debug)]
pub enum SidecarLine {
    Stdout(String),
    Stderr(String),
}

fn wide(s: &str) -> Vec<u16> {
    OsStr::new(s).encode_wide().collect()
}

/// SHA-256 einer Datei (hex). Bei Lese-Fehlern wird der leere String geliefert —
/// die Identität bleibt bestehen, der Hash ist dann nur ein Best-Effort-Nachweis.
fn sha256_file(path: &Path) -> String {
    use std::io::Read;
    let mut f = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(_) => return String::new(),
    };
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 65536];
    loop {
        match f.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => hasher.update(&buf[..n]),
            Err(_) => return String::new(),
        }
    }
    let digest = hasher.finalize();
    let mut out = String::with_capacity(64);
    for b in digest.iter() {
        out.push_str(&format!("{:02x}", b));
    }
    out
}

/// Erzeugt eine anonyme Pipe mit **erbbaren** Handles (STARTF_USESTDHANDLES
/// verlangt, dass das Kind die std-Handles erben kann) und liefert
/// (lesendes Ende, schreibendes Ende).
fn make_pipe() -> Result<(HANDLE, HANDLE), String> {
    use windows::Win32::Security::SECURITY_ATTRIBUTES;
    let mut sa = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: std::ptr::null_mut(),
        bInheritHandle: true.into(),
    };
    let mut read_end = HANDLE::default();
    let mut write_end = HANDLE::default();
    unsafe {
        CreatePipe(&mut read_end, &mut write_end, Some(&sa), 0)
            .map_err(|e| format!("CreatePipe: {e}"))?;
    }
    Ok((read_end, write_end))
}

/// Baut einen Umgebungsblock (null-terminierte `KEY=VALUE`-Zeilen, doppeltes NUL
/// am Ende) aus der aktuellen Umgebung plus Extra-Variablen. CreateProcessW
/// erwartet hier ALLE Variablen, die das Kind braucht — daher Kopie der Eltern-Umgebung.
fn build_env_block(extra: &[(String, String)]) -> Vec<u16> {
    let mut entries: Vec<(String, String)> = std::env::vars().collect();
    for (k, v) in extra {
        match entries.iter_mut().find(|(ek, _)| ek.eq_ignore_ascii_case(k)) {
            Some(e) => e.1 = v.clone(),
            None => entries.push((k.clone(), v.clone())),
        }
    }
    let mut block: Vec<u16> = Vec::new();
    for (k, v) in &entries {
        block.extend(OsStr::new(k.as_str()).encode_wide());
        block.push('=' as u16);
        block.extend(OsStr::new(v.as_str()).encode_wide());
        block.push(0); // jede Zeile null-terminiert
    }
    block.push(0); // abschließendes doppeltes NUL
    block
}

/// Spawnt den Sidecar mit Job-Object-Bindung (B8) und liefert
/// `(SidecarJob, Zeilen-Kanal)`. Der Prozess wird suspendiert erzeugt, dem Job
/// zugeordnet und erst dann resumed.
pub fn spawn_sidecar(
    exe_path: &Path,
    args: &[String],
    extra_env: &[(String, String)],
    cwd: Option<&Path>,
) -> Result<(SidecarJob, mpsc::Receiver<SidecarLine>), String> {
    // 1) Job Object mit KILL_ON_JOB_CLOSE anlegen.
    let job_name = HSTRING::from("JFWhisperSidecarJob");
    let job_handle = unsafe { CreateJobObjectW(None, &job_name).map_err(|e| format!("CreateJobObjectW: {e}"))? };

    let mut extended = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    extended.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    unsafe {
        SetInformationJobObject(
            job_handle,
            JobObjectExtendedLimitInformation,
            &extended as *const _ as *const c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
        .map_err(|e| format!("SetInformationJobObject: {e}"))?;
    }

    // 2) Pipes für stdout/stderr.
    let (out_read, out_write) = make_pipe()?;
    let (err_read, err_write) = make_pipe()?;

    // 3) Umgebungsblock (Eltern-Umgebung + Extra-Variablen).
    let env_block = build_env_block(extra_env);

    // 4) Kommandozeile: "exe" arg1 arg2 ...
    let mut cmd_line = String::with_capacity(512);
    cmd_line.push_str(&format!("\"{}\"", exe_path.to_string_lossy()));
    for a in args {
        cmd_line.push(' ');
        if a.contains(' ') {
            cmd_line.push('"');
            cmd_line.push_str(a);
            cmd_line.push('"');
        } else {
            cmd_line.push_str(a);
        }
    }

    // cwd: explizit übergeben, sonst Verzeichnis der Exe (PyInstaller braucht DLLs relativ dazu).
    let cwd_str = match cwd {
        Some(c) => c.to_string_lossy().to_string(),
        None => exe_path
            .parent()
            .map(|p| p.to_string_lossy().to_string())
            .unwrap_or_default(),
    };

    let exe_h = HSTRING::from(exe_path.to_string_lossy().as_ref());
    let cwd_h = HSTRING::from(cwd_str.as_str());
    let mut cmd_wide: Vec<u16> = wide(&cmd_line);
    cmd_wide.push(0); // PWSTR erwartet null-terminiert.

    let mut si = STARTUPINFOW::default();
    si.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = HANDLE::default(); // stdin: null
    si.hStdOutput = out_write;
    si.hStdError = err_write;

    let mut pi = PROCESS_INFORMATION::default();
    unsafe {
        CreateProcessW(
            &exe_h,
            Some(PWSTR(cmd_wide.as_mut_ptr())),
            None,
            None,
            true, // bInheritHandles: STARTF_USESTDHANDLES verlangt erbbare std-Handles.
            CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT,
            Some(env_block.as_ptr() as *const c_void),
            &cwd_h,
            &si,
            &mut pi,
        )
        .map_err(|e| format!("CreateProcessW: {e}"))?;
    }

    // Schreibenden Pipe-Enden schließen (wir lesen nur).
    unsafe {
        let _ = CloseHandle(out_write);
        let _ = CloseHandle(err_write);
    }

    // 5) Prozess dem Job zuordnen — VOR dem Resume.
    unsafe {
        AssignProcessToJobObject(job_handle, pi.hProcess)
            .map_err(|e| format!("AssignProcessToJobObject: {e}"))?;
    }

    // 6) Jetzt erst resume.
    unsafe {
        let _ = ResumeThread(pi.hThread);
    }

    // Prozess-/Thread-Handle schließen (der Job hält den Baum).
    unsafe {
        let _ = CloseHandle(pi.hProcess);
        let _ = CloseHandle(pi.hThread);
    }

    // 7) Vollständige Prozessidentität.
    let identity = ProcessIdentity {
        pid: pi.dwProcessId,
        exe_path: exe_path.to_string_lossy().to_string(),
        exe_sha256: sha256_file(exe_path),
    };

    // 8) Pipe-Lesethreads (blockierendes ReadFile je Pipe). Handles werden als
    //    rohe Werte übergeben, weil HANDLE kein Send-Typ ist.
    let out_raw = out_read.0 as isize;
    let err_raw = err_read.0 as isize;
    let (tx, rx) = mpsc::channel::<SidecarLine>(256);
    let tx_err = tx.clone();
    std::thread::spawn(move || read_pipe_lines(out_raw, tx, true));
    std::thread::spawn(move || read_pipe_lines(err_raw, tx_err, false));

    Ok((SidecarJob { job_handle_raw: job_handle.0 as isize, identity }, rx))
}

/// Liest eine Pipe blockierend zeilenweise und sendet sie über den Kanal.
/// Läuft auf einem dedizierten OS-Thread; `ReadFile` wartet selbst auf Daten/EOF.
fn read_pipe_lines(handle_raw: isize, tx: mpsc::Sender<SidecarLine>, is_stdout: bool) {
    let handle = HANDLE(handle_raw as *mut c_void);
    let mut buf = [0u8; 4096];
    loop {
        let mut n = 0u32;
        unsafe {
            if ReadFile(handle, Some(&mut buf), Some(&mut n), None).is_err() {
                break; // Pipe geschlossen / Fehler.
            }
        }
        if n == 0 {
            break; // EOF.
        }
        let chunk = String::from_utf8_lossy(&buf[..n as usize]).to_string();
        for line in chunk.split_inclusive('\n') {
            let t = line.trim_end_matches(['\r', '\n']);
            if !t.is_empty() {
                let _ = tx.blocking_send(if is_stdout {
                    SidecarLine::Stdout(t.to_string())
                } else {
                    SidecarLine::Stderr(t.to_string())
                });
            }
        }
    }
    unsafe {
        let _ = CloseHandle(handle);
    }
}
