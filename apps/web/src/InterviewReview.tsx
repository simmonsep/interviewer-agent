// F07Z-143: the review surface. Layered exactly as the spec asks:
//   本次结果 (session facts) → 你的作答 (user's own words) → 复盘 (facts + observations) → 下一步
// It renders what `reviewPresentation` derived; it never fetches, never re-generates and never
// scores. The `interview-report` test id stays on the facts block because existing suites read
// the end reason and the scripted note from there.
import {useState} from 'react';
import {observationLabel,reviewStopReason,type ReviewPresentation,type ReviewRetest} from './reviewPresentation.ts';

// F07Z-159/162: the same labels the interviewer used when the question was created.
const sourceLabel:Record<string,string>={role_pack:'角色基础题',builtin_bank:'内置题库',private_bank:'你的题库',project_material:'项目材料',wiki_reused:'面试 Wiki'};

export function InterviewReview({review,busy,desktop,onOpenApplications,onStartAnother,onRefresh,onStartRetest}:{
  review:ReviewPresentation;
  busy:boolean;
  desktop:boolean;
  onOpenApplications?:()=>void;
  onStartAnother:()=>void;
  onRefresh?:()=>void;
  /** F07Z-165 A: start a coaching session whose first question is this suggestion. */
  onStartRetest?:(item:ReviewRetest)=>void;
}){
  const facts=review.facts;
  // F07Z-145 PART E: the full answer is always in the DOM; this only lifts the clamp.
  const [expanded,setExpanded]=useState<Record<string,boolean>>({});
  const toggle=(key:string)=>setExpanded(value=>({...value,[key]:!value[key]}));
  return <section className="d-review" data-testid="interview-review" data-review-state={review.state}
   aria-label="面试复盘">
   <header className="d-review-head">
    <div className="d-review-title">
     {/* Desktop already has 本场复盘 as the pane title; repeating it here reads as two headers. */}
     {!desktop&&<span className="eyebrow">面试复盘</span>}
     <h2>{review.goalTitle??'本次面试复盘'}</h2>
     <p className="d-review-meta" data-testid="review-completion">
      <strong>{review.completion.title}</strong> · {review.completion.detail}
     </p>
    </div>
    <div className="d-review-badges">
     <span className="tag" data-testid="review-status">{review.statusLabel}</span>
     <span className="tag d-review-mode" data-mode={review.mode}>{review.modeLabel}</span>
    </div>
   </header>
   <p className="fine-print d-review-modenote" data-testid="review-mode-note">{review.modeNote}</p>
   {review.state!=='ready'&&<div className={review.state==='failed'?'error':'notice'} role="status"
    data-testid="review-state">
    <strong>{review.statusLabel}</strong>
    <p>{review.state==='failed'
      ?'你的面试作答已经保存，不会丢失。这版还不能重新生成复盘：报告由服务端在本场结束时生成。'
      :'你的作答记录已经保存，报告还没写到这里；可以先看下面的作答原文，稍后点「刷新状态」再回读。'}</p>
   </div>}

   <section className="d-review-block" data-testid="interview-report" aria-label="本次结果">
    <h3>本次结果</h3>
    {facts?<>
     <div className="interview-report-grid">
      <span>题目 <strong>{facts.questions}</strong></span><span>回答 <strong>{facts.answers}</strong></span>
      <span>答题观察 <strong>{facts.observations}</strong></span>
      <span>结束原因 <strong>{reviewStopReason(facts.stopReason)}</strong></span>
     </div>
     {facts.assisted>0&&<p className="fine-print" data-testid="assisted-note">本场有 {facts.assisted} 条回答标记为「已辅导」（陪练中讲过方法的主题）。
      这些不计入实战表现；查看复盘时以原回答为准，不要把讲解当成你的答案。</p>}
     {review.skills.length>0&&<p className="fine-print" data-testid="review-skills">
      本场技能：{review.skills.map(skill=>`${skill.origin==='user_install'?'用户安装':'预装'}「${skill.name}」v${skill.version}`).join(' · ')}。
      技能只影响提问方式，不参与评分。</p>}
     {review.modelSummary&&<p className="interview-summary">{review.modelSummary}</p>}
     {review.mode==='scripted'&&<p className="fine-print">本次是脚本流程演示，观察只用于核对关联与展示，不代表真实模型评价。</p>}
     {facts.unreviewed>0&&<p className="fine-print">有 {facts.unreviewed} 条回答尚未形成观察，本场不作正确性判断。</p>}
     <p className="fine-print">报告不作正式评分，个人经历与能力均未经外部核验；考中反馈由服务端隐藏，仅在终态展示。</p>
    </>:<p className="muted">这场还没有可读的报告。</p>}
   </section>

   <section className="d-review-block" data-testid="review-answers" aria-label="你的作答">
    <h3>你的作答{review.answers.length>0?`（${review.answers.length} 题）`:''}</h3>
    {review.answers.length===0&&<p className="muted">这场没有记录到题目事件。</p>}
    {review.answers.map(item=><article className="d-review-answer" key={item.questionId}>
     <span className="eyebrow">问题 {item.index}</span>
     <p className="d-review-question">{item.question}</p>
     {(item.source||item.strategyId)&&<p className="fine-print d-review-origin"
       data-testid={`review-origin-${item.index}`}>
      {item.source&&<>来源：{sourceLabel[item.source]??item.source}</>}
      {item.strategyId&&<>{item.source?' · ':''}追问策略：{item.strategyTitle??item.strategyId}</>}
     </p>}
     <span className="eyebrow d-review-answer-label">你的回答</span>
     {item.answer!==null
      ?<>
       <p className={`d-review-answer-text${item.fold&&!expanded[item.questionId]?' d-clamp':''}`}>{item.answer}</p>
       {item.fold&&<button type="button" className="text-button d-answer-toggle"
        data-testid={`review-answer-toggle-${item.index}`}
        aria-expanded={expanded[item.questionId]?'true':'false'}
        onClick={()=>toggle(item.questionId)}>{expanded[item.questionId]?'收起回答':'展开完整回答'}</button>}
      </>
      :<p className="muted">这一题没有作答。</p>}
     {item.assisted&&<span className="tag">已辅导</span>}
    </article>)}
   </section>

   {review.observations.length>0&&<section className="d-review-block" data-testid="review-observations"
    aria-label="事实与观察">
    <h3>复盘 · 事实与观察</h3>
    <p className="fine-print">每条观察都引用你的原回答与一条参考来源；这是关联核对，不是能力评分。</p>
    {review.observations.map(observation=><article className="interview-observation" key={observation.id}>
     <span className="eyebrow">答题观察 · {observationLabel[observation.label]??observation.label}</span>
     <p>{observation.summary}</p>
     <blockquote><span className="citation-label">你的回答原文</span><p>{observation.answer_quote}</p></blockquote>
     <blockquote><span className="citation-label">参考来源摘录 · {observation.evidence_id}</span><p>{observation.evidence_quote}</p>
      {observation.evidence_source?.url&&<a href={observation.evidence_source.url} target="_blank" rel="noopener noreferrer">
       {observation.evidence_source.url} ↗</a>}</blockquote>
     {observation.correction&&<p className="fine-print">该观察已撤回：{observation.correction}</p>}
     {(()=>{
      // 收尾 01: the server already resolved this observation to an authored retest prompt.
      // There is deliberately no "start now" button: the interview guard requires the topic to
      // be asked inside the new session first, so the only truthful route is a coaching session,
      // which reuses this goal's unsolved observations.
      const drill=review.retests.find(item=>item.observationId===observation.id);
      if(!drill)return null;
      return <div className="d-review-retest" data-testid={`review-retest-${observation.id}`}>
       <span className="eyebrow">可以练这一题 · {drill.topicTitle}</span>
       <p className="d-review-retest-question">{drill.question}</p>
       <p className="fine-print">来源：本场这条观察（原答见上）· 题库版次 {drill.packVersion}。
        {drill.assisted?'这条回答是在辅导后给出的，不能当作独立能力。':'这是同一主题的新情景，不是能力评分。'}</p>
       <div className="d-review-retest-actions">
        {onStartRetest&&<button type="button" className="secondary" disabled={busy}
         data-testid={`review-start-retest-${observation.id}`}
         onClick={()=>onStartRetest(drill)}>练这一题</button>}
        <a href={drill.referenceUrl} target="_blank" rel="noopener noreferrer">参考来源 ↗</a>
       </div>
       <p className="fine-print">点「练这一题」会开一场定向陪练，首题就是上面的建议题；作答记在新的一场，不改这次记录。已有进行中的场次时会先让你继续原场。</p>
      </div>;
     })()}
    </article>)}
   </section>}

   {review.bankUsage&&<p className="fine-print" data-testid="review-bank-usage">
    {review.bankUsage.bankQuestions>0
     ? `本场有 ${review.bankUsage.bankQuestions} / ${review.bankUsage.totalQuestions} 题来自你选的题库，其余按 JD 与角色基础出题。`
     : `本场没有用到你选的题库：面试官按 JD 与角色基础出题（库里有题不等于必须用）。`}
    {review.bankUsage.declaration==='not_relevant'&&review.bankUsage.consistent===true
      &&' 面试官已声明：这份库与本次 JD 无关。'}
    {review.bankUsage.consistent===false&&'（面试官的声明与实际选题不一致，这里按实际记录显示。）'}
   </p>}
   <footer className="d-review-next" data-testid="review-next">
    <span className="eyebrow">下一步</span>
    <p>{review.next.description}</p>
    <div className={desktop?'d-report-actions':'review-actions'}>
     {desktop&&onOpenApplications&&<button className="primary" data-testid="review-to-applications"
      data-application-entry={review.next.kind}
      disabled={busy} onClick={onOpenApplications}>{review.next.primary}</button>}
     <button className={desktop&&onOpenApplications?'secondary':'secondary'}
      disabled={busy} onClick={onStartAnother}>{review.next.secondary}</button>
     {onRefresh&&<button type="button" className="text-button" disabled={busy} onClick={onRefresh}>刷新状态</button>}
    </div>
   </footer>
  </section>;
}
