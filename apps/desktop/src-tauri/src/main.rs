#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// Desktop host. Three entry behaviours, chosen by environment variable so that the
// installed product and the developer tools stay separate:
//
//   (default)                      product: start the local API and the first-version
//                                  Workers, serve the built frontend from that same
//                                  API process, then open the workbench.
//   INTERVIEW_DESKTOP_PREVIEW=1    development: load the same workbench from Vite.
//   INTERVIEW_DESKTOP_PROBE=1      D-00 probe: embedded desktop.html plus the synthetic
//                                  task, kept for the historical checks.
//
// Paths and configuration always come from the launcher, never from web content.
use std::{
    fs,
    net::TcpListener,
    os::windows::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, Command},
    sync::{Arc, Mutex},
    thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use reqwest::blocking::Client;
use reqwest::cookie::{CookieStore, Jar};
use serde_json::{json, Value};
use tauri::Manager;


const PREVIEW_URL: &str = "http://127.0.0.1:5174/?shell=desktop";
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

// F07Z-83: the local identity's password used to sit in the webview's localStorage, which is a
// plain file on this machine and readable by anything that can read the profile. It goes to the
// Windows Credential Manager instead, one slot per identity. The id comes from web content, so it
// is validated to be a bare uuid and always prefixed: a page can reach this app's own slots and
// nothing else, so this cannot become a secret reader for other applications' credentials.
#[cfg(windows)]
mod local_secret {
    use std::ptr;
    use windows_sys::Win32::Foundation::FILETIME;
    use windows_sys::Win32::Security::Credentials::{
        CredDeleteW, CredFree, CredReadW, CredWriteW, CREDENTIALW, CRED_PERSIST_LOCAL_MACHINE,
        CRED_TYPE_GENERIC,
    };

    const PREFIX: &str = "interview-desktop/local-identity/";
    const USER_NAME: &str = "interview-desktop-workbench";
    const ERROR_NOT_FOUND: i32 = 1168;

    /// The identity id is a uuid and nothing else: web content cannot name another
    /// application's credential, only one of this app's own slots.
    fn target(id: &str) -> Result<Vec<u16>, String> {
        let ok = id.len() == 36
            && id
                .chars()
                .all(|c| c.is_ascii_hexdigit() || c == '-');
        if !ok {
            return Err("local_secret_bad_identity".into());
        }
        Ok(wide(&format!("{PREFIX}{id}")))
    }

    fn wide(value: &str) -> Vec<u16> {
        value.encode_utf16().chain(std::iter::once(0)).collect()
    }

    fn last_error() -> i32 {
        std::io::Error::last_os_error().raw_os_error().unwrap_or(0)
    }

    pub fn write(id: &str, payload: &str) -> Result<(), String> {
        let mut target = target(id)?;
        let mut user = wide(USER_NAME);
        let mut blob = payload.as_bytes().to_vec();
        let credential = CREDENTIALW {
            Flags: 0,
            Type: CRED_TYPE_GENERIC,
            TargetName: target.as_mut_ptr(),
            Comment: ptr::null_mut(),
            LastWritten: FILETIME {
                dwLowDateTime: 0,
                dwHighDateTime: 0,
            },
            CredentialBlobSize: blob.len() as u32,
            CredentialBlob: blob.as_mut_ptr(),
            Persist: CRED_PERSIST_LOCAL_MACHINE,
            AttributeCount: 0,
            Attributes: ptr::null_mut(),
            TargetAlias: ptr::null_mut(),
            UserName: user.as_mut_ptr(),
        };
        let ok = unsafe { CredWriteW(&credential, 0) };
        if ok == 0 {
            return Err(format!("local_secret_write_failed:{}", last_error()));
        }
        Ok(())
    }

    pub fn read(id: &str) -> Result<Option<String>, String> {
        let target = target(id)?;
        let mut out: *mut CREDENTIALW = ptr::null_mut();
        let ok = unsafe { CredReadW(target.as_ptr(), CRED_TYPE_GENERIC, 0, &mut out) };
        if ok == 0 {
            let code = last_error();
            if code == ERROR_NOT_FOUND {
                return Ok(None);
            }
            return Err(format!("local_secret_read_failed:{code}"));
        }
        let value = unsafe {
            let credential = &*out;
            let bytes = std::slice::from_raw_parts(
                credential.CredentialBlob,
                credential.CredentialBlobSize as usize,
            );
            let text = String::from_utf8_lossy(bytes).to_string();
            CredFree(out as *const _);
            text
        };
        Ok(Some(value))
    }

    pub fn delete(id: &str) -> Result<(), String> {
        let target = target(id)?;
        let ok = unsafe { CredDeleteW(target.as_ptr(), CRED_TYPE_GENERIC, 0) };
        if ok == 0 {
            let code = last_error();
            if code == ERROR_NOT_FOUND {
                return Ok(());
            }
            return Err(format!("local_secret_delete_failed:{code}"));
        }
        Ok(())
    }
}

#[cfg(not(windows))]
mod local_secret {
    pub fn write(_id: &str, _payload: &str) -> Result<(), String> {
        Err("local_secret_unsupported_platform".into())
    }
    pub fn read(_id: &str) -> Result<Option<String>, String> {
        Err("local_secret_unsupported_platform".into())
    }
    pub fn delete(_id: &str) -> Result<(), String> {
        Err("local_secret_unsupported_platform".into())
    }
}

type Shared = Arc<Mutex<Host>>;

// Children live in a Windows job object with KILL_ON_JOB_CLOSE. The handle is
// intentionally never closed: when this process dies for ANY reason (clean exit,
// crash, or an external kill), the OS terminates the API and the Workers it started.
// Nothing else is in the job, so processes owned by other programs are untouched.
#[cfg(windows)]
mod child_job {
    use std::os::windows::io::AsRawHandle;
    use std::process::Child;
    use windows_sys::Win32::Foundation::HANDLE;
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        SetInformationJobObject,
    };

    pub fn assign(child: &Child) -> bool {
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                return false;
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const _,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            ) == 0
            {
                return false;
            }
            AssignProcessToJobObject(job, child.as_raw_handle() as HANDLE) != 0
        }
    }
}

#[cfg(not(windows))]
mod child_job {
    use std::process::Child;
    pub fn assign(_child: &Child) -> bool { false }
}

struct Host {
    resources: PathBuf,
    app_dir: PathBuf,
    config: PathBuf,
    out: PathBuf,
    api: Option<Child>,
    workers: Vec<Child>,
    autorun: bool,
}

impl Host {
    fn exe(&self) -> PathBuf {
        self.resources.join("interview-sidecar.exe")
    }
    fn web(&self) -> PathBuf {
        self.resources.join("web")
    }

    /// Stop only the processes this window started. A server owned by another program
    /// is never adopted, so it is never stopped either.
    fn stop(&mut self) -> Result<(), String> {
        for mut child in self.workers.drain(..) {
            if child.try_wait().map_err(|e| e.to_string())?.is_none() {
                let _ = child.kill();
            }
            let _ = child.wait();
        }
        if let Some(mut child) = self.api.take() {
            if child.try_wait().map_err(|e| e.to_string())?.is_none() {
                child.kill().map_err(|e| e.to_string())?;
            }
            child.wait().map_err(|e| e.to_string())?;
        }
        Ok(())
    }

    /// The explicit live authorization, if the user enabled live model calls.
    fn live(&self) -> Option<(String, u64)> {
        let raw = fs::read(self.config.join("model.json")).ok()?;
        let value: Value = serde_json::from_slice(&raw).ok()?;
        live_authorization(&value)
    }

    /// F07Z-112: the separate materials switch. It never rides on the interview flag.
    fn material_live(&self) -> Option<(String, u64)> {
        let raw = fs::read(self.config.join("model.json")).ok()?;
        let value: Value = serde_json::from_slice(&raw).ok()?;
        material_live_authorization(&value)
    }

    /// S03c: the planning Agent is the third paid capability, with its own switch and cap.
    fn prep_live(&self) -> Option<(String, u64)> {
        let raw = fs::read(self.config.join("model.json")).ok()?;
        let value: Value = serde_json::from_slice(&raw).ok()?;
        prep_live_authorization(&value)
    }

    fn check(&self) -> Result<Value, String> {
        if !self.exe().is_file() {
            return Err("python_resource_missing".into());
        }
        if !self.web().join("index.html").is_file() {
            return Err("web_resource_missing".into());
        }
        // The API reads two fixture trees at runtime (practice catalog and builtin banks).
        // Checking them here turns a later 500 into a startup message that says what is
        // missing from the folder.
        let fixtures = self.resources.join("_internal").join("fixtures");
        if !fixtures.join("question-banks").is_dir() || !fixtures.join("pm0-05").is_dir() {
            return Err("resources_incomplete".into());
        }
        let api_config = self.config.join("api.json");
        let worker_config = self.config.join("worker.json");
        if !api_config.is_file() || !worker_config.is_file() {
            return Err("configuration_missing".into());
        }
        // The product only ever runs the test database with the free scripted provider.
        let config: Value = serde_json::from_slice(&fs::read(&api_config).map_err(|_| "configuration_invalid")?)
            .map_err(|_| "configuration_invalid")?;
        let worker: Value = serde_json::from_slice(&fs::read(&worker_config).map_err(|_| "configuration_invalid")?)
            .map_err(|_| "configuration_invalid")?;
        if config["mode"] != "test"
            || config["database"]["dbname"] != "py_interview_test"
            || worker["mode"] != "test"
        {
            return Err("configuration_invalid".into());
        }
        // F07Z-62: live is allowed only when the user explicitly enabled it and a capped
        // authorization exists; otherwise the shipped scripted default is required.
        if self.live().is_none()
            && config.get("assessment_mode").and_then(Value::as_str).unwrap_or("scripted") != "scripted"
        {
            return Err("configuration_invalid".into());
        }
        // Never attach to a server another program owns: probing the port first means a
        // foreign process is reported instead of silently served.
        let port = TcpListener::bind("127.0.0.1:3100").map_err(|_| "port_3100_in_use".to_string())?;
        drop(port);
        let mut check_command = Command::new(self.exe());
        check_command
            .args(["check-dependencies", "--api-config"])
            .arg(&api_config)
            .arg("--worker-config")
            .arg(&worker_config);
        if self.live().is_some() || self.material_live().is_some() {
            check_command.arg("--allow-live");
        }
        let output = check_command
            .current_dir(&self.out)
            .creation_flags(CREATE_NO_WINDOW)
            .output()
            .map_err(|_| "python_start_failed")?;
        if !output.status.success() {
            return Err("python_start_failed".into());
        }
        let checked: Value = serde_json::from_slice(&output.stdout).map_err(|_| "python_start_failed")?;
        if checked["ready"] != true {
            return Err(checked["code"].as_str().unwrap_or("dependency_check_failed").into());
        }
        Ok(checked)
    }

    fn log(&self, name: &str) -> Result<fs::File, String> {
        fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.out.join(name))
            .map_err(|e| e.to_string())
    }

    /// Start the API and the Workers the shipped features need, then wait until the API
    /// answers. Any failure stops whatever was already started, so a retry starts clean.
    fn start(&mut self) -> Result<(), String> {
        self.check()?;
        let result = self.start_inner();
        if result.is_err() {
            let _ = self.stop();
        }
        result
    }

    fn start_inner(&mut self) -> Result<(), String> {
        let api_config = self.config.join("api.json");
        let worker_config = self.config.join("worker.json");
        let api_log = self.log("api.log")?;
        let live = self.live();
        let material = self.material_live();
        let mut serve = Command::new(self.exe());
        serve
            .arg("serve")
            .arg("--config")
            .arg(&api_config)
            .arg("--static-dir")
            .arg(self.web())
            .arg("--model-config")
            .arg(self.config.join("model.json"));
        if live.is_some() {
            serve.args(["--assessment-mode", "live"]);
        }
        if material.is_some() {
            // F07Z-112: report the materials worker's mode to the settings pane. It does not
            // change interview behaviour, so it is a separate flag.
            serve.arg("--material-live");
        }
        let child = serve
            .current_dir(&self.out)
            .creation_flags(CREATE_NO_WINDOW)
            .stdout(api_log.try_clone().map_err(|e| e.to_string())?)
            .stderr(api_log)
            .spawn()
            .map_err(|e| e.to_string())?;
        // Belongs to our job: if this window ever dies without running its cleanup, the
        // OS still terminates this child.
        let _in_job = child_job::assign(&child);
        fs::write(self.out.join("sidecar-pid.txt"), child.id().to_string()).map_err(|e| e.to_string())?;
        self.api = Some(child);

        let client = Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(1))
            .build()
            .map_err(|e| e.to_string())?;
        for _ in 0..240 {
            if self.api.as_mut().unwrap().try_wait().map_err(|e| e.to_string())?.is_some() {
                return Err("sidecar_exited_before_ready".into());
            }
            if client
                .get("http://127.0.0.1:3100/health")
                .send()
                .is_ok_and(|r| r.status().is_success())
            {
                return self.start_workers(&worker_config, live, material);
            }
            thread::sleep(Duration::from_millis(250));
        }
        Err("sidecar_ready_timeout".into())
    }

    /// The interview, materials and career Workers are the three the shipped features
    /// need. The career coordinator is not optional: without it a new goal stays
    /// PENDING for ever, so the top-bar one-sentence input and the one-click
    /// preparation pipeline would never run in the shipped product.
    ///
    /// F07Z-112: interview and materials follow their own switches; career stays scripted.
    /// Each live worker gets the authorization id the API created from that capability's cap.
    fn start_workers(&mut self, config: &Path, live: Option<(String, u64)>,
                     material: Option<(String, u64)>) -> Result<(), String> {
        for (kind, log_name) in [
            ("interview", "worker-interview.log"),
            ("interview-prep", "worker-interview-prep.log"),
            ("materials", "worker-materials.log"),
            ("career", "worker-career.log"),
        ] {
            let log = self.log(log_name)?;
            let capability = match kind {
                "interview" => live.clone(),
                "materials" => material.clone(),
                "interview-prep" => self.prep_live(),
                _ => None,
            };
            let provider = if capability.is_some() { "live" } else { "scripted" };
            let mut command = Command::new(self.exe());
            command
                .args(["worker", "--config"])
                .arg(config)
                .args(["--kind", kind, "--provider", provider]);
            if let Some((authorization, _cap)) = &capability {
                command
                    .args(["--authorization", authorization, "--credentials", "vault", "--model-config"])
                    .arg(self.config.join("model.json"));
            }
            let child = command
                .current_dir(&self.out)
                .creation_flags(CREATE_NO_WINDOW)
                .stdout(log.try_clone().map_err(|e| e.to_string())?)
                .stderr(log)
                .spawn()
                .map_err(|e| e.to_string())?;
            let _in_job = child_job::assign(&child);
            self.workers.push(child);
        }
        // A Worker that exits immediately is a configuration problem, not a slow start.
        thread::sleep(Duration::from_millis(900));
        for child in self.workers.iter_mut() {
            if child.try_wait().map_err(|e| e.to_string())?.is_some() {
                return Err("worker_exited".into());
            }
        }
        Ok(())
    }

    // ---------------------------------------------------------------- D-00 probe only

    fn probe(&mut self) -> Result<Value, String> {
        if self.api.as_mut().ok_or("desktop_not_ready")?.try_wait().map_err(|e| e.to_string())?.is_some() {
            return Err("desktop_not_ready".into());
        }
        let jar = Arc::new(Jar::default());
        let base = "http://127.0.0.1:3100".parse().unwrap();
        let client = Client::builder()
            .no_proxy()
            .cookie_provider(jar.clone())
            .timeout(Duration::from_secs(20))
            .build()
            .map_err(|e| e.to_string())?;
        let saved = self.out.join("synthetic-session.json");
        if saved.exists() {
            let prior: Value =
                serde_json::from_slice(&fs::read(&saved).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
            jar.add_cookie_str(prior["cookie"].as_str().ok_or("missing_synthetic_cookie")?, &base);
            let id = prior["goal_id"].as_str().ok_or("missing_saved_goal")?;
            let restored: Value = client
                .get(format!("http://127.0.0.1:3100/api/career-goals/{id}"))
                .send()
                .and_then(|r| r.error_for_status())
                .and_then(|r| r.json())
                .map_err(|e| e.to_string())?;
            if restored != prior["snapshot"] {
                return Err("host_restart_snapshot_changed".into());
            }
            return Ok(json!({"status":"passed","scope":"tauri_developer_host_not_installer","restored_after_host_restart":true,"authenticated_http":true,"model_calls":0,"goal_id":id}));
        }
        let nonce = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos().to_string();
        let post = |path: &str, body: Value| -> Result<Value, String> {
            client
                .post(format!("http://127.0.0.1:3100{path}"))
                .header("Origin", "http://127.0.0.1:3100")
                .header("Idempotency-Key", format!("d00-{nonce}"))
                .json(&body)
                .send()
                .and_then(|r| r.error_for_status())
                .and_then(|r| r.json())
                .map_err(|e| e.to_string())
        };
        post(
            "/api/auth/sign-up/email",
            json!({"email":format!("d00-{nonce}@example.com"),"name":"Synthetic Tauri probe","password":format!("D00-synthetic-{nonce}-local-only")}),
        )?;
        let goal = post("/api/career-goals/", json!({"message":"我想找实习，方向还不明确。"}))?;
        let id = goal["id"].as_str().ok_or("missing_goal_id")?;
        let log = self.log("worker.log")?;
        let mut worker = Command::new(self.exe())
            .args(["probe-worker", "--config"])
            .arg(self.config.join("worker.json"))
            .args(["--goal-id", id])
            .current_dir(&self.out)
            .creation_flags(CREATE_NO_WINDOW)
            .stdout(log.try_clone().map_err(|e| e.to_string())?)
            .stderr(log)
            .spawn()
            .map_err(|e| e.to_string())?;
        let deadline = std::time::Instant::now() + Duration::from_secs(60);
        loop {
            if let Some(status) = worker.try_wait().map_err(|e| e.to_string())? {
                if !status.success() {
                    return Err("probe_worker_failed".into());
                }
                break;
            }
            if std::time::Instant::now() > deadline {
                let _ = worker.kill();
                let _ = worker.wait();
                return Err("probe_worker_timeout".into());
            }
            thread::sleep(Duration::from_millis(100));
        }
        let read = || -> Result<Value, String> {
            client
                .get(format!("http://127.0.0.1:3100/api/career-goals/{id}"))
                .send()
                .and_then(|r| r.error_for_status())
                .and_then(|r| r.json())
                .map_err(|e| e.to_string())
        };
        let original = read()?;
        if original["action"]["type"] != "ask_user" {
            return Err("expected_ask_user".into());
        }
        self.stop()?;
        self.start()?;
        if read()? != original {
            return Err("restart_snapshot_changed".into());
        }
        let cookie = jar.cookies(&base).ok_or("missing_session_cookie")?;
        fs::write(
            saved,
            serde_json::to_vec(&json!({"cookie":cookie.to_str().map_err(|e| e.to_string())?,"goal_id":id,"snapshot":original})).unwrap(),
        )
        .map_err(|e| e.to_string())?;
        Ok(json!({"status":"passed","scope":"tauri_developer_host_not_installer","action":"ask_user","authenticated_http":true,"same_cookie_after_api_restart":true,"model_calls":0,"goal_id":id,"account_retained":true}))
    }
}

impl Drop for Host {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

fn startup_message(code: &str) -> &'static str {
    match code {
        "ready" => "本地服务已就绪，可以开始练习。",
        "python_resource_missing" => "本地服务资源不完整，请重新解压完整程序目录。",
        "web_resource_missing" => "界面资源不完整，请重新解压完整程序目录。",
        "resources_incomplete" => "内置题库数据不完整，请重新解压完整程序目录。",
        "configuration_missing" => "尚未配置本地服务。请把 api.json 和 worker.json 放进下面这个配置目录，再重新打开程序。",
        "configuration_invalid" => "配置无效：本版本需要一致的测试数据库与免费脚本模式，请检查外部配置。",
        "postgres_unavailable" => "无法连接 PostgreSQL，请确认数据库已启动，并检查地址、端口和账号配置。",
        "redis_unavailable" => "无法连接 Redis，请确认 Redis 已启动，并检查地址、端口和密码配置。",
        "port_3100_in_use" => "本地端口 3100 已被占用。请关闭占用该端口的程序后重试；本程序不会接管其他程序的端口。",
        "worker_exited" => "后台脚本进程未能保持运行，请检查 Redis 配置与本地日志。",
        "sidecar_exited_before_ready" => "本地服务启动过程中退出，请查看本次运行日志。",
        "sidecar_ready_timeout" => "本地服务启动超时，请重试；若持续失败，请查看本次运行日志。",
        _ => "本地服务未能启动，请检查程序资源和本地配置；详细信息见本次运行日志。",
    }
}

#[tauri::command]
async fn prepare_desktop(state: tauri::State<'_, Shared>) -> Result<Value, String> {
    let shared = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut host = shared.lock().map_err(|_| "host_lock_failed")?;
        let result = if host.api.is_some() { Ok(()) } else { host.start() };
        let code = match result {
            Ok(()) => "ready".to_owned(),
            Err(e) => {
                let _ = host.stop();
                e
            }
        };
        let status = json!({"ready":code=="ready","code":code,"message":startup_message(&code),
            "config_dir":host.config.display().to_string()});
        fs::write(host.out.join("startup.json"), serde_json::to_vec_pretty(&status).unwrap()).ok();
        Ok(status)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn quit_app(app: tauri::AppHandle, state: tauri::State<'_, Shared>) {
    if let Ok(mut host) = state.lock() {
        let _ = host.stop();
    }
    app.exit(0);
}

#[tauri::command]
async fn run_probe(state: tauri::State<'_, Shared>) -> Result<Value, String> {
    let shared = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut host = shared.lock().map_err(|_| "host_lock_failed")?;
        let result = host.probe();
        let receipt = match &result {
            Ok(v) => v.clone(),
            Err(e) => json!({"status":"failed","error":e}),
        };
        fs::write(host.out.join("receipt.json"), serde_json::to_vec_pretty(&receipt).unwrap()).map_err(|e| e.to_string())?;
        result
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn finish_probe(app: tauri::AppHandle, state: tauri::State<'_, Shared>) {
    let host = state.lock().unwrap();
    let _ = fs::write(host.out.join("frontend-finished.txt"), "React invoked native probe and received completion");
    if host.autorun {
        thread::spawn(move || {
            thread::sleep(Duration::from_secs(3));
            app.exit(0);
        });
    }
}

/// Window controls for the undecorated product window.
///
/// The product window draws its own top bar, so it also has to provide what the
/// native title bar used to: moving the window, minimising it and closing it.
/// The command only ever touches the app's own window; it cannot read files or
/// reach anything outside this process.
#[tauri::command]
fn window_control(window: tauri::Window, action: String) -> Result<(), String> {
    match action.as_str() {
        "drag" => window.start_dragging().map_err(|e| e.to_string()),
        "minimize" => window.minimize().map_err(|e| e.to_string()),
        "toggle_maximize" => {
            // Restoring a minimised window and maximising it are different Win32
            // operations. Without this, pressing maximise while the window sits in
            // the taskbar sets the maximised flag but leaves the window minimised,
            // which looks like the button does nothing.
            if window.is_minimized().map_err(|e| e.to_string())? {
                window.unminimize().map_err(|e| e.to_string())?;
                return window.maximize().map_err(|e| e.to_string());
            }
            if window.is_maximized().map_err(|e| e.to_string())? {
                window.unmaximize().map_err(|e| e.to_string())
            } else {
                window.maximize().map_err(|e| e.to_string())
            }
        }
        "close" => window.close().map_err(|e| e.to_string()),
        other => Err(format!("unknown_window_action:{other}")),
    }
}

/// Reads the window's own state so the top bar can show the right control.
///
/// The maximise button has to show "maximise" or "restore" depending on the
/// current state; without this the button looks identical either way and a
/// working toggle reads as "nothing happened".
#[tauri::command]
fn window_state(window: tauri::Window) -> Result<Value, String> {
    Ok(json!({
        "maximized": window.is_maximized().map_err(|e| e.to_string())?,
        "minimized": window.is_minimized().map_err(|e| e.to_string())?,
    }))
}

/// Stores the local identity's password in the OS credential store (F07Z-83).
///
/// The target name is built here from a validated uuid, so a page can only reach this app's own
/// slots and cannot write over another program's credential by passing a name.
#[tauri::command]
fn local_identity_secret_set(id: String, secret: String) -> Result<(), String> {
    if secret.len() > 512 {
        return Err("local_secret_too_long".into());
    }
    local_secret::write(&id, &secret)
}

/// Reads the secret for one identity. Returns null when nothing is stored.
#[tauri::command]
fn local_identity_secret_get(id: String) -> Result<Option<String>, String> {
    local_secret::read(&id)
}

/// Removes one identity's secret.
#[tauri::command]
fn local_identity_secret_clear(id: String) -> Result<(), String> {
    local_secret::delete(&id)
}

/// Opens one address in the user's default browser (F07Z-86).
///
/// The learning list may contain a link the user typed, so a host allowlist cannot apply here -
/// unlike the practice sites, which stay inside this app's own window. Two things are still
/// enforced: only `http`/`https` reaches the shell, and nothing is done with the address beyond
/// handing it over. The browser shows the real address in its own chrome, which is also why an
/// arbitrary link is not opened in a window that looks like this application.
#[tauri::command]
fn open_external(url: String) -> Result<(), String> {
    let lower = url.to_ascii_lowercase();
    if !(lower.starts_with("http://") || lower.starts_with("https://")) {
        return Err("external_url_scheme_not_allowed".into());
    }
    if url.len() > 2000 || url.contains(['\r', '\n', '\0']) {
        return Err("external_url_invalid".into());
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::UI::Shell::ShellExecuteW;
        use windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;
        let operation = wide_z("open");
        let target = wide_z(&url);
        let handle = unsafe {
            ShellExecuteW(std::ptr::null_mut(), operation.as_ptr(), target.as_ptr(),
                          std::ptr::null(), std::ptr::null(), SW_SHOWNORMAL)
        };
        // The documented contract: a value above 32 means the shell accepted the request.
        if handle as isize <= 32 {
            return Err("external_url_open_failed".into());
        }
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let _ = lower;
        Err("external_url_unsupported_platform".into())
    }
}

fn wide_z(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}

/// Hands one absolute target to the shell. Returns false if the shell refused it.
#[cfg(windows)]
fn shell_open(target: &std::path::Path) -> bool {
    use windows_sys::Win32::UI::Shell::ShellExecuteW;
    use windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;
    let operation = wide_z("open");
    let path = wide_z(&target.display().to_string());
    let handle = unsafe {
        ShellExecuteW(std::ptr::null_mut(), operation.as_ptr(), path.as_ptr(),
                      std::ptr::null(), std::ptr::null(), SW_SHOWNORMAL)
    };
    // The documented contract: a value above 32 means the shell accepted the request.
    handle as isize > 32
}

#[cfg(not(windows))]
fn shell_open(_target: &std::path::Path) -> bool {
    false
}

/// What this machine holds, for the settings page: where the configuration and the run data
/// live, and which configuration files are present.
///
/// Paths come from the host, never from the page, so this cannot be used to look around the
/// filesystem. File contents are not included - only whether a name exists.
#[tauri::command]
fn local_data(state: tauri::State<'_, Shared>) -> Result<Value, String> {
    let host = state.lock().map_err(|_| "host_lock_failed".to_string())?;
    let installed = host.app_dir.join("build.json").is_file();
    let files: Vec<Value> = ["api.json", "worker.json", "model.json", "tracking-adapters.json"]
        .iter()
        .map(|name| json!({"name": name, "present": host.config.join(name).is_file()}))
        .collect();
    Ok(json!({
        "layout": if installed { "installed" } else { "folder" },
        "config_dir": host.config.display().to_string(),
        "run_data_dir": host.out.display().to_string(),
        "files": files,
        "config_from_env": std::env::var_os("INTERVIEW_DESKTOP_CONFIG_DIR").is_some(),
    }))
}

/// Opens one of this app's two data folders in the system file manager.
///
/// The caller passes a name, not a path: `config` or `run`. There is no argument that could
/// point somewhere else on the disk.
#[tauri::command]
fn open_data_folder(state: tauri::State<'_, Shared>, kind: String) -> Result<(), String> {
    let host = state.lock().map_err(|_| "host_lock_failed".to_string())?;
    let target = match kind.as_str() {
        "config" => host.config.clone(),
        "run" => host.out.clone(),
        other => return Err(format!("unknown_data_folder:{other}")),
    };
    // The configuration directory may not exist yet on a folder build that was never configured.
    let _ = fs::create_dir_all(&target);
    if shell_open(&target) {
        Ok(())
    } else {
        Err("data_folder_open_failed".into())
    }
}

/// Opens one configured practice site in its own window (F07Z-58).
///
/// The window is deliberately given **no capabilities**: the capability file only lists the
/// `main` window, so a remote page here cannot call `window_control`, `quit_app` or anything
/// else this app exposes. The allowlist below is the second guard: without it this command
/// would be a general-purpose https launcher reachable from our own page. Adding a site to
/// `desktop/practice_sites.py` also adds its host here (and vice versa) — the two lists are
/// small and checked against each other by hand.
const PRACTICE_HOSTS: [&str; 13] = [
    "www.nowcoder.com",
    "nowcoder.com",
    "leetcode.cn",
    "www.leetcode.cn",
    "leetcode.com",
    "www.luogu.com.cn",
    "www.acwing.com",
    "www.hackerrank.com",
    "codeforces.com",
    "atcoder.jp",
    "www.acmcoder.com",
    "www.lanqiao.cn",
    "www.patest.cn",
];

#[tauri::command]
async fn open_practice_window(app: tauri::AppHandle, url: String) -> Result<String, String> {
    let parsed = tauri::Url::parse(&url).map_err(|_| "practice_url_invalid".to_string())?;
    if parsed.scheme() != "https" {
        return Err("practice_https_required".into());
    }
    let host = parsed.host_str().unwrap_or("").to_lowercase();
    if !PRACTICE_HOSTS.iter().any(|allowed| host == *allowed) {
        return Err("practice_host_not_allowed".into());
    }
    let label = format!("practice-{}", host.replace('.', "-"));
    if let Some(existing) = app.get_webview_window(&label) {
        // One window per site: clicking "力扣 · 热题 100" after "力扣 · 题库" should take that
        // window to the new page rather than silently focusing a stale one. The URL has already
        // passed the allowlist above, so this cannot be turned into an arbitrary navigation.
        let _ = existing.navigate(parsed);
        let _ = existing.set_focus();
        return Ok(label);
    }
    let builder = tauri::WebviewWindowBuilder::new(&app, &label, tauri::WebviewUrl::External(parsed))
        .title("算法练习 · 外部站点")
        .inner_size(1200.0, 900.0)
        .min_inner_size(760.0, 520.0)
        .drag_and_drop(false);
    // WebView2 allows several windows on one user-data folder only when their browser
    // options match; a second window built with default options while the first carries
    // the debug port fails with ERROR_INVALID_STATE (0x8007139F). Mirroring the same args
    // keeps both windows on one environment. In the product build no variable is set, so
    // both sides use the default options.
    let builder = devtools_args(builder, "INTERVIEW_DESKTOP_PREVIEW_CDP_PORT");
    builder.build().map_err(|e| e.to_string())?;
    Ok(label)
}

/// Injects a small window-control bridge into every page the app loads.
///
/// The workbench is served from the local API (http://127.0.0.1:3100) rather than
/// from the app's own asset origin. Tauri still injects its global bridge there,
/// but the page cannot rely on it being present at first render, so the app
/// polls for it. This script only marks the bridge as present; the actual calls
/// go through the same `window_control` command the top bar uses.
fn inject_window_bridge(webview: &tauri::WebviewWindow) {
    let script = r#"
(function () {
  if (window.__PY_WINDOW_BRIDGE__) { return; }
  window.__PY_WINDOW_BRIDGE__ = { available: true };
})();
"#;
    let _ = webview.eval(script);
}

/// Optional loopback-only DevTools port for automated acceptance. OFF by default, and
/// only honoured for the two modes that are never shipped.
fn devtools_args<'a, M: tauri::Manager<tauri::Wry>>(
    builder: tauri::WebviewWindowBuilder<'a, tauri::Wry, M>,
    var: &str,
) -> tauri::WebviewWindowBuilder<'a, tauri::Wry, M> {
    if let Ok(value) = std::env::var(var) {
        if let Ok(port) = value.parse::<u16>() {
            if port >= 1024 {
                return builder.additional_browser_args(&format!(
                    "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection \
                     --autoplay-policy=no-user-gesture-required \
                     --remote-debugging-address=127.0.0.1 --remote-debugging-port={port} \
                     --remote-allow-origins=*"
                ));
            }
        }
    }
    builder
}

/// F07Z-62: the desktop goes live only when the user explicitly enabled it and the API
/// created a capped authorization. Both fields are non-secret and live in model.json.
fn live_authorization(model: &Value) -> Option<(String, u64)> {
    if model.get("enabled").and_then(Value::as_bool) != Some(true) {
        return None;
    }
    let id = model
        .get("live_authorization")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    let cap = model.get("live_cap_nano").and_then(Value::as_u64).unwrap_or(0);
    if id.is_empty() || cap == 0 || cap > 2_000_000_000 {
        return None;
    }
    Some((id, cap))
}

/// S03c: planning has its own switch, cap and authorization. It never rides on the interview
/// flag, and the ceiling is the same 2 CNY the other capabilities use.
fn prep_live_authorization(model: &Value) -> Option<(String, u64)> {
    if model.get("prep_enabled").and_then(Value::as_bool) != Some(true) {
        return None;
    }
    let id = model
        .get("prep_live_authorization")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    let cap = model.get("prep_live_cap_nano").and_then(Value::as_u64).unwrap_or(0);
    if id.is_empty() || cap == 0 || cap > 2_000_000_000 {
        return None;
    }
    Some((id, cap))
}

/// F07Z-112: materials have their own switch, cap and authorization (policy ja04); they are
/// deliberately not tied to the interview `enabled` flag.
fn material_live_authorization(model: &Value) -> Option<(String, u64)> {
    if model.get("material_enabled").and_then(Value::as_bool) != Some(true) {
        return None;
    }
    let id = model
        .get("material_live_authorization")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    let cap = model.get("material_live_cap_nano").and_then(Value::as_u64).unwrap_or(0);
    if id.is_empty() || cap == 0 || cap > 2_000_000_000 {
        return None;
    }
    Some((id, cap))
}

/// Where logs and run state go when the launcher does not say.
///
/// Installed builds are marked by `build.json` beside the executable, which only the installer
/// ships. Everything else is a folder build and keeps its data next to itself.
fn default_output_dir(app_dir: &Path) -> PathBuf {
    if app_dir.join("build.json").is_file() {
        if let Some(base) = std::env::var_os("LOCALAPPDATA") {
            return PathBuf::from(base)
                .join("dev.simmonsep.interview.probe")
                .join("run-data");
        }
    }
    app_dir.join("run-data")
}

/// Configuration directory for this layout, seeded on first run.
///
/// A folder build keeps `settings/` beside the executable, unchanged. An installed build uses
/// `%LOCALAPPDATA%\dev.simmonsep.interview.probe\settings`: the installer owns the program
/// directory, and user configuration must survive both an upgrade and an uninstall.
fn prepare_config_dir(app_dir: &Path) -> PathBuf {
    let beside = app_dir.join("settings");
    if !app_dir.join("build.json").is_file() {
        return beside;
    }
    let Some(base) = std::env::var_os("LOCALAPPDATA") else {
        return beside;
    };
    let dir = PathBuf::from(base)
        .join("dev.simmonsep.interview.probe")
        .join("settings");
    if !dir.is_dir() {
        // Ship the editable template only: it contains no credentials, and the user supplies
        // api.json / worker.json themselves.
        let template = app_dir.join("settings.template");
        if template.is_dir() {
            let _ = copy_files(&template, &dir);
        }
    }
    // A one-time move for installs that kept their settings beside the executable.
    if beside.is_dir()
        && !dir.join("api.json").is_file()
        && beside.join("api.json").is_file()
    {
        let _ = copy_files(&beside, &dir);
    }
    dir
}

fn copy_files(from: &Path, to: &Path) -> std::io::Result<()> {
    fs::create_dir_all(to)?;
    for entry in fs::read_dir(from)?.flatten() {
        let path = entry.path();
        if path.is_file() {
            let _ = fs::copy(&path, to.join(entry.file_name()));
        }
    }
    Ok(())
}

fn main() {
    let preview = std::env::var_os("INTERVIEW_DESKTOP_PREVIEW").is_some();
    let probe = std::env::var_os("INTERVIEW_DESKTOP_PROBE").is_some();
    let app_dir = std::env::current_exe().expect("Executable path unavailable").parent().unwrap().to_path_buf();
    let resources = std::env::var_os("INTERVIEW_DESKTOP_RESOURCES").map(PathBuf::from).unwrap_or_else(|| app_dir.join("sidecar"));
    // F07Z-87: an installed build keeps its configuration under the user's application data
    // too. Inside the program directory it was user data the uninstaller could not remove and
    // an upgrade could not preserve; here it survives both.
    let config = std::env::var_os("INTERVIEW_DESKTOP_CONFIG_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| prepare_config_dir(&app_dir));
    // F07Z-85: an installed build keeps its logs and state under the user's application data,
    // not inside the program directory. The program directory belongs to the installer: an
    // uninstaller cannot know about a folder the app invented, and a per-machine install under
    // Program Files is read-only for the app. A folder build (no `build.json` next to the exe)
    // keeps everything beside itself, so the folder stays copyable.
    let out = std::env::var_os("INTERVIEW_PROBE_OUTPUT")
        .map(PathBuf::from)
        .unwrap_or_else(|| default_output_dir(&app_dir));
    // The Vite preview owns no sidecar, no config and no run data: it borrows the API that
    // is already running on 3100 and closes with its own window.
    if !preview {
        fs::create_dir_all(&out).expect("Output directory unavailable");
    }
    let host = Arc::new(Mutex::new(Host {
        resources,
        app_dir: app_dir.clone(),
        config,
        out,
        api: None,
        workers: Vec::new(),
        autorun: probe && std::env::var_os("INTERVIEW_PROBE_AUTORUN").is_some(),
    }));
    let cleanup = host.clone();
    tauri::Builder::default()
        .manage(host)
        .invoke_handler(tauri::generate_handler![prepare_desktop, quit_app, run_probe, finish_probe, window_control, window_state, open_practice_window, open_external,
            local_data, open_data_folder,
            local_identity_secret_set, local_identity_secret_get, local_identity_secret_clear])
        .setup(move |app| {
            if preview {
                // Development only: the WebView loads the same React workbench from Vite,
                // which forwards /api to the already-running FastAPI.
                let builder = tauri::WebviewWindowBuilder::new(
                    app,
                    "main",
                    tauri::WebviewUrl::External(PREVIEW_URL.parse().expect("Invalid preview URL")),
                )
                .title("求职 Agent · 桌面工作台（Tauri 预览）")
                .decorations(false)
                .inner_size(1280.0, 860.0)
                .min_inner_size(880.0, 600.0)
                .drag_and_drop(false)
                .on_navigation(|url| url.scheme() == "http" && url.host_str() == Some("127.0.0.1") && url.port() == Some(5174))
                .on_page_load(|webview, _payload| {
                    inject_window_bridge(&webview);
                });
                devtools_args(builder, "INTERVIEW_DESKTOP_PREVIEW_CDP_PORT").build()?;
            } else if probe {
                let state = app.state::<Shared>();
                let host = state.lock().unwrap();
                let url = if host.autorun { "desktop.html?probe=1" } else { "desktop.html" };
                tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::App(url.into()))
                    .title("求职 Agent · 桌面接缝验证")
                    .inner_size(860.0, 600.0)
                    .build()?;
            } else {
                // Product: the window opens on the startup page, which asks the host to
                // prepare the local stack and then moves to the workbench on the same
                // origin as the API. Nothing here can read files or run commands.
                // The window is undecorated: the native Windows title bar was a second
                // frame around the app, and the app's own top bar is the only chrome.
                // The top bar therefore has to provide what the title bar used to:
                // dragging the window, minimising it and closing it.
                let mut builder = tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::App("startup.html".into()))
                    .title("求职 Agent · 桌面工作台")
                    .decorations(false)
                    .inner_size(1280.0, 860.0)
                    .min_inner_size(880.0, 600.0)
                    .drag_and_drop(false)
                    .on_navigation(|url| {
                        // Windows serves the bundled pages from http://tauri.localhost, other
                        // platforms from tauri://localhost; both are the app's own origin.
                        // The only remote origin allowed is the local API that serves the
                        // built frontend.
                        let host = url.host_str().unwrap_or("");
                        match url.scheme() {
                            "tauri" | "about" => true,
                            "http" | "https" => {
                                (host == "tauri.localhost" || host == "localhost" && url.port().is_none())
                                    || host == "127.0.0.1" && url.port() == Some(3100)
                            }
                            _ => false,
                        }
                    })
                    // Every page the app loads gets the window-control bridge, so
                    // the app's own top bar can drive the window it lives in.
                    .on_page_load(|webview, _payload| {
                        inject_window_bridge(&webview);
                    });
                // The profile directory is a launcher parameter like the rest: it lets an
                // acceptance run start from a first-run profile without touching the one a
                // real user already populated.
                if let Some(dir) = std::env::var_os("INTERVIEW_DESKTOP_DATA_DIR") {
                    builder = builder.data_directory(PathBuf::from(dir));
                }
                devtools_args(builder, "INTERVIEW_DESKTOP_CDP_PORT").build()?;
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Desktop initialization failed")
        .run(move |_app, event| {
            if matches!(event, tauri::RunEvent::Exit) {
                let _ = cleanup.lock().unwrap().stop();
            }
        });
}

#[cfg(test)]
mod live_authorization_tests {
    use super::live_authorization;
    use serde_json::json;

    #[test]
    fn disabled_or_incomplete_settings_never_go_live() {
        assert_eq!(live_authorization(&json!({})), None);
        assert_eq!(live_authorization(&json!({"enabled": false,
            "live_authorization": "desktop-live-x", "live_cap_nano": 100000000})), None);
        assert_eq!(live_authorization(&json!({"enabled": true,
            "live_authorization": "", "live_cap_nano": 100000000})), None);
        assert_eq!(live_authorization(&json!({"enabled": true,
            "live_authorization": "desktop-live-x", "live_cap_nano": 0})), None);
    }

    #[test]
    fn cap_above_the_hard_limit_is_refused() {
        assert_eq!(live_authorization(&json!({"enabled": true,
            "live_authorization": "desktop-live-x", "live_cap_nano": 3_000_000_000u64})), None);
    }

    #[test]
    fn explicit_enable_returns_the_authorization_and_cap() {
        assert_eq!(live_authorization(&json!({"enabled": true,
            "live_authorization": "desktop-live-abc-500", "live_cap_nano": 500_000_000})),
            Some(("desktop-live-abc-500".to_string(), 500_000_000)));
    }

    #[test]
    fn planning_uses_its_own_switch_and_never_the_interview_one() {
        use super::prep_live_authorization;
        assert_eq!(prep_live_authorization(&json!({"enabled": true,
            "live_authorization": "desktop-live-x", "live_cap_nano": 100000000})), None);
        assert_eq!(prep_live_authorization(&json!({"live_authorization": "desktop-live-x",
            "live_cap_nano": 100000000})), None);
        assert_eq!(prep_live_authorization(&json!({"prep_enabled": false,
            "prep_live_authorization": "desktop-prep-x", "prep_live_cap_nano": 100000000})), None);
        assert_eq!(prep_live_authorization(&json!({"prep_enabled": true,
            "prep_live_authorization": "", "prep_live_cap_nano": 100000000})), None);
        assert_eq!(prep_live_authorization(&json!({"prep_enabled": true,
            "prep_live_authorization": "desktop-prep-x", "prep_live_cap_nano": 3_000_000_000u64})), None);
        assert_eq!(prep_live_authorization(&json!({"prep_enabled": true,
            "prep_live_authorization": "desktop-prep-x", "prep_live_cap_nano": 100000000})),
            Some(("desktop-prep-x".to_string(), 100_000_000)));
    }

    #[test]
    fn materials_use_their_own_switch_and_never_the_interview_one() {
        use super::material_live_authorization;
        assert_eq!(material_live_authorization(&json!({"enabled": true,
            "live_authorization": "desktop-live-x", "live_cap_nano": 100000000})), None);
        assert_eq!(material_live_authorization(&json!({"material_enabled": false,
            "material_live_authorization": "desktop-material-live-x",
            "material_live_cap_nano": 100000000})), None);
        assert_eq!(material_live_authorization(&json!({"material_enabled": true,
            "material_live_authorization": "", "material_live_cap_nano": 100000000})), None);
        assert_eq!(material_live_authorization(&json!({"material_enabled": true,
            "material_live_authorization": "desktop-material-live-x",
            "material_live_cap_nano": 3_000_000_000u64})), None);
        assert_eq!(material_live_authorization(&json!({"material_enabled": true,
            "material_live_authorization": "desktop-material-live-abc-500",
            "material_live_cap_nano": 500_000_000})),
            Some(("desktop-material-live-abc-500".to_string(), 500_000_000)));
    }
}
