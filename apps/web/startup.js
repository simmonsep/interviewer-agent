// Startup surface for the product window. It is the only page that talks to the host,
// and the only thing it can ask for is "prepare the local stack" or "quit".
const APP = 'http://127.0.0.1:3100/?shell=desktop';
const el = (id) => document.getElementById(id);
const title = el('title');
const text = el('text');
const detail = el('detail');
const actions = el('actions');
const dot = el('dot');
const retry = el('retry');
const exit = el('exit');
const invoke = (cmd) => window.__TAURI__.core.invoke(cmd);

function fail(code, message, configDir) {
  title.textContent = '本地服务没有启动';
  text.textContent = message || '请重试；若持续失败，请查看本次运行日志。';
  detail.hidden = false;
  // F07Z-87: say where the service expects its configuration, so "provide api.json" is
  // actionable without hunting for the folder.
  detail.textContent = (configDir ? '配置目录：' + configDir + ' · ' : '') + '诊断码：' + (code || 'unknown');
  actions.hidden = false;
  retry.disabled = false;
  dot.style.background = '#d24545';
}

async function start() {
  retry.disabled = true;
  actions.hidden = true;
  detail.hidden = true;
  title.textContent = '正在启动本地服务';
  text.textContent = '正在检查数据库与后台进程，请稍候。';
  dot.style.background = '';
  try {
    const status = await invoke('prepare_desktop');
    if (status && status.ready === true) {
      title.textContent = '服务已就绪';
      text.textContent = '正在打开工作台…';
      window.location.replace(APP);
      return;
    }
    fail(status && status.code, status && status.message, status && status.config_dir);
  } catch (error) {
    fail('prepare_failed', '本地服务未能启动：' + (error && error.message ? error.message : String(error)));
  }
}

retry.addEventListener('click', start);
exit.addEventListener('click', () => invoke('quit_app').catch(() => window.close()));
start();
