import {useEffect, useRef, useState} from 'react';

declare global {interface Window {__TAURI__: {core: {invoke<T>(command: string): Promise<T>}}}}

/** Developer harness only; product pages continue to use the shared Python API. */
export function DesktopProbe() {
  const [status, setStatus] = useState('等待验证');
  const [receipt, setReceipt] = useState<unknown>(null);
  const [running, setRunning] = useState(false);
  const [ready, setReady] = useState(false);
  const started = useRef(false);
  async function run() {
    if (started.current) return;
    started.current = true;
    setRunning(true);
    setStatus('正在验证认证、免费任务和 Python 服务重启恢复…');
    try {
      const result = await window.__TAURI__.core.invoke('run_probe');
      setReceipt(result);
      setStatus('验证通过：任务已保存，服务重启后原会话可读取');
    } catch (error) {
      setStatus(`验证失败：${String(error)}`);
    } finally {
      setRunning(false);
      await window.__TAURI__.core.invoke('finish_probe');
    }
  }
  useEffect(() => {
    let active = true;
    async function prepare() {
      setStatus('正在检查程序资源与本地服务…');
      try {
        const result = await window.__TAURI__.core.invoke<{ready: boolean; message: string}>('prepare_desktop');
        if (!active) return;
        setReady(result.ready);
        setStatus(result.message);
        if (new URLSearchParams(location.search).has('probe')) {
          if (result.ready) await run();
          else await window.__TAURI__.core.invoke('finish_probe');
        }
      } catch {
        if (active) setStatus('无法完成启动检查，请查看本次运行日志。');
      }
    }
    void prepare();
    return () => {active = false;};
  }, []);
  return <main style={{maxWidth:720,margin:'48px auto',fontFamily:'system-ui',lineHeight:1.7,padding:24}}>
    <h1>求职 Agent · 桌面最小验证</h1>
    <p>这是开发验证窗口，尚非完整求职工作台。使用合成账号与免费脚本任务，依赖本机 PostgreSQL 和 Redis。</p>
    <button disabled={!ready || running || started.current} onClick={() => void run()}>执行一次免费验证</button>
    <p role="status" aria-live="polite">{status}</p>
    {receipt ? <pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{JSON.stringify(receipt,null,2)}</pre> : null}
  </main>;
}
