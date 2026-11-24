import {useCallback,useEffect,useRef,useState} from 'react';
import {ApiError,api,describeError,post} from './api.ts';

// Settings -> 技能. A skill is declarative text only (Agent Skills' text subset): the server
// rejects scripts, binaries and paths outside references/. Installing never grants tools.
type SkillVersion={skill_id:string;version:number;enabled:boolean;revision:number;content_sha256:string;
 name:string;description:string;author:string;body_chars:number;reference_count:number};
type SkillSummary={skill_id:string;active_version:number|null;enabled:boolean;versions:SkillVersion[]};
type Listing={skills:SkillSummary[]};
type Preview={preview_id:string;document:{skill_id:string;name:string;description:string;author:string;
 body:string;references:{path:string;text:string}[];declared_tools:string[]};
 declared_tools_are_metadata_only:boolean;review:string;content_sha256:string};

const skillErrorText:Record<string,string>={
 skill_entry_missing:'这个文件夹里没有根目录的 SKILL.md。请选择一个技能文件夹（里面直接放 SKILL.md）。',
 skill_frontmatter_missing:'SKILL.md 必须以 --- 开头的配置块开始，写明 name 和 description。',
 skill_frontmatter_unterminated:'SKILL.md 的配置块没有结束的 ---。',
 skill_frontmatter_invalid:'SKILL.md 的配置块里有一行不是 key: value。',
 skill_name_invalid:'SKILL.md 的 name 为空或超过 80 字。',
 skill_description_invalid:'SKILL.md 的 description 为空或超过 200 字。',
 skill_id_invalid:'技能需要一个英文 id：在配置块加一行 id: my-skill（小写字母、数字、短横线）。',
 skill_body_missing:'SKILL.md 的配置块之后没有正文。',
 skill_body_too_long:'SKILL.md 正文超过 1200 字，请把细节放进 references/。',
 skill_path_unsafe:'包里有越界路径（绝对路径或 ..），已拒绝。',
 skill_path_not_supported:'包里有 SKILL.md 与 references/ 之外的文件，已拒绝。',
 skill_file_type_not_supported:'只接受 .md / .txt 文本；脚本、二进制或其它类型不兼容。',
 skill_package_too_large:'整包超过 64 KB，请精简正文与参考。',
 skill_reference_limit:'references/ 最多 8 个文件。',
 skill_preview_limit:'待确认的导入过多，请先处理已有条目。',
 idempotency_conflict:'这次导入和之前的内容不一致，请重新选择。',
 resource_unavailable:'这个技能已不可用，或不属于当前账号。',
 skill_version_unknown:'这个版本已不存在，请刷新。',
 skill_single_active_limit:'同一时间只启用一个用户技能，请先停用另一个再启用它。',
};

function explain(error:unknown):string{
 if(error instanceof ApiError&&skillErrorText[error.code])return skillErrorText[error.code];
 return describeError(error);
}

export function SkillsSection(){
 const [listing,setListing]=useState<Listing|null>(null);
 const [preview,setPreview]=useState<Preview|null>(null);
 const [busy,setBusy]=useState(false);
 const [error,setError]=useState(''),[message,setMessage]=useState('');
 const input=useRef<HTMLInputElement|null>(null);

 const load=useCallback(async()=>{
  try{setListing(await api<Listing>('/api/skills'));}catch(e){setError(explain(e));}
 },[]);
 useEffect(()=>{void load();},[load]);
 useEffect(()=>{
  // React has no typed attribute for directory selection; set it on the DOM node instead.
  if(input.current){(input.current as HTMLInputElement&{webkitdirectory:boolean}).webkitdirectory=true;}
 },[]);

 async function choose(event:React.ChangeEvent<HTMLInputElement>){
  const picked=[...(event.target.files??[])];
  event.target.value='';
  if(!picked.length)return;
  setBusy(true);setError('');setMessage('');setPreview(null);
  try{
   // Keep the paths relative to the chosen folder: SKILL.md must be at the root.
   const root=picked[0].webkitRelativePath.split('/')[0]??'';
   const files=[];
   for(const file of picked.slice(0,12))files.push({
    path:file.webkitRelativePath.split('/').slice(1).join('/')||file.name,
    text:await file.text()});
   void root;
   const value=await post<Preview>('/api/skills/imports',{files},crypto.randomUUID());
   setPreview(value);
  }catch(e){setError(explain(e));}
  finally{setBusy(false);}
 }

 async function commit(){
  if(!preview)return;
  setBusy(true);setError('');setMessage('');
  try{
   const value=await post<{skill_id:string;version:number;enabled:boolean;note?:string}>(
    `/api/skills/imports/${preview.preview_id}/commit`,{enabled:true},crypto.randomUUID());
   setMessage(value.enabled
    ? `已安装「${preview.document.name}」v${value.version}。新开一场面试时会用到它，已开的场次仍用原版本。`
    : `已安装「${preview.document.name}」v${value.version}，但暂未启用：同一时间只启用一个用户技能。停用当前技能后再点「启用」。`);
   setPreview(null);await load();
  }catch(e){setError(explain(e));}
  finally{setBusy(false);}
 }

 async function toggle(skill:SkillSummary){
  setBusy(true);setError('');setMessage('');
  try{
   setListing(await api<Listing>(`/api/skills/${encodeURIComponent(skill.skill_id)}`,
    {method:'PATCH',body:JSON.stringify({enabled:!skill.enabled})}));
   setMessage(skill.enabled?'已停用：之后开的面试不再使用它，旧场次不受影响。':'已启用：之后开的面试会使用当前版本。');
  }catch(e){setError(explain(e));}
  finally{setBusy(false);}
 }

 async function uninstall(skill:SkillSummary){
  if(busy)return;
  if(!window.confirm(`卸载「${skill.versions[0]?.name??skill.skill_id}」？已开的场次仍保留其原版本。`))return;
  setBusy(true);setError('');setMessage('');
  try{
   setListing(await api<Listing>(`/api/skills/${encodeURIComponent(skill.skill_id)}`,{method:'DELETE'}));
   setMessage('已卸载。之后开的面试不再使用它。');
  }catch(e){setError(explain(e));}
  finally{setBusy(false);}
 }

 return <section className="d-set-group" data-testid="skills-section">
  <h2>技能（Skill）</h2>
  <p className="hint">技能是面试官的文本指引（例如怎么选题、怎么追问），只影响提问方式。
   只接受 SKILL.md 与 references/ 里的 .md/.txt，不接受脚本、命令或网络操作；安装不会授予任何工具权限。</p>
  <div className="d-set-actions">
   <button type="button" className="secondary" disabled={busy}
    onClick={()=>input.current?.click()} data-testid="skills-choose">选择技能文件夹</button>
   <input ref={input} type="file" multiple hidden onChange={event=>void choose(event)}
    data-testid="skills-input" aria-hidden="true"/>
  </div>

  {preview&&<div className="d-card" data-testid="skills-preview">
   <div className="d-card-head"><h3>{preview.document.name}</h3>
    <span className="tag">待确认</span></div>
   <p className="fine-print">{preview.document.description}</p>
   <div className="d-set-row"><span>技能 ID</span><span className="val d-num d-num-sm">{preview.document.skill_id}</span></div>
   <div className="d-set-row"><span>正文</span><span className="val">{preview.document.body.length} 字</span></div>
   <div className="d-set-row"><span>参考文件</span><span className="val">{preview.document.references.length} 个</span></div>
   {preview.document.declared_tools.length>0&&
    <p className="fine-print" data-testid="skills-declared-tools">
     包内声明了 {preview.document.declared_tools.join('、')}：仅作为元数据识别，不会据此授予工具。</p>}
   <p className="fine-print">内容是用户提供的文本，安装前请自己确认它没有要求越权或编造事实。</p>
   <div className="d-set-actions">
    <button type="button" disabled={busy} onClick={()=>void commit()} data-testid="skills-commit">确认安装</button>
    <button type="button" className="text-button" disabled={busy} onClick={()=>setPreview(null)}>取消</button>
   </div>
  </div>}

  {listing&&listing.skills.length===0&&<p className="fine-print" data-testid="skills-empty">还没有安装技能。面试官会按产品自带规则出题。</p>}
  {listing&&listing.skills.map(skill=>{
   const active=skill.versions.find(item=>item.version===skill.active_version)??skill.versions[0];
   return <div className="d-card" key={skill.skill_id} data-testid={`skill-${skill.skill_id}`}>
    <div className="d-card-head"><h3>{active?.name??skill.skill_id}</h3>
     <span className={`tag ${skill.enabled?'done':'bad'}`}>{skill.enabled?'启用中':'已停用'}</span></div>
    <p className="fine-print">{active?.description}</p>
    <div className="d-set-row"><span>版本</span><span className="val">
     {skill.versions.map(item=><span key={item.version} className={`tag ${item.version===skill.active_version?'done':''}`}>
      v{item.version}{item.version===skill.active_version?' 当前':''}</span>)}</span></div>
    <div className="d-set-row"><span>内容摘要</span><span className="val d-num d-num-sm">{active?.content_sha256.slice(0,12)}</span></div>
    <div className="d-set-actions">
     <button type="button" className="secondary" disabled={busy} onClick={()=>void toggle(skill)}
      data-testid={`skill-toggle-${skill.skill_id}`}>{skill.enabled?'停用':'启用'}</button>
     <button type="button" className="text-button" disabled={busy} onClick={()=>void uninstall(skill)}
      data-testid={`skill-remove-${skill.skill_id}`}>卸载</button>
    </div>
   </div>;
  })}
  {message&&<p className="notice" role="status" data-testid="skills-message">{message}</p>}
  {error&&<p className="error" role="alert" data-testid="skills-error">{error}</p>}
 </section>;
}
