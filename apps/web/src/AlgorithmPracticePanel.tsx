// F07Z-57/58: algorithm practice stays outside this product. The app opens a configured
// practice site in its own window (desktop build) or in the isolated browser profile the
// tracking panel uses (browser preview), the user logs in by hand once, and records what
// they did afterwards. No judging, no scraping, no credentials.
import {useCallback,useEffect,useRef,useState} from 'react';
import {api,post,ApiError,describeError} from './api.ts';

interface SiteUrl{label:string;url:string}
interface Site{id:string;label:string;blurb?:string;urls:SiteUrl[]}
interface Sites{items:Site[];note:string;outcomes:string[];checked_on?:string}
interface RecordItem{id:string;site_id:string;site_label:string;url:string;title:string;
 practiced_on:string;outcome:string;note:string;statement?:string;
 explanation?:{text:string;origin:string;created_at:string}|null}

const outcomeText:Record<string,string>={SOLVED:'做出来了',PARTIAL:'做出一部分',
 UNSOLVED:'没做出来',REVIEWED:'只复盘了思路'};

type TauriBridge={core?:{invoke?:(command:string,args?:Record<string,unknown>)=>Promise<unknown>}};

// Inside the desktop app the site opens in its own app window (no browser chrome, no
// permissions); in a plain browser preview it falls back to the local API, which launches
// the user's own browser profile.
function practiceWindow(){return (window as unknown as {__TAURI__?:TauriBridge}).__TAURI__;}

// Tauri rejects `invoke` with the plain string returned by the Rust `Err`, which is not an
// Error instance; the API path rejects with ApiError. Both shapes are read here so the
// notice under the form says what actually happened instead of "request failed".
function messageOf(error:unknown){
 if(error instanceof ApiError)return `${error.code} ${error.serverMessage??''}`;
 if(typeof error==='string')return error;
 if(error instanceof Error)return error.message;
 return '';
}

export function AlgorithmPracticePanel({goalId,onAuthLost}:{goalId:string;onAuthLost:()=>void}){
 const [sites,setSites]=useState<Site[]>([]),[note,setNote]=useState('');
 const [checkedOn,setCheckedOn]=useState('');
 const [records,setRecords]=useState<RecordItem[]>([]);
 const [site,setSite]=useState(''),[url,setUrl]=useState('');
 const [title,setTitle]=useState(''),[outcome,setOutcome]=useState('SOLVED'),[day,setDay]=useState('');
 const [memo,setMemo]=useState('');
 const [statement,setStatement]=useState('');
 const [error,setError]=useState(''),[notice,setNotice]=useState('');
 const [busy,setBusy]=useState(false);
 const alive=useRef(true),lock=useRef(false);

 const load=useCallback(async()=>{
  try{
   const [config,list]=await Promise.all([
    api<Sites>('/api/desktop/practice-sites'),
    api<{items:RecordItem[]}>(`/api/career-goals/${goalId}/practice-records`),
   ]);
   if(!alive.current)return;
   setSites(config.items);setNote(config.note);setRecords(list.items);
   setCheckedOn(config.checked_on??'');
   setSite(current=>current||config.items[0]?.id||'');
   setUrl(current=>current||config.items[0]?.urls[0]?.url||'');
   setDay(current=>current||new Date().toISOString().slice(0,10));
  }catch(e){if(alive.current){if(e instanceof ApiError&&e.status===401)onAuthLost();else setError(describeError(e));}}
 },[goalId,onAuthLost]);

 useEffect(()=>{alive.current=true;void load();return()=>{alive.current=false;};},[load]);

 async function run(action:()=>Promise<void>){
  if(lock.current)return;lock.current=true;setBusy(true);setError('');setNotice('');
  try{await action();}catch(e){
   if(e instanceof ApiError&&e.status===401){onAuthLost();return;}
   const text=messageOf(e);
   if(text.includes('practice_host_not_allowed')){setError('这个站点不在允许清单里（应用内窗口只打开已核对的练习站点）。');return;}
   if(text.includes('practice_https_required')){setError('只允许打开 https 站点。');return;}
   if(text.includes('practice_url_invalid')){setError('地址格式不对。');return;}
   if(e instanceof ApiError&&e.code==='practice_url_not_allowed'){setError('只能打开配置里列出的地址。');return;}
   if(e instanceof ApiError&&e.code==='practice_date_in_future'){setError('练习日期不能晚于今天。');return;}
   if(e instanceof ApiError&&e.code==='practice_browser_timeout'){setError('浏览器启动超时：请确认本机 Edge/Chrome 可用，或先关掉占用该独立配置目录的旧窗口。');return;}
   setError(describeError(e));
  }finally{lock.current=false;if(alive.current)setBusy(false);}
 }

 const openOne=(siteId:string,link:string)=>run(async()=>{
  if(!siteId||!link)return;
  const bridge=practiceWindow();
  if(bridge?.core?.invoke){
   await bridge.core.invoke('open_practice_window',{url:link});
   if(!alive.current)return;
   setNotice('已在应用内窗口打开。首次需要在那个窗口里手动登录一次；登录态保存在本机应用数据目录。');
   return;
  }
  await post('/api/desktop/practice-sites/open',{site_id:siteId,url:link},crypto.randomUUID());
  if(!alive.current)return;
  setNotice('已用本机独立浏览器窗口打开。首次需要你在窗口里手动登录一次；登录态只留在本机配置目录。');
 });

 const openSite=()=>openOne(site,url);

 const save=()=>run(async()=>{
  if(!site||!url||!title.trim()){setError('先选站点和地址，再写题目名。');return;}
  await post(`/api/career-goals/${goalId}/practice-records`,
   {request_id:crypto.randomUUID(),site_id:site,url,title:title.trim(),practiced_on:day,outcome,note:memo,
    statement:statement.trim()},crypto.randomUUID());
  if(!alive.current)return;
  setTitle('');setMemo('');setStatement('');setNotice('这一次练习已记录。');
  await load();
 });

 const remove=(id:string)=>run(async()=>{
  await api(`/api/career-goals/${goalId}/practice-records/${id}`,{method:'DELETE'});
  if(!alive.current)return;
  setNotice('这条记录已删除。');
  await load();
 });

 const current=sites.find(item=>item.id===site);

 return <section className="card d-card" aria-label="算法练习">
  <div className="d-card-head"><h2>算法练习 · 外部站点</h2><span className="tag">本机判题走站点</span></div>
  <p className="muted">算法题交给外部站点判题：应用只负责打开你已登录的练习网站，并把成绩手动记到这里。
   不读题库、不判题、不保存账号密码；题目可以自己粘进来，面试训练才谈得到具体条件。</p>
  {note&&<p className="fine-print">{note}</p>}
  {checkedOn&&<p className="fine-print">站点地址核对日期：{checkedOn}（本机脚本实测；真实打开仍需你在窗口里登录）。</p>}
  {error&&<p role="alert" className="error">{error}</p>}
  {notice&&<p className="notice" role="status">{notice}</p>}

  <div className="career-fields">
   <label>站点<select aria-label="算法练习站点" value={site} onChange={e=>{
    const next=e.target.value;setSite(next);
    setUrl(sites.find(item=>item.id===next)?.urls[0]?.url??'');
   }}>
    {sites.map(item=><option key={item.id} value={item.id}>{item.label}</option>)}
   </select></label>
   <label>地址<select aria-label="算法练习地址" value={url} onChange={e=>setUrl(e.target.value)}>
    {(current?.urls??[]).map(item=><option key={item.url} value={item.url}>{item.label}</option>)}
   </select></label>
  </div>
  <div className="d-practice-sites" role="list" aria-label="可用站点">
   {sites.map(item=><button key={item.id} type="button" role="listitem" className="d-practice-site"
    disabled={busy} title={`${item.label}：${item.blurb??''}`}
    onClick={()=>{setSite(item.id);setUrl(item.urls[0]?.url??'');void openOne(item.id,item.urls[0]?.url??'');}}>
    <strong>{item.label}</strong><small>{item.blurb??item.urls[0]?.label??''}</small>
   </button>)}
  </div>
  <div className="d-form-foot"><span className="fine-print">打开后可自行关闭；应用不接管你的登录操作。</span>
   <button type="button" className="secondary" disabled={busy||!url} onClick={()=>void openSite()}>打开站点</button></div>

  <div className="d-practice-record">
   <div className="career-fields">
    <label>题目<input aria-label="练习题名" value={title} maxLength={120} placeholder="例如：两数之和"
     onChange={e=>setTitle(e.target.value)}/></label>
    <label>日期<input aria-label="练习日期" type="date" value={day} onChange={e=>setDay(e.target.value)}/></label>
    <label>结果<select aria-label="练习结果" value={outcome} onChange={e=>setOutcome(e.target.value)}>
     {Object.entries(outcomeText).map(([value,text])=><option key={value} value={value}>{text}</option>)}
    </select></label>
   </div>
   <label className="d-practice-note">复盘<textarea aria-label="练习复盘" value={memo} maxLength={1000}
    placeholder="错在哪、下次怎么改；不写也没关系。" onChange={e=>setMemo(e.target.value)}/></label>
   <label className="d-practice-note">题面（可选）<textarea aria-label="题目描述" value={statement} maxLength={4000}
    placeholder="把题目描述粘贴进来。应用不读站点页面，不贴的话 Agent 只知道题目名和你的复盘。"
    onChange={e=>setStatement(e.target.value)}/></label>
   <p className="fine-print">粘贴的题面会随讲解请求发送给你在本机配置的模型服务（离设备）；不想发送就不要粘贴。</p>
   <div className="d-form-foot"><span className="fine-print">记录只保存在本机目标下，可随时删除。</span>
    <button type="button" className="primary" disabled={busy||!title.trim()} onClick={()=>void save()}>记录这一次</button></div>
  </div>

  <div className="d-practice-list">
   <div className="d-card-head"><h3>最近记录</h3><span className="n d-num d-num-sm">{records.length}</span></div>
   {records.length===0?<p className="fine-print">还没有记录。先在站点里做一题，再回来记一行。</p>:
    <div className="d-session-list">{records.map(item=><div key={item.id} className="d-session-item d-practice-item" role="listitem">
     <div className="d-practice-row">
      <span><strong>{item.title}</strong>
       <small>{item.site_label} · {item.practiced_on} · {outcomeText[item.outcome]??item.outcome}{item.statement?' · 已附题面':''}</small></span>
      {item.note&&<small className="d-practice-memo">{item.note}</small>}
      <button type="button" className="text-button" disabled={busy}
       onClick={()=>void remove(item.id)}>删除</button>
     </div>
     {item.explanation&&<details className="d-practice-explanation">
      <summary>上次讲解 · {item.explanation.created_at.slice(0,10)}（{item.explanation.origin==='real_model'
       ?'模型输出，不是标准答案':'脚本流程演示，不含讲解内容'}）</summary>
      <p>{item.explanation.text}</p>
     </details>}
    </div>)}</div>}
  </div>
 </section>;
}
