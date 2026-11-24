// F07Z-151: the full brief for the selected position. It renders only what `getPositionBrief`
// derived from the stored snapshot; every requirement keeps its JD quote visible.
import type {PositionBrief} from './positionBrief.ts';

export function PositionBriefCard({brief}: {brief: PositionBrief}) {
  return <section className="d-brief" data-testid="position-brief" aria-label="岗位简报">
    <div className="d-brief-head">
      <div>
        <span className="eyebrow">岗位简报</span>
        <h3>{brief.title}</h3>
      </div>
      <span className="tag">来自岗位原文</span>
    </div>

    <div className="d-brief-facts">
      {brief.facts.map(fact => <span key={fact.label}><b>{fact.label}</b>{fact.value}</span>)}
    </div>

    <div className="d-brief-section" data-testid="brief-requirements">
      <h4>岗位要求</h4>
      {brief.mustHave.length === 0 && brief.niceToHave.length === 0 &&
        <p className="muted">原文没有可分类的必须项 / 加分项；请直接看下方 JD 原文。</p>}
      {brief.mustHave.length > 0 && <>
        <span className="eyebrow">必须项（你确认过的硬条件）</span>
        <ul>{brief.mustHave.map((line, index) => <li key={`must-${index}`}>
          {line.value}{line.quote && <q>{line.quote}</q>}</li>)}</ul>
      </>}
      {brief.niceToHave.length > 0 && <>
        <span className="eyebrow">原文提到、未声明为必须</span>
        <ul>{brief.niceToHave.map((line, index) => <li key={`nice-${index}`}>
          {line.value}{line.quote && <q>{line.quote}</q>}</li>)}</ul>
        <p className="fine-print">是否属于「必须」由你在目标条件里声明；系统不替你判定。</p>
      </>}
    </div>

    <div className="d-brief-section" data-testid="brief-skills">
      <h4>JD 中出现的技能词</h4>
      {brief.skills.length === 0
        ? <p className="muted">没有匹配到已知技能词；这不代表岗位没有技术要求。</p>
        : <div className="d-brief-chips">
          {brief.skills.map(hit => <span className="tag" key={hit.name} title={hit.quote}>{hit.name}</span>)}
        </div>}
      <p className="fine-print">只做原文词面匹配，不判断熟练度，也不代表你的能力。</p>
    </div>

    <div className="d-brief-section" data-testid="brief-unknowns">
      <h4>待确认</h4>
      <ul>{brief.unknowns.map((line, index) => <li key={`unknown-${index}`}>{line}</li>)}</ul>
    </div>

    <div className="d-brief-section" data-testid="brief-sources">
      <h4>来源与版本</h4>
      <dl>{brief.sources.map(source => <div key={source.label}>
        <dt>{source.label}</dt><dd>{source.value}</dd></div>)}</dl>
      {(brief.responsibilities || brief.requirements) && <div className="d-brief-split">
        {brief.responsibilities && <><span className="eyebrow">职责原文</span><p>{brief.responsibilities}</p></>}
        {brief.requirements && <><span className="eyebrow">要求原文</span><p>{brief.requirements}</p></>}
      </div>}
      <details className="d-brief-raw" data-testid="brief-raw-jd">
        <summary>JD 原文</summary>
        <p>{brief.rawJd}</p>
      </details>
      {brief.warnings.length > 0 && <p className="fine-print">原文提示：{brief.warnings.join('；')}</p>}
    </div>
  </section>;
}
