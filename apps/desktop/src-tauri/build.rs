fn main() {
    // Declaring the app's own commands makes them available to the webview.
    // Once a capability file exists it replaces the default "every command is
    // allowed", so all five are listed here: the four the startup and probe
    // pages already use, plus `window_control`, which the undecorated window's
    // top bar calls to move, minimise, maximise and close this app's window.
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "prepare_desktop",
                "quit_app",
                "run_probe",
                "finish_probe",
                "window_control",
                "window_state",
                "open_practice_window",
                // F07Z-86: the learning list opens links the user added, in the default
                // browser. There is no host allowlist here by design; see open_external.
                "open_external",
                // F07Z-88: the settings page reports where the local data lives and opens
                // one of those two folders. It takes a name, never a path.
                "local_data",
                "open_data_folder",
                // F07Z-83: the local identity's password lives in the Windows Credential
                // Manager, so the page needs exactly these three - no key argument, one
                // fixed target name inside the host.
                "local_identity_secret_set",
                "local_identity_secret_get",
                "local_identity_secret_clear",
            ]),
        ),
    )
    .unwrap();
}
