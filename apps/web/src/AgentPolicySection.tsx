import {useCallback,useEffect,useState} from 'react';
import {api,post,describeError} from './api.ts';
import type {AgentPolicy} from './AgentConsole.tsx';

// Settings -> Agent autonomy. Mirrors the permission-mode pickers in goose
// (Settings > Chat > Default Mode) and Codex CLI (approval_policy), plus
// opencode's remembered once/always/reject decisions. The server owns the
// actual effect; this section only chooses and displays it.
const toolLabel:Record<string,string>={
 read_goal:'读取目标',read_application:'读取申请',ask_user:'提出澄清',
 prepare_job_research:'岗位研究',prepare_training:'训练准备',
 prepare_materials:'材料准备',prepare_application_action:'申请动作',finish:'结束本轮',
};

export function AgentPolicySection(){
 const [policy,setPolicy]=useState<AgentPolicy|null>(null);
 const [error,setError]=useState(''),[message,setMessage]=useState(''),[busy,setBusy]=useState(false);
 const load=useCallback(async()=>{
  try{
   const value=await api<AgentPolicy>('/api/runtime/agent-policy');
   setPolicy(value);
  }catch(e){setError(describeError(e));}
 },[]);
 useEffect(()=>{
  void load();
  // The settings pane stays mounted while hidden, so a grant recorded on the
  // Agent console must be broadcast explicitly.
  const refresh=()=>void load();
  window.addEventListener('py-agent-policy-changed',refresh);
  return()=>window.removeEventListener('py-agent-policy-changed',refresh);
 },[load]);

 async function choose(mode:string){
  if(busy||!policy)return;
  setBusy(true);setError('');setMessage('');
  try{
   const next=await api<AgentPolicy>('/api/runtime/agent-policy',
    {method:'PUT',body:JSON.stringify({mode})});
   setPolicy(next);setMessage('已保存。Agent 工作台的提议卡会立刻按新策略显示。');
   window.dispatchEvent(new CustomEvent('py-agent-policy-changed'));
  }catch(e){setError(describeError(e));}
  finally{setBusy(false);}
 }
 async function forget(tool:string){
  if(busy)return;
  setBusy(true);setError('');setMessage('');
  try{
   const next=await post<AgentPolicy>('/api/runtime/agent-policy/decision',{tool,decision:'once'});
   setPolicy(next);setMessage(`已取消「${toolLabel[tool]??tool}」的长期授权，下次会重新询问。`);
   window.dispatchEvent(new CustomEvent('py-agent-policy-changed'));
  }catch(e){setError(describeError(e));}
  finally{setBusy(false);}
 }

 const grants=Object.entries(policy?.grants??{});
 return <section className="d-set-group" data-testid="agent-policy-section">
  <h2>Agent 自治</h2>
  <p className="hint">决定 Agent 的提议要不要先问你。参考 goose / Codex / opencode 的权限模式：只在这里选一次，不需要逐个功能面板点开；长期授权可以逐条撤销。</p>
  {!policy&&<p className="hint">{error||'正在读取本机策略…'}</p>}
  {policy&&<div className="d-policy-modes" role="group" aria-label="默认自治模式">
   {policy.modes.map(mode=><button key={mode.id} type="button" className="d-policy-mode"
     data-testid={`policy-${mode.id}`} data-active={policy.mode===mode.id}
     aria-pressed={policy.mode===mode.id} disabled={busy} onClick={()=>void choose(mode.id)}>
     <span className="d-policy-mode-head"><strong>{mode.label}</strong>
      {policy.mode===mode.id&&<span className="d-policy-current">当前</span>}</span>
     <span className="fine-print">{mode.description}</span></button>)}
  </div>}
  {policy&&<div className="d-policy-grants">
   <div className="d-set-row"><span>已记住的授权</span><span className="val">{grants.length} 条</span></div>
   {grants.length===0&&<p className="fine-print">还没有长期授权。提议卡上选「这类都自动」或「拒绝」后会记在这里。</p>}
   {grants.map(([tool,grant])=><div className="d-policy-grant" key={tool}>
    <span>{toolLabel[tool]??tool}</span><span className="tag">{grant==='always'?'自动执行':'不再执行'}</span>
    <button type="button" className="text-button" disabled={busy} onClick={()=>void forget(tool)}>撤销</button>
   </div>)}
  </div>}
  {policy&&<p className="fine-print">{policy.note}</p>}
  {message&&<p className="notice" role="status">{message}</p>}
  {error&&<p className="error" role="alert">{error}</p>}
 </section>;
}
