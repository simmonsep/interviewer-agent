// F07Z-138: which entry a window opens is a product decision (the browser default is the full
// workbench, the practice entry lives at `?shell=web`). This suite pins that decision without a
// stack: no API, no Postgres, no Vite, no browser. It runs in a second, so flipping the fallback
// back to `web` - or dropping the `?shell=` parameter - goes red immediately.
import assert from 'node:assert/strict';

const KEY = 'py-shell';
const passed: string[] = [];

function install(options: { search?: string; stored?: string; blocked?: boolean }) {
  const store = new Map<string, string>();
  if (options.stored !== undefined) store.set(KEY, options.stored);
  const blocked = Boolean(options.blocked);
  (globalThis as Record<string, unknown>).window = { location: { search: options.search ?? '' } };
  (globalThis as Record<string, unknown>).sessionStorage = {
    getItem(key: string) {
      if (blocked) throw new Error('storage blocked');
      return store.has(key) ? store.get(key)! : null;
    },
    setItem(key: string, value: string) {
      if (blocked) throw new Error('storage blocked');
      store.set(key, value);
    },
  };
  return store;
}

const { resolveShell } = await import('../src/shell.ts');

// The decision this node exists for: a plain browser tab gets the full workbench.
install({});
assert.equal(resolveShell(), 'desktop', 'browser_default_must_be_desktop');
passed.push('无参数打开 = 完整工作台（默认壳是 desktop，不是练习间）');

// The practice entry has to stay reachable, and it must be remembered for this window.
let store = install({ search: '?shell=web' });
assert.equal(resolveShell(), 'web', 'explicit_web_entry_lost');
assert.equal(store.get(KEY), 'web', 'explicit_web_entry_not_remembered');
passed.push('`?shell=web` 仍能进入练习间，并记住这个窗口的选择');

// A reload or a rewritten URL (`/?goal=…`) must not bounce the practice window back to desktop.
install({ stored: 'web' });
assert.equal(resolveShell(), 'web', 'stored_web_shell_ignored_after_reload');
passed.push('刷新 / URL 被改写后，练习窗口仍停在 web 壳');

// The desktop host still opens `?shell=desktop`, and that wins over a stored choice.
store = install({ search: '?shell=desktop', stored: 'web' });
assert.equal(resolveShell(), 'desktop', 'explicit_desktop_entry_lost');
assert.equal(store.get(KEY), 'desktop', 'explicit_desktop_entry_not_remembered');
passed.push('`?shell=desktop` 优先于本窗口已存的选择（桌面宿主的行为不变）');

// Unknown values must not silently select the practice shell.
install({ search: '?shell=tablet' });
assert.equal(resolveShell(), 'desktop', 'unknown_shell_value_should_fall_back_to_desktop');
passed.push('未知的 `?shell=` 值忽略，回落到完整工作台');

// Storage can be unavailable (private mode / blocked profile). An explicit entry still has to
// win; only the remembered choice disappears.
install({ search: '?shell=web', blocked: true });
assert.equal(resolveShell(), 'web', 'blocked_storage_must_not_discard_explicit_entry');
passed.push('存储被禁用时，显式的 `?shell=web` 依然生效（不会被吞掉）');

install({ blocked: true });
assert.equal(resolveShell(), 'desktop', 'blocked_storage_should_fall_back_to_desktop');
passed.push('存储被禁用且没有参数时，回落到完整工作台');

for (const name of passed) console.log('PASS', name);
console.log(`${passed.length}/${passed.length} passed`);
