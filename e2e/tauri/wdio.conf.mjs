// Desktop E2E for the Tauri shell, driven by WebdriverIO + @wdio/tauri-service.
//
// Why this file exists: apps/web/tests/*.mts drive the real WebView2 window over the
// DevTools protocol, which is excellent for renderer assertions but is not a WebDriver
// session: it cannot exercise the Tauri command boundary the way a user's click does.
// This config adds the missing layer (React -> Tauri IPC -> Rust command -> window).
//
// Provider choice: 'official' (cargo `tauri-driver` + msedgedriver) is used on purpose.
// The 'embedded' provider would require adding tauri-plugin-wdio-webdriver to
// apps/desktop/src-tauri, i.e. an in-app WebDriver server inside the shipping binary.
// The project's security rules keep the release binary free of new attack surface, so
// the external driver is the default here. Switching providers is a deliberate,
// documented decision, not a config tweak.
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '../..');

const appBinary = process.env.TAURI_APP_BINARY
  ?? resolve(repo, 'apps/desktop/src-tauri/target/debug/interview-desktop-probe.exe');
const tauriDriver = process.env.TAURI_DRIVER_PATH
  ?? 'E:\\codex\\toolchains\\cargo\\bin\\tauri-driver.exe';
const edgeDriverDir = process.env.MSEDGEDRIVER_DIR ?? 'E:\\codex\\toolchains\\msedgedriver';

// tauri-driver looks for msedgedriver.exe on PATH. Keep it out of the repo: the driver
// version must match the installed WebView2 runtime, so it is a machine tool, not source.
process.env.PATH = `${edgeDriverDir};${process.env.PATH ?? ''}`;

export const config = {
  runner: 'local',
  specs: [resolve(here, 'smoke.e2e.mjs')],
  maxInstances: 1,
  maxInstancesPerCapability: 1,
  capabilities: [{
    browserName: 'tauri',
    'tauri:options': {application: appBinary},
  }],
  services: [['tauri', {
    appBinaryPath: appBinary,
    driverProvider: 'official',
    tauriDriverPath: tauriDriver,
    // msedgedriver is pinned in E:\codex\toolchains\msedgedriver (matching the installed
    // WebView2 runtime) and placed on PATH by the config below. The service's own copy step
    // is enabled only as a fallback: its version probe does not recognise the current
    // msedgedriver banner ("Microsoft Edge WebDriver x.y.z"), so it reports "unknown".
    autoInstallTauriDriver: false,
    autoDownloadEdgeDriver: true,
    logLevel: 'info',
    startTimeout: 60000,
    commandTimeout: 60000,
  }]],
  framework: 'mocha',
  reporters: ['spec'],
  mochaOpts: {ui: 'bdd', timeout: 120000},
  logLevel: 'warn',
  waitforTimeout: 20000,
  connectionRetryTimeout: 90000,
  connectionRetryCount: 2,
};
