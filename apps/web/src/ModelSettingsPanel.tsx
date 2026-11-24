import {useEffect,useState} from 'react';
import {api,ApiError,describeError,post} from './api.ts';

// Settings pane section for the user's own model access. The key is write-only:
// GET returns a mask, PUT with an empty key keeps the saved one, and saving is
// never activation - live mode still requires the external development-mode
// configuration and a Worker restart, so the free scripted default stays put.
interface Preset{id:string;label:string;provider:string;base_url:string;model:string}
// Stable codes from POST /model-settings/test, so a failure says what actually happened.
const probeText:Record<string,string>={model_settings_incomplete:'服务地址、模型名称或 Key 还不完整',
 model_key_rejected:'Key 被服务端拒绝（401），检查是否复制完整、是否属于这个服务',
 model_key_forbidden:'Key 无权访问该模型（403）',model_or_path_not_found:'服务地址或模型名称不对（404）',
 model_rate_limited:'触发限流（429），稍后再试',model_provider_error:'服务端 5xx，问题在提供方',
 model_timeout:'连接超时',model_network_error:'网络不可达（域名、代理或证书）',
 model_invalid_payload:'返回内容不是预期的 JSON'};
type Capability='interview'|'material'|'prep';
interface State{provider:string;base_url:string;model:string;key_masked:string;enabled:boolean;active:boolean;
 assessment_mode:string;key_storage:string;presets:Preset[];vault_available:boolean;
 live_cap_cny:number;live_authorization:string;live_state:string;
 live_spent_cny?:number|null;live_remaining_cny?:number|null;
 material_enabled:boolean;material_active:boolean;material_live_authorization:string;material_live_cap_cny:number;
 material_state:string;material_spent_cny?:number|null;material_remaining_cny?:number|null;
 prep_enabled:boolean;prep_live_authorization:string;prep_live_cap_cny:number;prep_state:string;
 prep_spent_cny?:number|null;prep_remaining_cny?:number|null}

// F07Z-165 (arbitration THIRD_OPTION): state/spent/remaining of one authorization instance.
// `''` means nothing is bound; PAUSED means the row stopped after an uncertain call.
function spendText(state:string|undefined,spent?:number|null,remaining?:number|null){
 if(state!=='ACTIVE'&&state!=='PAUSED')return '尚未读取';
 const used=typeof spent==='number'?`¥${spent.toFixed(3)} 已花`:'已用未读取';
 const left=typeof remaining==='number'?` · 剩余约 ¥${remaining.toFixed(3)}`:'';
 return `${state==='PAUSED'?'已暂停 · ':''}${used}${left}`;
}

export function ModelSettingsSection(){
 const [state,setState]=useState<State|null>(null);
 const [missing,setMissing]=useState(false);
 const [provider,setProvider]=useState(''),[baseUrl,setBaseUrl]=useState(''),[model,setModel]=useState(''),[apiKey,setApiKey]=useState(''),[enabled,setEnabled]=useState(false);
 const [cap,setCap]=useState(1);
 const [materialEnabled,setMaterialEnabled]=useState(false),[materialCap,setMaterialCap]=useState(0.5);
 const [prepEnabled,setPrepEnabled]=useState(false),[prepCap,setPrepCap]=useState(0.2);
 const [busy,setBusy]=useState(false),[message,setMessage]=useState(''),[error,setError]=useState('');
 // One real request, and only when asked. Saving a config still makes no call.
 const [probeBusy,setProbeBusy]=useState(false);
 const [probe,setProbe]=useState<{ok:boolean;code?:string;latency_ms?:number;reply_preview?:string;model?:string}|null>(null);
 const [saved,setSaved]=useState(false),[preset,setPreset]=useState('');
 // A fresh request id is minted only when the user asks for a new authorization (first enable,
 // or "新建授权"). Retrying the same save reuses it, so the server treats it as one instance.
 const [requestIds,setRequestIds]=useState<Record<Capability,string>>({interview:'',material:'',prep:''});
 function applyPreset(id:string){
  setPreset(id);
  const item=state?.presets.find(entry=>entry.id===id);
  if(!item)return;
  setProvider(item.provider);setBaseUrl(item.base_url);setModel(item.model);
  setMessage(item.id==='self-hosted'?'已切到自建网关：请填入自己的服务地址、模型名和 Key。':'已填入常用配置；服务地址与模型名请按提供方实际文档核对，仓库未验证这些端点。');
 }

 useEffect(()=>{
  let alive=true;
  void (async()=>{
   try{
    const value=await api<State>('/api/runtime/model-settings');
    if(!alive)return;
    setState(value);setProvider(value.provider);setBaseUrl(value.base_url);setModel(value.model);setEnabled(value.enabled);setCap(value.live_cap_cny||1);
    setMaterialEnabled(value.material_enabled);setMaterialCap(value.material_live_cap_cny||0.5);
    setPrepEnabled(value.prep_enabled);setPrepCap(value.prep_live_cap_cny||0.2);
   }catch(e){
    if(!alive)return;
    if(e instanceof ApiError&&e.code==='model_config_path_not_set')setMissing(true);
    else setError(describeError(e));
   }
  })();
  return()=>{alive=false;};
 },[]);

 function toggleCapability(kind:Capability,next:boolean){
  const current=kind==='interview'?state?.enabled:kind==='material'?state?.material_enabled:state?.prep_enabled;
  if(kind==='interview')setEnabled(next);else if(kind==='material')setMaterialEnabled(next);else setPrepEnabled(next);
  if(next&&!current)setRequestIds(previous=>({...previous,[kind]:crypto.randomUUID()}));
 }

 function renew(kind:Capability){
  const id=crypto.randomUUID();
  setRequestIds(previous=>({...previous,[kind]:id}));
  void save(undefined,{[kind]:id});
 }

 async function save(formEvent?:React.FormEvent,extra?:Partial<Record<Capability,string>>){
  formEvent?.preventDefault();if(busy)return;setBusy(true);setError('');setMessage('');
  const ids={...requestIds,...extra};
  const configChanged=model!==(state?.model??'')||baseUrl!==(state?.base_url??'');
  const interviewChanged=Boolean(state?.enabled)&&(configChanged||Number(cap)!==Number(state?.live_cap_cny));
  const materialChanged=Boolean(state?.material_enabled)&&(configChanged||Number(materialCap)!==Number(state?.material_live_cap_cny));
  const prepChanged=Boolean(state?.prep_enabled)&&(configChanged||Number(prepCap)!==Number(state?.prep_live_cap_cny));
  const interviewNew=enabled&&(ids.interview!==''||!state?.enabled||interviewChanged);
  const materialNew=materialEnabled&&(ids.material!==''||!state?.material_enabled||materialChanged);
  const prepNew=prepEnabled&&(ids.prep!==''||!state?.prep_enabled||prepChanged);
  const fresh={interview:interviewNew?(ids.interview||crypto.randomUUID()):'',
   material:materialNew?(ids.material||crypto.randomUUID()):'',prep:prepNew?(ids.prep||crypto.randomUUID()):''};
  // Keep the minted ids until the save is confirmed. If the response is lost, a retry reuses
  // the same request id, so the server treats it as one instance instead of minting a second.
  setRequestIds(fresh);
  try{
   const result=await api<{saved:boolean;key_masked:string;active:boolean;key_storage:string;
    material_enabled:boolean;material_active:boolean;material_live_authorization:string;material_live_cap_cny:number;
     prep_enabled:boolean;prep_live_authorization:string;prep_live_cap_cny:number}>('/api/runtime/model-settings',
    {method:'PUT',body:JSON.stringify({provider,base_url:baseUrl,model,api_key:apiKey,enabled,live_cap_cny:cap,
     new_authorization:interviewNew,authorization_request_id:fresh.interview,
     material_enabled:materialEnabled,material_live_cap_cny:materialCap,
     material_new_authorization:materialNew,material_authorization_request_id:fresh.material,
      prep_enabled:prepEnabled,prep_live_cap_cny:prepCap,
      prep_new_authorization:prepNew,prep_authorization_request_id:fresh.prep})});
   setState(previous=>({...previous!,key_masked:result.key_masked,enabled,active:result.active,
    key_storage:result.key_storage,live_cap_cny:cap,material_enabled:materialEnabled,
    material_active:result.material_active,material_live_authorization:result.material_live_authorization,
    material_live_cap_cny:materialCap,prep_enabled:prepEnabled,
    prep_live_authorization:result.prep_live_authorization,prep_live_cap_cny:prepCap}));
   // F07Z-120: a new cap can point at a different authorization row, so the spent/remaining
   // figures must be re-read instead of inheriting the previous ones.
   const refreshed=await api<State>('/api/runtime/model-settings').catch(()=>null);
   if(refreshed)setState(refreshed);
   setRequestIds({interview:'',material:'',prep:''});
   setSaved(true);setApiKey('');
   const parts=[];
   if(enabled)parts.push(result.active?'面试/教练：已启用':'面试/教练：重启后生效');
   if(materialEnabled)parts.push(result.material_active?'材料改写：已启用':'材料改写：重启后生效');
   setMessage(enabled||materialEnabled
    ?`已保存。${parts.join('；')}。费用按实际用量记账，用完即停。`
    :'已保存（未启用）。当前仍走免费脚本模式。');
  }catch(e){
   if(e instanceof ApiError&&e.code==='model_settings_incomplete')setError('启用前需要填写服务地址、模型名称和 API Key。');
   else if(e instanceof ApiError&&e.code==='model_base_url_requires_https')setError('服务地址必须以 https:// 开头。');
   else if(e instanceof ApiError&&e.code==='authorization_requires_new_instance')
    setError('模型名称或授权上限已变化，需要新建授权：请点对应能力旁的「新建授权」，旧花费不会结转。');
   else if(e instanceof ApiError&&e.code==='authorization_request_id_invalid')
    setError('授权请求标识无效，请重新点「新建授权」再保存。');
   else if(e instanceof ApiError&&e.code==='authorization_conflict')
    setError('这笔授权的模型或上限与已有记录不一致，请新建授权，不要复用旧标识。');
   else setError(describeError(e));
  }finally{setBusy(false);}
 }

 async function removeKey(){
  if(busy||!state?.key_masked)return;
  if(!window.confirm('删除本机保存的模型 Key，并关闭真实模型？授权记录会保留用于审计。'))return;
  setBusy(true);setError('');setMessage('');
  try{
   await api('/api/runtime/model-settings',{method:'DELETE'});
   setState(previous=>previous?{...previous,key_masked:'',key_storage:'',enabled:false,active:false,
    live_authorization:'',live_state:'',live_spent_cny:null,live_remaining_cny:null,live_cap_cny:1,
    material_enabled:false,material_active:false,
    material_live_authorization:'',material_state:'',material_spent_cny:null,material_remaining_cny:null,material_live_cap_cny:0.5,
    prep_enabled:false,prep_live_authorization:'',prep_state:'',prep_spent_cny:null,prep_remaining_cny:null,prep_live_cap_cny:0.2}:previous);
   setEnabled(false);setApiKey('');setCap(1);setMaterialEnabled(false);setMaterialCap(0.5);
   setPrepEnabled(false);setPrepCap(0.2);setRequestIds({interview:'',material:'',prep:''});
   setMessage('已删除保存的 Key，面试与材料真实模型均已关闭；重启应用后生效。');
  }catch(e){setError(describeError(e));}finally{setBusy(false);}
 }

 async function testConnection(){  setProbeBusy(true);setProbe(null);setError('');setMessage('');
  try{
   const value=await post<{ok:boolean;code?:string;latency_ms?:number;reply_preview?:string;model?:string}>(
    '/api/runtime/model-settings/test',
    {base_url:baseUrl.trim(),model:model.trim(),api_key:apiKey.trim()},
    crypto.randomUUID());
   setProbe(value);
  }catch(e){
   setProbe({ok:false,code:e instanceof ApiError?e.code:'request_failed'});
  }finally{setProbeBusy(false);}
 }

 if(missing)return <section className="d-set-group">
  <h2>模型接入</h2>
  <p className="hint">此构建未开放模型配置（缺少外部配置文件）。仍可使用免费的脚本评价流程。</p>
 </section>;
 if(!state)return <section className="d-set-group"><h2>模型接入</h2><p className="hint">{error||'正在读取…'}</p></section>;

 return <section className="d-set-group">
  <h2>模型接入</h2>
  <p className="hint">默认走免费脚本模式。下面有<b>三个独立开关</b>：面试/教练、材料改写、规划 Agent（各自有独立的上限与授权）；勾选并保存后，<b>重启应用</b>才会生效。费用由你的 Key 直接结算，应用只按实际用量记账，并受各自上限约束。Key 优先存进 Windows 凭据管理器，model.json 里不留明文；界面只显示掩码。「测试连接」会真的发一次请求（只回两个字），保存本身不会调用模型。</p>
  <form onSubmit={save} className="d-model-form">
   <label className="d-model-preset">快速填写
    <select aria-label="提供方预设" value={preset} onChange={e=>applyPreset(e.target.value)}>
     <option value="">不使用预设</option>
     {state.presets.map(item=><option key={item.id} value={item.id}>{item.label}</option>)}
    </select></label>
   <div className="career-fields">
    <label>提供方<input maxLength={60} value={provider} onChange={e=>setProvider(e.target.value)} placeholder="例如 deepseek / openrouter / 自建网关"/></label>
    <label>服务地址（https）<input maxLength={300} value={baseUrl} onChange={e=>setBaseUrl(e.target.value)} placeholder="https://…"/></label>
   </div>
   <div className="career-fields">
    <label>模型名称<input maxLength={120} value={model} onChange={e=>setModel(e.target.value)} placeholder="例如 deepseek-chat"/></label>
    <label>API Key<input type="password" autoComplete="off" maxLength={400} value={apiKey} onChange={e=>setApiKey(e.target.value)}
     placeholder={state.key_masked?`已保存 ${state.key_masked}，留空则保留`:'尚未保存'}/></label>
   </div>
   <label className="d-check-row"><input type="checkbox" checked={enabled} onChange={e=>toggleCapability('interview',e.target.checked)}/>
    <span>允许面试/教练使用真实模型（按实际用量计费，重启应用后生效）</span></label>
   <div className="career-fields">
    <label>本机授权总额（元，仅这一次面试/教练授权）<input type="number" min={0.2} max={2} step={0.1} value={cap}
     onChange={e=>setCap(Math.min(2,Math.max(0.2,Number(e.target.value)||0.2)))}/></label>
   </div>
   <p className="fine-print">完整模拟（10–12 题、约 45 分钟）建议 ≥¥0.5；两题快练 0.2 起。金额上限是最终闸门，次数用满也会停；同一授权跑两场完整模拟需要点「新建授权」。</p>
   <label className="d-check-row"><input type="checkbox" checked={materialEnabled} onChange={e=>toggleCapability('material',e.target.checked)}/>
    <span>允许材料改写使用真实模型（每份草稿约 ¥0.04，按实际用量记账，重启应用后生效）</span></label>
   <div className="career-fields">
    <label>材料本机授权总额（元，仅材料改写）<input type="number" min={0.2} max={2} step={0.1} value={materialCap}
     onChange={e=>setMaterialCap(Math.min(2,Math.max(0.2,Number(e.target.value)||0.2)))}/></label>
   </div>
   <p className="fine-print">每次调用会先按最坏情况预留约 ¥0.15，实际按用量结算、用不完不收费。
    最低档 ¥0.2 只保证放得下第一次预留：一次长材料改写实测约 ¥0.06–0.07，最低档跑不完；默认 ¥0.5 在钱上够用。
    每次启用或修改上限/模型都会新建一笔授权：旧花费不结转，新上限也不叠加在旧余额上；只改 Key 会沿用当前授权。</p>
    <label className="d-check-row"><input type="checkbox" checked={prepEnabled} data-testid="prep-live-toggle"
     onChange={e=>toggleCapability('prep',e.target.checked)}/>
     <span>允许规划 Agent 使用真实模型（每次准备约 ¥0.004，按实际用量记账，重启应用后生效）</span></label>
    <div className="career-fields">
     <label>规划本机授权总额（元，仅规划）<input type="number" min={0.2} max={2} step={0.1} value={prepCap}
      data-testid="prep-live-cap"
      onChange={e=>setPrepCap(Math.min(2,Math.max(0.2,Number(e.target.value)||0.2)))}/></label>
    </div>
   <div className="d-set-row"><span>面试/教练模式</span>
    <span className="val">{state.active?'真实模型已启用':state.enabled?'已保存，重启后生效':saved?'已保存，未启用':'免费脚本模式'}</span></div>
   {state.enabled&&<div className="d-set-row"><span>面试/教练授权</span>
    <span className="val">{state.live_authorization?`${state.live_authorization} · 上限 ¥${(state.live_cap_cny||cap).toFixed(2)}`:'尚未创建（保存后生成）'}</span></div>}
   {state.enabled&&state.live_authorization&&<div className="d-set-row" data-testid="live-spend">
    <span>面试/教练授权已用</span>
    <span className="val">{spendText(state.live_state,state.live_spent_cny,state.live_remaining_cny)}</span></div>}
   {state.enabled&&state.live_state==='PAUSED'&&<p className="error" role="alert">
    上次调用结果不明，这笔授权已停止：不会继续计费，也不会自动恢复。更新 Key 后点「新建授权」继续。</p>}
   {state.enabled&&<div className="d-set-row"><span>面试/教练授权操作</span>
    <span className="val"><button type="button" data-testid="renew-live" disabled={busy}
     onClick={()=>renew('interview')}>新建授权</button></span></div>}
   <div className="d-set-row"><span>材料改写模式</span>
    <span className="val">{state.material_active?'真实模型已启用':state.material_enabled?'已保存，重启后生效':'免费脚本模式'}</span></div>
   {state.material_enabled&&<div className="d-set-row"><span>材料改写授权</span>
    <span className="val">{state.material_live_authorization?`${state.material_live_authorization} · 上限 ¥${(state.material_live_cap_cny||materialCap).toFixed(2)}`:'尚未创建（保存后生成）'}</span></div>}
   {state.material_enabled&&state.material_live_authorization&&<div className="d-set-row" data-testid="material-spend">
    <span>材料授权已用</span>
    <span className="val">{spendText(state.material_state,state.material_spent_cny,state.material_remaining_cny)}</span></div>}
   {state.material_enabled&&state.material_state==='PAUSED'&&<p className="error" role="alert">
    材料授权已停止（上次结果不明），不会自动恢复；需要时点「新建授权」。</p>}
   {state.material_enabled&&<div className="d-set-row"><span>材料授权操作</span>
    <span className="val"><button type="button" data-testid="renew-material" disabled={busy}
     onClick={()=>renew('material')}>新建授权</button></span></div>}
    <div className="d-set-row" data-testid="prep-mode"><span>规划 Agent 模式</span>
     <span className="val">{state.prep_enabled?'已保存，重启后生效':'免费确定性计划'}</span></div>
    {state.prep_enabled&&<div className="d-set-row" data-testid="prep-authorization"><span>规划授权</span>
     <span className="val">{state.prep_live_authorization?`${state.prep_live_authorization} · 上限 ¥${(state.prep_live_cap_cny||prepCap).toFixed(2)}`:'尚未创建（保存后生成）'}</span></div>}
    {state.prep_enabled&&state.prep_live_authorization&&<div className="d-set-row" data-testid="prep-spend"><span>规划授权已用</span>
     <span className="val">{spendText(state.prep_state,state.prep_spent_cny,state.prep_remaining_cny)}</span></div>}
    {state.prep_enabled&&state.prep_state==='PAUSED'&&<p className="error" role="alert">
     规划授权已停止（上次结果不明），不会自动恢复；需要时点「新建授权」。</p>}
    {state.prep_enabled&&<div className="d-set-row"><span>规划授权操作</span>
     <span className="val"><button type="button" data-testid="renew-prep" disabled={busy}
      onClick={()=>renew('prep')}>新建授权</button></span></div>}
   <div className="d-set-row" data-testid="key-storage"><span>密钥存放位置</span>
    <span className="val">{state.key_storage==='windows_credential_manager'?'Windows 凭据管理器'
     :state.key_storage==='local_file'?'本机配置文件（未启用凭据管理器）':'尚未保存'}</span></div>
   {message&&<p className="notice" role="status">{message}</p>}
   {error&&<p className="error" role="alert">{error}</p>}
   {probe&&<p className={probe.ok?'notice':'error'} role="status" data-testid="model-probe-result">
    {probe.ok?`已连通：${probe.model??model} 用时 ${probe.latency_ms}ms，模型回复「${probe.reply_preview||'空'}」`
     :`未能连通：${probeText[probe.code??'']??probe.code}`}
   </p>}
   <div className="d-form-foot"><span className="fine-print">默认脚本；只有勾选并重启后才会计费，「测试连接」不计入授权上限。</span>
    <span className="d-form-actions">
     <button type="button" data-testid="model-probe" disabled={probeBusy||busy}
      onClick={()=>void testConnection()}>{probeBusy?'正在连接…':'测试连接'}</button>
     <button type="button" data-testid="model-remove-key" disabled={busy||!state.key_masked}
      onClick={()=>void removeKey()}>删除已保存的 Key</button>
     <button type="submit" className="primary" disabled={busy}>{busy?'正在保存…':'保存模型配置'}</button>
    </span></div>
  </form>
 </section>;
}
