/*
Copyright 2026 Firefly Software Foundation.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
Author: Firefly Software Foundation
SPDX-License-Identifier: Apache-2.0
*/
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Deserialize;
use std::{
    io::ErrorKind,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex, MutexGuard,
    },
    time::Duration,
};
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    webview::DownloadEvent,
    Manager, Url, WebviewUrl, WebviewWindowBuilder,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

struct ActiveHost {
    child: CommandChild,
    label: String,
    profile: Option<PathBuf>,
}
#[derive(Default)]
struct HostState {
    active: Mutex<Option<ActiveHost>>,
    starting: AtomicBool,
    sequence: AtomicU64,
    login: Mutex<Option<CommandChild>>,
    signing_in: AtomicBool,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Bootstrap {
    #[serde(rename = "type")]
    kind: String,
    version: u8,
    origin: String,
    pairing_code: String,
}

fn validate_bootstrap(raw: &[u8]) -> Result<Bootstrap, &'static str> {
    if raw.len() > 1024 {
        return Err("Oversized host bootstrap");
    }
    let value: Bootstrap = serde_json::from_slice(raw).map_err(|_| "Invalid host bootstrap")?;
    let origin: tauri::Url = value.origin.parse().map_err(|_| "Invalid host origin")?;
    if value.kind != "weave-desktop-ready"
        || value.version != 1
        || origin.scheme() != "http"
        || origin.host_str() != Some("127.0.0.1")
        || origin.port().is_none()
        || !origin.username().is_empty()
        || origin.password().is_some()
        || origin.path() != "/"
        || origin.query().is_some()
        || origin.fragment().is_some()
        || value.origin != format!("http://127.0.0.1:{}", origin.port().unwrap())
        || value.pairing_code.len() < 24
        || value.pairing_code.len() > 128
        || !value
            .pairing_code
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        return Err("Host bootstrap violates the loopback policy");
    }
    Ok(value)
}
fn stop_host(mut host: ActiveHost) {
    let _ = host.child.write(b"STOP\n");
    let _ = host.child.kill();
}
fn report(app: &tauri::AppHandle, message: &str) {
    app.dialog()
        .message(message)
        .title("Firefly Weave Studio")
        .show(|_| {});
}
fn report_error(app: &tauri::AppHandle, message: &str) {
    app.dialog()
        .message(message)
        .title("Firefly Weave Studio")
        .kind(MessageDialogKind::Error)
        .show(|_| {});
}

/// Pairs a Studio window with its own host. It runs at document start in the main frame of every page load,
/// so a reload after the host session ended pairs again whenever the host still accepts the desktop code.
/// The code only travels in a same-origin POST body, never in a URL. A refused code (403) leaves the page to
/// explain the next step, and at most three automatic re-pairing reloads a minute stop a session cookie that
/// never sticks from turning into a reload loop. Session storage holds only those timestamps.
///
/// The code stays reusable for the host's lifetime, so page scripts must never reach it: the request body is
/// encoded in Rust and held in this function's scope, so no global a page could replace (`JSON.stringify`,
/// `fetch`) sees it, and the callbacks handed to promises (whose source text `Function.prototype.toString`
/// reveals) only name it.
const PAIRING_SCRIPT: &str = r#"(() => {
  if (window.top !== window || location.origin !== __WEAVE_ORIGIN__) return;
  const body = __WEAVE_BODY__;
  const request = window.fetch.bind(window);
  const key = 'weave-desktop-pairing';
  const now = Date.now();
  let recent = null;
  try {
    const stored = JSON.parse(sessionStorage.getItem(key) || '[]');
    recent = Array.isArray(stored)
      ? stored.filter((t) => typeof t === 'number' && t <= now && now - t < 60000)
      : [];
  } catch (_) {
    recent = null;
  }
  request('/studio/session', { credentials: 'same-origin', cache: 'no-store' })
    .then((r) => (r.ok ? r.json() : null))
    .then((s) => {
      if (!s || s.paired !== false) return;
      if (recent !== null && recent.length >= 3) return;
      return request('/studio/session', {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body,
      }).then((r) => {
        if (!r.ok) return;
        if (recent === null) {
          // Without session storage, allow one automatic reload per navigation the person started.
          const entry = performance.getEntriesByType('navigation')[0];
          if (entry && entry.type === 'reload') return;
        } else {
          try {
            sessionStorage.setItem(key, JSON.stringify(recent.concat(now)));
          } catch (_) {
            return;
          }
        }
        location.reload();
      });
    })
    .catch(() => {});
})();"#;

fn pairing_script(origin: &str, code: &str) -> String {
    // The body is the JSON request document, embedded as one JavaScript string literal.
    let body = serde_json::json!({ "code": code }).to_string();
    PAIRING_SCRIPT
        .replacen(
            "__WEAVE_ORIGIN__",
            &serde_json::to_string(origin).expect("a string always encodes as JSON"),
            1,
        )
        .replacen(
            "__WEAVE_BODY__",
            &serde_json::to_string(&body).expect("a string always encodes as JSON"),
            1,
        )
}

/// Plain-text formats Studio exports. Any other name gets a `.txt` suffix, so a download can never land as a
/// file the system would execute or hand to another application.
const SAFE_DOWNLOAD_EXTENSIONS: [&str; 5] = ["json", "yaml", "yml", "txt", "csv"];
const MAX_DOWNLOAD_NAME_BYTES: usize = 128;
const MAX_NAME_ATTEMPTS: u32 = 1000;
const MAX_PENDING_DOWNLOADS: usize = 32;
const FALLBACK_DOWNLOAD_STEM: &str = "studio-export";

/// True when the Studio window may navigate to `url`: its own origin, without user info. On macOS only `http`
/// pages qualify: WKWebView hands downloads to the download handler without asking this check, and where
/// that API is missing (before macOS 11.3) an exported same-origin `blob:` file could otherwise replace
/// Studio and every unsaved edit. WebKitGTK asks this check before it starts a `blob:` download, so other
/// platforms keep accepting same-origin `blob:` URLs here.
fn navigation_allowed(url: &Url, owned_origin: &str) -> bool {
    (url.scheme() == "http" || !cfg!(target_os = "macos"))
        && url.username().is_empty()
        && url.password().is_none()
        && url.origin().ascii_serialization() == owned_origin
}

/// True for a `blob:` URL minted by the owned origin, or an `http` URL on the owned origin itself.
fn download_allowed(url: &Url, owned_origin: &str) -> bool {
    let source = if url.scheme() == "blob" {
        match Url::parse(url.path()) {
            Ok(inner) => inner,
            Err(_) => return false,
        }
    } else {
        url.clone()
    };
    source.scheme() == "http"
        && source.username().is_empty()
        && source.password().is_none()
        && source.origin().ascii_serialization() == owned_origin
}

fn is_unsafe_name_char(c: char) -> bool {
    c.is_control()
        || matches!(c, '/' | '\\' | '<' | '>' | ':' | '"' | '|' | '?' | '*')
        // Bidirectional controls can disguise an extension ("report\u{202E}nosj.exe").
        || matches!(c, '\u{061C}' | '\u{200E}' | '\u{200F}' | '\u{202A}'..='\u{202E}' | '\u{2066}'..='\u{2069}')
}

fn trim_name(text: &str) -> &str {
    text.trim_matches(|c: char| c.is_whitespace() || c == '.')
}

/// Reduces a suggested download name to a bounded base name with a safe extension: no path separators,
/// no reserved or control characters, no leading dots, and never a Windows device name.
fn sanitize_download_name(suggested: &str) -> String {
    let base = suggested.rsplit(['/', '\\']).next().unwrap_or_default();
    let cleaned: String = base
        .chars()
        .map(|c| if is_unsafe_name_char(c) { '_' } else { c })
        .collect();
    let cleaned = trim_name(&cleaned);
    let (stem, extension) = match cleaned.rsplit_once('.') {
        Some((stem, extension))
            if !trim_name(stem).is_empty()
                && SAFE_DOWNLOAD_EXTENSIONS.contains(&extension.to_ascii_lowercase().as_str()) =>
        {
            (trim_name(stem), extension.to_ascii_lowercase())
        }
        _ => (cleaned, "txt".to_string()),
    };
    let mut stem = if stem.is_empty() {
        FALLBACK_DOWNLOAD_STEM.to_string()
    } else {
        stem.to_string()
    };
    let device = stem
        .split('.')
        .next()
        .unwrap_or_default()
        .trim()
        .to_ascii_uppercase();
    let reserved = matches!(device.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || ((device.starts_with("COM") || device.starts_with("LPT"))
            && device.len() == 4
            && device.as_bytes()[3].is_ascii_digit());
    if reserved {
        stem.insert(0, '_');
    }
    let budget = MAX_DOWNLOAD_NAME_BYTES - extension.len() - 1;
    if stem.len() > budget {
        let mut end = budget;
        while !stem.is_char_boundary(end) {
            end -= 1;
        }
        stem.truncate(end);
        stem = trim_name(&stem).to_string();
        if stem.is_empty() {
            stem = FALLBACK_DOWNLOAD_STEM.to_string();
        }
    }
    format!("{stem}.{extension}")
}

/// The first free `name`, `stem (1).ext`, `stem (2).ext`, … in `folder`. `None` when the folder cannot be
/// inspected (for example when privacy settings deny access) or every candidate is taken.
fn unique_destination(folder: &Path, name: &str) -> Option<PathBuf> {
    let (stem, extension) = name.rsplit_once('.').unwrap_or((name, "txt"));
    for attempt in 0..MAX_NAME_ATTEMPTS {
        let candidate = if attempt == 0 {
            folder.join(name)
        } else {
            folder.join(format!("{stem} ({attempt}).{extension}"))
        };
        match candidate.symlink_metadata() {
            Err(error) if error.kind() == ErrorKind::NotFound => return Some(candidate),
            Ok(_) => continue,
            Err(_) => return None,
        }
    }
    None
}

#[derive(Debug, PartialEq)]
enum DownloadRefusal {
    Foreign,
    NoFolder,
    Unavailable(String),
}

/// Decides where a requested download is saved, or why it is refused.
fn plan_download(
    url: &Url,
    owned_origin: &str,
    suggested: &Path,
    folder: Option<PathBuf>,
) -> Result<PathBuf, DownloadRefusal> {
    if !download_allowed(url, owned_origin) {
        return Err(DownloadRefusal::Foreign);
    }
    let name = sanitize_download_name(
        &suggested
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_default(),
    );
    let folder = folder.ok_or(DownloadRefusal::NoFolder)?;
    unique_destination(&folder, &name).ok_or(DownloadRefusal::Unavailable(name))
}

/// Saves Studio exports from one window to the Downloads folder. The webview reports where each download
/// came from; only the window's own loopback origin may save files, and failures surface as native dialogs
/// because the page cannot learn whether a save finished.
struct Downloads {
    origin: String,
    pending: Mutex<Vec<(String, PathBuf)>>,
    blocked_reported: AtomicBool,
}

/// Whether a finished download failed. wry 0.57 keeps a single failure flag per WebKitGTK web context, so after
/// one failed or refused download every later one reports failure too; where that happens (`sticky_failures`), a
/// file at the planned destination means the save worked. WebKitGTK only moves a download there once complete.
fn save_failed(success: bool, destination: &Path, sticky_failures: bool) -> bool {
    !(success || (sticky_failures && destination.is_file()))
}

fn file_label(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default()
}

impl Downloads {
    fn new(origin: String) -> Self {
        Self {
            origin,
            pending: Mutex::new(Vec::new()),
            blocked_reported: AtomicBool::new(false),
        }
    }
    fn pending(&self) -> MutexGuard<'_, Vec<(String, PathBuf)>> {
        self.pending
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
    fn handle(&self, app: &tauri::AppHandle, event: DownloadEvent<'_>) -> bool {
        match event {
            DownloadEvent::Requested { url, destination } => {
                let folder = app.path().download_dir().ok();
                match plan_download(&url, &self.origin, destination, folder) {
                    Ok(path) => {
                        let mut pending = self.pending();
                        // A download that never reports back must not grow this list without bound.
                        if pending.len() >= MAX_PENDING_DOWNLOADS {
                            pending.remove(0);
                        }
                        pending.push((url.to_string(), path.clone()));
                        *destination = path;
                        true
                    }
                    Err(DownloadRefusal::Foreign) => {
                        if !self.blocked_reported.swap(true, Ordering::SeqCst) {
                            report_error(
                                app,
                                "Studio blocked a download that did not come from this Studio window.",
                            );
                        }
                        false
                    }
                    Err(DownloadRefusal::NoFolder) => {
                        report_error(
                            app,
                            "Studio could not find your Downloads folder, so the export was not saved.",
                        );
                        false
                    }
                    Err(DownloadRefusal::Unavailable(name)) => {
                        report_error(app, &format!("Studio could not save {name} to your Downloads folder. Check that Firefly Weave Studio may use that folder in your system privacy settings, then export again."));
                        false
                    }
                }
            }
            DownloadEvent::Finished { url, success, .. } => {
                let planned = {
                    let mut pending = self.pending();
                    pending
                        .iter()
                        .position(|(requested, _)| requested == url.as_str())
                        .map(|index| pending.remove(index).1)
                };
                if let Some(path) = planned {
                    if save_failed(success, &path, cfg!(target_os = "linux")) {
                        let name = file_label(&path);
                        report_error(app, &format!("Studio could not save {name} to your Downloads folder. Check that the folder is available, then export again."));
                    }
                }
                true
            }
            _ => false,
        }
    }
}
async fn launch(app: tauri::AppHandle, profile: Option<PathBuf>) -> Result<(), String> {
    if let Some(child) = app.state::<HostState>().login.lock().unwrap().take() {
        let _ = child.kill();
    }
    let mut command = app
        .shell()
        .sidecar("weave-studio-host")
        .map_err(|_| "Packaged host unavailable")?;
    if let Some(path) = profile.clone() {
        command = command.arg("--profile").arg(path);
    }
    let (mut events, child) = command
        .spawn()
        .map_err(|_| "Could not start the packaged host")?;
    let ready = tokio::time::timeout(Duration::from_secs(45), async {
        let mut seen = 0usize;
        while let Some(event) = events.recv().await {
            match event {
                CommandEvent::Stdout(raw) => {
                    seen += raw.len();
                    if seen > 65536 {
                        return Err("Host startup output exceeded its limit");
                    }
                    if let Ok(bootstrap) = validate_bootstrap(&raw) {
                        return Ok(bootstrap);
                    }
                }
                CommandEvent::Terminated(_) | CommandEvent::Error(_) => {
                    return Err("Host startup failed; check your profile and bundle")
                }
                _ => {}
            }
        }
        Err("Host closed before startup")
    })
    .await;
    let bootstrap = match ready {
        Ok(Ok(value)) => value,
        _ => {
            let _ = child.kill();
            return Err("Studio host did not become ready. Check the selected profile and application bundle.".into());
        }
    };
    let origin: tauri::Url = bootstrap
        .origin
        .parse()
        .map_err(|_| "Invalid host origin")?;
    let allowed_origin = origin.origin().ascii_serialization();
    let script = pairing_script(&bootstrap.origin, &bootstrap.pairing_code);
    let downloads = Arc::new(Downloads::new(allowed_origin.clone()));
    let sequence = app
        .state::<HostState>()
        .sequence
        .fetch_add(1, Ordering::SeqCst);
    let label = format!("studio-{sequence}");
    let built = WebviewWindowBuilder::new(&app, &label, WebviewUrl::External(origin))
        .title("Firefly Weave Studio")
        .inner_size(1440.0, 900.0)
        .min_inner_size(600.0, 500.0)
        .initialization_script(script)
        .incognito(true)
        .disable_drag_drop_handler()
        .on_navigation(move |url| navigation_allowed(url, &allowed_origin))
        .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
        // Without a handler WKWebView cancels every download, so exports would silently do nothing.
        .on_download(move |webview, event| downloads.handle(webview.app_handle(), event))
        .build();
    if built.is_err() {
        let _ = child.kill();
        return Err("Could not open the Studio window".into());
    }
    let old = app
        .state::<HostState>()
        .active
        .lock()
        .unwrap()
        .replace(ActiveHost {
            child,
            label: label.clone(),
            profile,
        });
    if let Some(host) = old {
        if let Some(window) = app.get_webview_window(&host.label) {
            let _ = window.close();
        }
        stop_host(host);
    }
    if let Some(window) = app.get_webview_window("launcher") {
        let _ = window.close();
    }
    tauri::async_runtime::spawn(async move {
        while let Some(event) = events.recv().await {
            if matches!(event, CommandEvent::Terminated(_)) {
                let state = app.state::<HostState>();
                let active = state.active.lock().unwrap();
                if active.as_ref().is_some_and(|h| h.label == label) {
                    report(
                        &app,
                        "The local Studio host stopped. Choose Studio → Reopen Studio to continue.",
                    );
                }
                break;
            }
        }
    });
    Ok(())
}
fn request_launch(app: tauri::AppHandle, profile: Option<PathBuf>) {
    if app
        .state::<HostState>()
        .starting
        .swap(true, Ordering::SeqCst)
    {
        return;
    }
    tauri::async_runtime::spawn(async move {
        if let Err(message) = launch(app.clone(), profile).await {
            report(&app, &message);
        }
        app.state::<HostState>()
            .starting
            .store(false, Ordering::SeqCst);
    });
}
/// Restarts the host, either with the shared saved platforms or with one profile file the person picks.
fn restart_studio(app: tauri::AppHandle, pick_profile: bool) {
    std::thread::spawn(move || {
        if !app
            .dialog()
            .message(if pick_profile {
                "Studio restarts to open a profile file. Export unsaved workflows before continuing."
            } else {
                "Studio restarts with your saved platforms. Export unsaved workflows before continuing."
            })
            .title("Restart Studio")
            .buttons(MessageDialogButtons::OkCancel)
            .blocking_show()
        {
            return;
        }
        let profile = if pick_profile {
            match app
                .dialog()
                .file()
                .add_filter("Studio profile", &["json"])
                .blocking_pick_file()
            {
                Some(file) => match file.into_path() {
                    Ok(path) => Some(path),
                    Err(_) => {
                        report(&app, "Choose a local profile JSON file.");
                        return;
                    }
                },
                None => return,
            }
        } else {
            None
        };
        request_launch(app, profile);
    });
}
fn sign_in(app: tauri::AppHandle) {
    if app.state::<HostState>().starting.load(Ordering::SeqCst) {
        report(&app, "Wait for Studio to finish opening before signing in.");
        return;
    }
    let profile = app
        .state::<HostState>()
        .active
        .lock()
        .unwrap()
        .as_ref()
        .and_then(|host| host.profile.clone());
    let Some(profile) = profile else {
        report(
            &app,
            "Sign in from the platform menu at the top of the Studio window. This menu item signs in only after you open a profile file with Choose platform profile.",
        );
        return;
    };
    if app
        .state::<HostState>()
        .signing_in
        .swap(true, Ordering::SeqCst)
    {
        return;
    }
    tauri::async_runtime::spawn(async move {
        let result = async {
            let command = app.shell().sidecar("weave-studio-host").map_err(|_| "Sign-in host unavailable")?
                .arg("--login").arg("--profile").arg(profile);
            let (mut events, child) = command.spawn().map_err(|_| "Could not start sign-in")?;
            app.state::<HostState>().login.lock().unwrap().replace(child);
            let completed = tokio::time::timeout(Duration::from_secs(910), async {
                let mut seen = 0usize;
                while let Some(event) = events.recv().await {
                    match event {
                        CommandEvent::Stdout(raw) => {
                            seen += raw.len();
                            if seen > 65536 || raw.len() > 8192 { return Err("Invalid sign-in response"); }
                            let value: serde_json::Value = serde_json::from_slice(&raw).map_err(|_| "Invalid sign-in response")?;
                            if value["type"] == "weave-desktop-login-complete" { return Ok(()); }
                            if value["type"] == "weave-desktop-login" {
                                let uri = value["uri"].as_str().filter(|v| v.len() <= 4096).ok_or("Invalid sign-in address")?;
                                let code = value["code"].as_str().filter(|v| v.len() <= 200 && v.bytes().all(|b| (32..127).contains(&b))).ok_or("Invalid sign-in code")?;
                                report(&app, &format!("Complete sign-in in your default browser at {uri}\n\nEnter this code: {code}\n\nKeep Studio open while signing in. The selected authentication configuration validates the destination."));
                            }
                        },
                        CommandEvent::Terminated(_) | CommandEvent::Error(_) => return Err("Sign-in did not complete. Check the selected OAuth configuration and device-flow support."),
                        _ => {},
                    }
                }
                Err("Sign-in host closed")
            }).await;
            if let Some(child) = app.state::<HostState>().login.lock().unwrap().take() { let _ = child.kill(); }
            completed.map_err(|_| "Sign-in timed out")?
        }.await;
        match result {
            Ok(()) => report(
                &app,
                "Signed in. Choose View → Reload Studio to load your current permissions.",
            ),
            Err(message) => report(&app, message),
        }
        app.state::<HostState>()
            .signing_in
            .store(false, Ordering::SeqCst);
    });
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(HostState::default())
        .setup(|app| {
            let profile = MenuItem::with_id(
                app,
                "profile",
                "Choose platform profile…",
                true,
                None::<&str>,
            )?;
            let signin = MenuItem::with_id(
                app,
                "signin",
                "Sign in to selected platform…",
                true,
                None::<&str>,
            )?;
            let reopen =
                MenuItem::with_id(app, "reopen", "Reopen Studio", true, None::<&str>)?;
            let submenu = Submenu::with_items(
                app,
                "Studio",
                true,
                &[
                    &profile,
                    &signin,
                    &reopen,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::quit(app, Some("Quit Studio"))?,
                ],
            )?;
            let edit = Submenu::with_items(
                app,
                "Edit",
                true,
                &[
                    &PredefinedMenuItem::undo(app, None)?,
                    &PredefinedMenuItem::redo(app, None)?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::cut(app, None)?,
                    &PredefinedMenuItem::copy(app, None)?,
                    &PredefinedMenuItem::paste(app, None)?,
                    &PredefinedMenuItem::select_all(app, None)?,
                ],
            )?;
            let refresh = MenuItem::with_id(
                app,
                "refresh",
                "Reload Studio…",
                true,
                Some("CmdOrCtrl+R"),
            )?;
            let view = Submenu::with_items(app, "View", true, &[&refresh])?;
            app.set_menu(Menu::with_items(app, &[&submenu, &edit, &view])?)?;
            request_launch(app.handle().clone(), None);
            Ok(())
        })
        .on_menu_event(|app, event| match event.id.as_ref() {
            "profile" => restart_studio(app.clone(), true),
            "signin" => sign_in(app.clone()),
            "reopen" => restart_studio(app.clone(), false),
            "refresh" => {
                let app = app.clone();
                std::thread::spawn(move || {
                    if app
                        .dialog()
                        .message("Reloading Studio discards edits you have not exported. Export unsaved workflows first.")
                        .title("Reload Studio")
                        .buttons(tauri_plugin_dialog::MessageDialogButtons::OkCancel)
                        .blocking_show()
                    {
                        let label = app
                            .state::<HostState>()
                            .active
                            .lock()
                            .unwrap()
                            .as_ref()
                            .map(|host| host.label.clone());
                        if let Some(window) = label.and_then(|label| app.get_webview_window(&label))
                        {
                            let _ = window.eval("location.reload()");
                        }
                    }
                });
            }
            _ => {}
        })
        .build(tauri::generate_context!())
        .expect("Could not initialize Studio desktop");
    app.run(|app, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            if let Some(child) = app.state::<HostState>().login.lock().unwrap().take() {
                let _ = child.kill();
            }
            if let Some(host) = app.state::<HostState>().active.lock().unwrap().take() {
                stop_host(host);
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    fn sample(origin: &str) -> Vec<u8> {
        serde_json::to_vec(&serde_json::json!({"type":"weave-desktop-ready","version":1,"origin":origin,"pairing_code":"a23456789012345678901234567890123"})).unwrap()
    }
    #[test]
    fn bootstrap_accepts_only_owned_ipv4_origin_and_no_url_secrets() {
        assert!(validate_bootstrap(&sample("http://127.0.0.1:32199")).is_ok());
        for origin in [
            "https://127.0.0.1:32199",
            "http://localhost:32199",
            "http://evil.example:32199",
            "http://127.0.0.1:32199/path",
            "http://127.0.0.1:32199?code=secret",
            "http://user@127.0.0.1:32199",
            "http://127.0.0.1",
        ] {
            assert!(validate_bootstrap(&sample(origin)).is_err());
        }
        assert!(validate_bootstrap(&vec![b'x'; 1025]).is_err());
    }

    const ORIGIN: &str = "http://127.0.0.1:32199";
    const CODE: &str = "a23456789012345678901234567890123";
    fn url(text: &str) -> Url {
        text.parse().unwrap()
    }
    struct TempFolder(PathBuf);
    impl TempFolder {
        fn new(tag: &str) -> Self {
            let nanos = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let path = std::env::temp_dir().join(format!(
                "weave-downloads-{tag}-{}-{nanos}",
                std::process::id()
            ));
            std::fs::create_dir_all(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for TempFolder {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn downloads_are_accepted_only_from_the_owned_origin() {
        for allowed in [
            "blob:http://127.0.0.1:32199/9b1c3c5e-1f0a-4c55-9a43-1f2d8e2a7f10",
            "http://127.0.0.1:32199/studio/export",
        ] {
            assert!(download_allowed(&url(allowed), ORIGIN), "{allowed}");
        }
        for refused in [
            "blob:http://127.0.0.1:32198/9b1c3c5e",
            "blob:http://localhost:32199/9b1c3c5e",
            "blob:https://127.0.0.1:32199/9b1c3c5e",
            "blob:null/9b1c3c5e",
            "blob:blob:http://127.0.0.1:32199/9b1c3c5e",
            "blob:http://user@127.0.0.1:32199/9b1c3c5e",
            "data:application/json,%7B%7D",
            "file:///etc/passwd",
            "https://evil.example/orders.json",
            "http://127.0.0.1:32198/orders.json",
            "http://user:secret@127.0.0.1:32199/orders.json",
            "about:blank",
        ] {
            assert!(!download_allowed(&url(refused), ORIGIN), "{refused}");
        }
    }

    #[test]
    fn the_window_navigates_only_within_its_own_origin() {
        for allowed in [
            "http://127.0.0.1:32199/",
            "http://127.0.0.1:32199/studio?view=home#top",
        ] {
            assert!(navigation_allowed(&url(allowed), ORIGIN), "{allowed}");
        }
        for refused in [
            "http://127.0.0.1:32198/",
            "http://localhost:32199/",
            "https://127.0.0.1:32199/",
            "http://user@127.0.0.1:32199/",
            "http://user:secret@127.0.0.1:32199/",
            "https://evil.example/",
            "blob:http://127.0.0.1:32198/1d7f",
            "data:text/html,studio",
            "about:blank",
            "file:///etc/passwd",
        ] {
            assert!(!navigation_allowed(&url(refused), ORIGIN), "{refused}");
        }
        // An exported file never replaces Studio on macOS; WebKitGTK must still be allowed to start the download.
        assert_eq!(
            navigation_allowed(&url("blob:http://127.0.0.1:32199/1d7f"), ORIGIN),
            !cfg!(target_os = "macos")
        );
    }

    #[test]
    fn only_a_missing_file_counts_as_a_failed_save_where_failures_stick() {
        let folder = TempFolder::new("finished");
        let saved = folder.0.join("orders.yaml");
        std::fs::write(&saved, "name: orders\n").unwrap();
        let missing = folder.0.join("missing.yaml");
        assert!(!save_failed(true, &missing, true));
        assert!(!save_failed(true, &missing, false));
        assert!(save_failed(false, &missing, true));
        assert!(save_failed(false, &saved, false));
        assert!(!save_failed(false, &saved, true));
        assert!(save_failed(false, &folder.0, true));
        assert_eq!(file_label(&saved), "orders.yaml");
    }

    #[test]
    fn download_names_are_bounded_base_names_with_safe_extensions() {
        for (suggested, expected) in [
            ("orders.yaml", "orders.yaml"),
            ("orders.layout.json", "orders.layout.json"),
            ("orders.yml", "orders.yml"),
            ("Orders.JSON", "Orders.json"),
            ("../../.ssh/authorized_keys", "authorized_keys.txt"),
            ("..\\..\\evil.json", "evil.json"),
            ("_evil_.hidden_name.json", "_evil_.hidden_name.json"),
            (".bashrc", "bashrc.txt"),
            (".json", "json.txt"),
            ("...", "studio-export.txt"),
            ("", "studio-export.txt"),
            ("run.command", "run.command.txt"),
            ("payload.exe", "payload.exe.txt"),
            ("report\u{202E}nosj.exe", "report_nosj.exe.txt"),
            ("a:b|c?.json", "a_b_c_.json"),
            ("line\nbreak.yaml", "line_break.yaml"),
            ("trailing. .json", "trailing.json"),
            ("CON.json", "_CON.json"),
            ("lpt1.yaml", "_lpt1.yaml"),
            ("con.exe", "_con.exe.txt"),
            ("console.json", "console.json"),
        ] {
            assert_eq!(sanitize_download_name(suggested), expected, "{suggested:?}");
        }
        let wide = sanitize_download_name(&format!("{}.json", "é".repeat(200)));
        assert!(wide.len() <= MAX_DOWNLOAD_NAME_BYTES && wide.ends_with("é.json"));
        let plain = sanitize_download_name(&"x".repeat(300));
        assert_eq!(plain.len(), MAX_DOWNLOAD_NAME_BYTES);
        assert!(plain.ends_with("x.txt"));
    }

    #[test]
    fn downloads_never_overwrite_and_stay_in_the_downloads_folder() {
        let folder = TempFolder::new("plan");
        let blob = url("blob:http://127.0.0.1:32199/1d7f");
        let plan = |suggested: &str| {
            plan_download(&blob, ORIGIN, Path::new(suggested), Some(folder.0.clone()))
        };
        let first = plan("/Users/someone/Downloads/orders.layout.json").unwrap();
        assert_eq!(first, folder.0.join("orders.layout.json"));
        std::fs::write(&first, "{}").unwrap();
        let second = plan("orders.layout.json").unwrap();
        assert_eq!(second, folder.0.join("orders.layout (1).json"));
        std::fs::write(&second, "{}").unwrap();
        assert_eq!(
            plan("orders.layout.json").unwrap(),
            folder.0.join("orders.layout (2).json")
        );
        assert_eq!(
            plan("../../escape.json").unwrap(),
            folder.0.join("escape.json")
        );
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink("/nonexistent/weave-target", folder.0.join("link.json"))
                .unwrap();
            assert_eq!(plan("link.json").unwrap(), folder.0.join("link (1).json"));
        }
        assert_eq!(
            plan_download(
                &url("https://evil.example/orders.json"),
                ORIGIN,
                Path::new("orders.json"),
                Some(folder.0.clone())
            ),
            Err(DownloadRefusal::Foreign)
        );
        assert_eq!(
            plan_download(&blob, ORIGIN, Path::new("orders.json"), None),
            Err(DownloadRefusal::NoFolder)
        );
        // A folder that cannot be inspected refuses instead of guessing a free name.
        assert_eq!(
            plan_download(&blob, ORIGIN, Path::new("orders.json"), Some(first)),
            Err(DownloadRefusal::Unavailable("orders.json".into()))
        );
    }

    #[test]
    fn pairing_script_keeps_the_code_out_of_urls_and_bounds_reloads() {
        let script = pairing_script(ORIGIN, CODE);
        assert!(!script.contains("__WEAVE_"));
        assert_eq!(script.matches(CODE).count(), 1);
        // The code lives in one scope-level constant: no page-replaceable encoder, and no callback whose
        // source text would reveal it. studio/tests/desktop-pairing.test.ts runs the script itself.
        let body_line = format!("  const body = \"{{\\\"code\\\":\\\"{CODE}\\\"}}\";\n");
        assert!(script.contains(&body_line), "{script}");
        assert!(script.contains("        body,\n"));
        assert!(!script.contains("code:"));
        assert!(script.contains(&format!("location.origin !== \"{ORIGIN}\"")));
        assert!(script.contains("window.top !== window"));
        assert_eq!(script.matches("request('/studio/session'").count(), 2);
        for navigation in [
            "location.href",
            "location.assign",
            "location.replace",
            "history.",
        ] {
            assert!(!script.contains(navigation), "{navigation}");
        }
        // A refused code never reloads, and automatic re-pairing reloads are bounded.
        assert!(script.contains("if (!r.ok) return;"));
        assert!(script.contains("recent.length >= 3"));
        assert_eq!(script.matches("location.reload()").count(), 1);
    }
}
