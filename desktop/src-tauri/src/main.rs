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
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Mutex,
    },
    time::Duration,
};
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    Manager, WebviewUrl, WebviewWindowBuilder,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
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
    let allowed_origin = origin.origin();
    let script = format!(
        r#"(() => {{
      if (location.origin !== {origin}) return;
      fetch('/studio/session', {{credentials:'same-origin'}}).then(r=>r.json()).then(s=>{{
        if (s.paired) return;
        return fetch('/studio/session', {{method:'POST', credentials:'same-origin',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{code:{code}}})}}).then(r=>{{if(r.ok) location.reload();}});
      }}).catch(()=>{{}});
    }})();"#,
        origin = serde_json::to_string(&bootstrap.origin).unwrap(),
        code = serde_json::to_string(&bootstrap.pairing_code).unwrap()
    );
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
        .on_navigation(move |url| {
            url.origin() == allowed_origin && url.username().is_empty() && url.password().is_none()
        })
        .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
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
                    report(&app, "The local Studio host stopped. Use the Studio menu to reopen your workspace.");
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
fn change_workspace(app: tauri::AppHandle, pick_profile: bool) {
    std::thread::spawn(move || {
        if !app
            .dialog()
            .message(
                "Changing workspace restarts Studio. Export unsaved workflows before continuing.",
            )
            .title("Change Studio workspace")
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
        report(
            &app,
            "Wait for the workspace to finish opening before signing in.",
        );
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
            "Choose a platform profile from the Studio menu before signing in.",
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
                "Signed in. Refresh your Studio workspace to load current permissions.",
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
            let offline =
                MenuItem::with_id(app, "offline", "Open offline authoring", true, None::<&str>)?;
            let submenu = Submenu::with_items(
                app,
                "Studio",
                true,
                &[
                    &profile,
                    &signin,
                    &offline,
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
                "Refresh workspace…",
                true,
                Some("CmdOrCtrl+R"),
            )?;
            let view = Submenu::with_items(app, "View", true, &[&refresh])?;
            app.set_menu(Menu::with_items(app, &[&submenu, &edit, &view])?)?;
            request_launch(app.handle().clone(), None);
            Ok(())
        })
        .on_menu_event(|app, event| match event.id.as_ref() {
            "profile" => change_workspace(app.clone(), true),
            "signin" => sign_in(app.clone()),
            "offline" => change_workspace(app.clone(), false),
            "refresh" => {
                let app = app.clone();
                std::thread::spawn(move || {
                    if app
                        .dialog()
                        .message("Refresh reloads this workspace. Export any unsaved drafts first.")
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
}
