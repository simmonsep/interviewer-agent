// Minimal Tauri smoke: proves the three things a browser test cannot prove.
//   1. the packaged window really starts,
//   2. the frontend really renders inside WebView2,
//   3. the page can call a Rust command through Tauri IPC and get a real answer.
//
// It deliberately asserts nothing about the workbench: with no sidecar next to the
// debug binary the startup page is expected to stop at "service not available". That
// is the product's documented first-run behaviour, not a failure of the shell.
import assert from 'node:assert/strict';

const UI_TIMEOUT = 30000;

describe('Tauri desktop shell', () => {
  it('opens the startup page inside the packaged window', async () => {
    await browser.waitUntil(
      async () => (await browser.getTitle()).length > 0,
      {timeout: UI_TIMEOUT, timeoutMsg: 'window title never became readable'},
    );
    const title = await browser.getTitle();
    assert.ok(title.length > 0, 'empty window title');

    const heading = await browser.execute(
      () => document.querySelector('#title')?.textContent ?? '',
    );
    assert.ok(
      heading.trim().length > 0,
      `startup page did not render a heading (title=${title})`,
    );
  });

  it('answers a real Tauri IPC call from the page', async () => {
    const result = await browser.execute(async () => {
      const tauri = globalThis.__TAURI__;
      if (!tauri?.core?.invoke) return {error: 'tauri_global_missing'};
      try {
        return {ok: await tauri.core.invoke('window_state')};
      } catch (error) {
        return {error: String(error)};
      }
    });

    assert.equal(result.error, undefined, `IPC invoke failed: ${JSON.stringify(result)}`);
    assert.ok(result.ok && typeof result.ok === 'object', 'window_state returned no object');
    assert.ok(
      'maximized' in result.ok && 'minimized' in result.ok,
      `unexpected window_state payload: ${JSON.stringify(result.ok)}`,
    );
  });

  it('exits through the app command instead of being killed', async () => {
    const exit = await browser.execute(async () => {
      const tauri = globalThis.__TAURI__;
      if (!tauri?.core?.invoke) return {error: 'tauri_global_missing'};
      try {
        await tauri.core.invoke('quit_app');
        return {ok: true};
      } catch (error) {
        return {error: String(error)};
      }
    });
    assert.equal(exit.error, undefined, `quit_app failed: ${JSON.stringify(exit)}`);

    // The window going away is the observable proof that Rust ran the shutdown path.
    const closed = await browser.waitUntil(
      async () => {
        try {
          await browser.getTitle();
          return false;
        } catch {
          return true;
        }
      },
      {timeout: 20000, timeoutMsg: 'app window still answering after quit_app'},
    ).catch(() => false);
    assert.equal(closed, true, 'app window still answering after quit_app');
  });
});
