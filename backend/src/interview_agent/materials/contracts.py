"""Separate source-bound declarations from editable, source-linked resume rewrites."""
import hashlib
import html
import re
from typing import Annotated, Literal
from pydantic import Field, field_validator
from interview_agent.contracts.common import Contract, PositiveInt
from interview_agent.contracts.practice import UuidString
from .alignment import ClarificationAnswer

Revision = Annotated[int, Field(ge=0, le=30)]
Kind = Literal['education','experience','project','contribution','result','skill']
EDUCATION_MARKERS = ('教育', '学历', '本科', '硕士', '博士', '大专', '大学', '学院', 'education')
WORK_MARKERS = ('工作经历', '实习经历', '任职', '工作单位', 'experience')
PROJECT_MARKERS = ('项目经历', '项目经验', '个人项目', 'projects')
SKILL_MARKERS = ('技能', '技术栈', 'skills')

LABELS = {'education':'教育经历','experience':'实习与经历','project':'项目','contribution':'个人贡献','result':'结果与验证','skill':'技能声明'}

# A source segment address: the model returns these instead of retyping structured lines.
SegmentId = Annotated[str, Field(pattern=r'^S\d{3}$')]


class Versioned(Contract):
    expected_version: PositiveInt
    expected_revision: Revision


class SourceInput(Versioned):
    name: Annotated[str, Field(min_length=1,max_length=80)]
    format: Literal['text','markdown']
    # F07Z-117: detailed experience is normal; 20,000 characters is a DoS ceiling, not a
    # product limit. The agent condenses long material while rewriting.
    text: Annotated[str, Field(min_length=1,max_length=20000)]

    @field_validator('text')
    @classmethod
    def readable(cls,value):
        value=value.replace('\r\n','\n').replace('\r','\n')
        if not value.strip() or any(ord(c)<32 and c not in '\n\t' for c in value) or '\ufffd' in value:
            raise ValueError('readable_utf8_text_required')
        return value

    @field_validator('name')
    @classmethod
    def supported_name(cls,value):
        if not value.strip() or value.lower().endswith(('.pdf','.docx','.doc')):raise ValueError('unsupported_material_format')
        return value.strip()


class FilePreviewInput(Versioned):
    filename: Annotated[str, Field(min_length=1,max_length=80)]

    @field_validator('filename')
    @classmethod
    def file_name(cls,value):
        if any(c in value for c in '/\\') or any(ord(c)<32 for c in value) or not value.lower().endswith(('.pdf','.docx')):
            raise ValueError('unsupported_material_format')
        return value


class RunInput(Versioned):
    mode: Literal['analyze','draft','align'] = 'analyze'
    draft_style: Literal['source','rewrite'] = 'source'
    source_ids: Annotated[list[UuidString],Field(max_length=2)] = []
    fact_ids: Annotated[list[UuidString],Field(max_length=8)] = []

    @field_validator('source_ids','fact_ids')
    @classmethod
    def distinct(cls,v):
        if len(v)!=len(set(v)):raise ValueError('duplicate_source')
        return v


class ResumeImportInput(Versioned):
    """Import the uploaded text as a resume version; no model call."""
    source_ids: Annotated[list[UuidString], Field(max_length=2)] = []


class Confirmation(Versioned):
    continue_after_confirmation: bool = False
    run_id: UuidString
    proposal_ids: Annotated[list[Annotated[int,Field(ge=0,le=11)]],Field(min_length=1,max_length=8)]

    @field_validator('proposal_ids')
    @classmethod
    def distinct(cls,v):
        if len(v)!=len(set(v)):raise ValueError('duplicate_proposal')
        return v


class ClarificationInput(Versioned):
    answers:Annotated[list[ClarificationAnswer],Field(min_length=1,max_length=6)]

    @field_validator('answers')
    @classmethod
    def unique(cls,value):
        if len({a.requirement_index for a in value})!=len(value):raise ValueError('duplicate_clarification_answer')
        return value


class FactProposal(Contract):
    source_id: UuidString
    kind: Kind
    quote: Annotated[str,Field(min_length=1,max_length=300)]


class AnalysisArgs(Contract):
    facts: Annotated[list[FactProposal],Field(min_length=1,max_length=8)]
    question: Annotated[str,Field(max_length=300)] = ''


class DraftArgs(Contract):
    fact_ids: Annotated[list[UuidString],Field(min_length=1,max_length=8)]


class RewriteItem(Contract):
    text: Annotated[str,Field(min_length=1,max_length=500,description='可直接用于简历的项目条目；候选方案、待完善说明、进度计划和原文未明确的技术分工放到suggestions/change_note。')]
    source_ids: Annotated[list[UuidString],Field(min_length=1,max_length=2)]
    change_note: Annotated[str,Field(min_length=1,max_length=200)]
    # Provenance, not proof: a cited segment says where the bullet came from, it does not
    # prove the rewrite is faithful to it. Semantic grounding stays with C1/C2 (F07Z-36).
    evidence_segments: Annotated[list[SegmentId],Field(max_length=6)] = []

    @field_validator('text','change_note')
    @classmethod
    def nonblank(cls,v):
        if not v.strip() or any(ord(c)<32 and c not in '\n\t' for c in v):raise ValueError('readable_rewrite_required')
        return v


OmissionReason = Literal['duplicate','merged_elsewhere','low_relevance','structural_noise',
                         'user_excluded','other']


class OmittedSegment(Contract):
    """A narrative segment the model decided not to write. The reason is a closed enum so
    "why is this line gone" is answerable without reading prose (F07Z-36)."""

    segment_id: SegmentId
    reason: OmissionReason
    note: Annotated[str,Field(max_length=80)] = ''


class RewriteArgs(Contract):
    # A batch may carry only structured lines, so emptiness is checked once the
    # whole document is merged (`validate_result` requires >=1 item overall).
    items: Annotated[list[RewriteItem],Field(max_length=8)]
    # The model no longer retypes structured content: it returns source segment IDs
    # (`S001`), and `materials.segments.resolve_sections` copies the original line. Prompt
    # wording alone could not guarantee fidelity (F07Z-33), so the contract carries it.
    # Optional per batch: a batch may only touch one section. `validate_result` still
    # refuses an empty section once the batches are merged and the source states it.
    education: Annotated[list[SegmentId],Field(max_length=8)] = []
    experience: Annotated[list[SegmentId],Field(max_length=8)] = []
    projects: Annotated[list[SegmentId],Field(max_length=8)] = []
    skills: Annotated[list[SegmentId],Field(max_length=8)] = []
    suggestions: Annotated[list[Annotated[str,Field(min_length=1,max_length=300)]],Field(max_length=4,description='正文之外的技术方案、补充信息和待完善事项，供用户选用，不自动写成经历。')] = []
    omitted_segments: Annotated[list[OmittedSegment],Field(max_length=120)] = []

    @field_validator('education','experience','projects','skills')
    @classmethod
    def distinct_segments(cls,v):
        if len(v)!=len(set(v)):raise ValueError('duplicate_segment')
        return v

    @field_validator('omitted_segments')
    @classmethod
    def distinct_omissions(cls,v):
        if len(v)!=len({entry.segment_id for entry in v}):raise ValueError('duplicate_omission')
        return v


class DraftRevision(Versioned):
    expected_draft_version: PositiveInt
    action: Literal['edit','accept','restore']
    items: Annotated[list[RewriteItem],Field(max_length=48)] = []
    selected_suggestion_indexes: Annotated[list[Annotated[int,Field(ge=0,le=3)]],Field(max_length=4)] = []
    # Edit-only and optional: `None` means "keep the parent draft's line". The panel
    # sends all four so the owner can correct a line the model transcribed wrongly;
    # the same validate_result guards decide whether the result is still renderable.
    basics: Annotated[list[Annotated[str,Field(min_length=1,max_length=300)]],Field(max_length=6)]|None = None
    education: Annotated[list[Annotated[str,Field(min_length=1,max_length=300)]],Field(max_length=64)]|None = None
    experience: Annotated[list[Annotated[str,Field(min_length=1,max_length=300)]],Field(max_length=64)]|None = None
    projects: Annotated[list[Annotated[str,Field(min_length=1,max_length=300)]],Field(max_length=64)]|None = None
    skills: Annotated[list[Annotated[str,Field(min_length=1,max_length=300)]],Field(max_length=64)]|None = None

    @field_validator('selected_suggestion_indexes')
    @classmethod
    def distinct_suggestions(cls,v):
        if len(v)!=len(set(v)):raise ValueError('duplicate_suggestion')
        return v


class MaterialResult(Contract):
    action: Literal['analysis','draft','ask_user','alignment']
    alignment_items: Annotated[list[dict],Field(max_length=6)] = []
    proposals: Annotated[list[dict],Field(max_length=8)] = []
    fact_ids: Annotated[list[UuidString],Field(max_length=8)] = []
    question: Annotated[str,Field(max_length=300)] = ''
    # A batched rewrite merges up to three batches, so the *document* can hold more
    # than one batch's worth; the per-call cap stays on RewriteArgs.items (8).
    # Batching multiplies the document: six batches of at most eight items each.
    rewrite_items: Annotated[list[RewriteItem],Field(max_length=48)] = []
    basics: Annotated[list[Annotated[str,Field(min_length=1,max_length=300)]],Field(max_length=6)] = []
    education: Annotated[list[Annotated[str,Field(min_length=1,max_length=300)]],Field(max_length=64)] = []
    experience: Annotated[list[Annotated[str,Field(min_length=1,max_length=300)]],Field(max_length=64)] = []
    projects: Annotated[list[Annotated[str,Field(min_length=1,max_length=300)]],Field(max_length=64)] = []
    skills: Annotated[list[Annotated[str,Field(min_length=1,max_length=300)]],Field(max_length=64)] = []
    suggestions: Annotated[list[Annotated[str,Field(min_length=1,max_length=300)]],Field(max_length=4)] = []
    # The coverage ledger travels with the result so a revision can recompute it without
    # re-deriving segments from a filtered source list (F07Z-37).
    omitted_segments: Annotated[list[OmittedSegment],Field(max_length=120)] = []
    # F07Z-117: material longer than the six-batch budget is cut deterministically; these
    # segment IDs were never shown to the model and must be reported, not silently dropped.
    truncated_segment_ids: Annotated[list[SegmentId],Field(max_length=999)] = []
    structured_segment_ids: Annotated[list[SegmentId],Field(max_length=64)] = []
    # id -> section, so the UI can answer "where did this line go" without guessing
    # from text (F07Z-42).
    structured_sections: Annotated[dict[str,list[SegmentId]],Field()] = {}
    # Batched runs produce one entry per read and per submit; 8 was a one-shot assumption.
    tool_log: Annotated[list[dict],Field(max_length=20)] = []
    # S00Gb: claims the cited evidence did not support. The sentence itself was moved into
    # `suggestions`; this list is the machine-readable form (stable code + source pointer) so the
    # panel can explain what was withheld and why.
    grounding_findings: Annotated[list[dict],Field(max_length=16)] = []


def _flexible_match(text,quote):
    """Locate a quote whose characters match but whose whitespace was re-typed.

    Models routinely put a space around `+` or drop a double space when copying a line. The
    characters still have to match exactly and in order; only whitespace may differ, and the
    match has to be unique. Returns (start,end) or None.
    """
    compact=[c for c in quote if not c.isspace()]
    if not compact:return None
    hits=[]
    for start,ch in enumerate(text):
        if ch.isspace() or ch!=compact[0]:continue
        index=1;position=start+1
        while index<len(compact) and position<len(text):
            current=text[position]
            if current.isspace():position+=1;continue
            if current!=compact[index]:break
            index+=1;position+=1
        if index==len(compact):
            hits.append((start,position))
            if len(hits)>1:return None
    return hits[0] if hits else None


def grounded(proposal,sources):
    source=next((s for s in sources if s['id']==proposal.source_id),None)
    if source is None or not proposal.quote.strip():raise ValueError('source_not_in_scope')
    # F07Z-98: quotes are taken from the normalised segments the model was shown, so the
    # lookup has to use the same normalisation. Otherwise every quote of a Kangxi-radical
    # line fails as "not found" even though the model copied it exactly.
    from .segments import normalize_text
    text=normalize_text(source['text'])
    quote=normalize_text(proposal.quote)
    start=text.find(quote)
    if start>=0 and text.find(quote,start+1)<0:
        # Unique exact match.
        return {**proposal.model_dump(),'start_char':start,'end_char':start+len(quote),
                'content_sha256':hashlib.sha256(text.encode()).hexdigest()}
    # Fall back to a whitespace-insensitive match; characters still have to be identical.
    found=_flexible_match(text,quote)
    if found is None:raise ValueError('unique_exact_quote_required')
    start,end=found
    return {**proposal.model_dump(),'quote':text[start:end],'start_char':start,'end_char':end,
            'content_sha256':hashlib.sha256(text.encode()).hexdigest()}


def validate_result(scope,value):
    result=MaterialResult.model_validate(value)
    if scope.get('draft_style')=='rewrite' and result.action=='draft':
        if scope['mode']!='draft' or not result.rewrite_items or result.fact_ids or result.proposals or result.alignment_items or result.question:
            raise ValueError('rewrite_content_required')
        # Structured facts must not survive only because the model chose to write
        # them: if the source clearly has such a block, an empty list is an error.
        # F07Z-126: the demand follows the *declared blocks*, not marker words anywhere in the
        # text. Resume 4 wrote "技能栈" as a plain line (it was missing from HEADING_WORDS), so
        # its skill rows were told to live under `projects` while this backstop still demanded
        # `skills`: a requirement the batch rules forbade satisfying. The model could only
        # retry into `all_batches_done` and burn the rest of its budget.
        # F07Z-118: a source that opens no section at all (a spoken transcript) has no
        # sections to fill. Demanding one here forced a live draft to put an arbitrary
        # segment into 教育经历 — which is what pushed 29 bullets under that heading. The
        # renderer shows such material as one 经历要点 list, so the backstop only applies to
        # sources that actually declare sections.
        from .segments import heading_less, scope_segments
        segments=scope_segments(scope)
        declares_sections=not heading_less(segments)
        declared={segment['block'] for segment in segments if segment['block']}
        # F07Z-117: when the tail of the material was cut, a whole section may legitimately be
        # absent from what the model saw. The per-batch section-closing gaps still guard the
        # processed part; the global backstop would only reject an honest truncated draft.
        if not result.truncated_segment_ids and declares_sections:
            for block,code in (('education','education_section_missing'),
                               ('experience','experience_section_missing'),
                               ('projects','projects_section_missing'),
                               ('skills','skills_section_missing')):
                if block in declared and not getattr(result,block):
                    raise ValueError(code)
        allowed={s['id'] for s in scope['sources']}
        for item in result.rewrite_items:
            if len(set(item.source_ids))!=len(item.source_ids) or not set(item.source_ids)<=allowed:raise ValueError('rewrite_source_not_in_scope')
        return result
    if (result.rewrite_items or result.suggestions or result.basics or result.education
            or result.experience or result.projects
            or result.skills or result.omitted_segments
            or result.structured_segment_ids or result.structured_sections):raise ValueError('rewrite_only')
    if result.action=='alignment':
        from .alignment import validate_alignment
        validate_alignment(scope,result)
        return result
    if result.alignment_items:raise ValueError('alignment_only')
    if result.action=='ask_user':
        if not result.question.strip() or result.proposals or result.fact_ids:raise ValueError('invalid_clarification')
    elif result.action=='analysis':
        if scope['mode']!='analyze' or not result.proposals or result.fact_ids:raise ValueError('invalid_analysis')
        seen=set()
        for fact in result.proposals:
            checked=grounded(FactProposal.model_validate({k:fact[k] for k in ('source_id','kind','quote')}),scope['sources'])
            if checked!=fact:raise ValueError('fact_evidence_changed')
            identity=(fact['source_id'],fact['start_char'],fact['end_char'],fact['kind'])
            if identity in seen:raise ValueError('duplicate_fact')
            seen.add(identity)
    else:
        allowed={f['id'] for f in scope['facts']}
        if scope['mode']!='draft' or not result.fact_ids or len(set(result.fact_ids))!=len(result.fact_ids) or not set(result.fact_ids)<=allowed or result.proposals or result.question:
            raise ValueError('confirmed_facts_only')
    return result


def escaped(value):
    # Markdown needs `<`/`>` neutralised, not `&`: html.escape turned `Agent & LLM` into
    # `Agent &amp; LLM` in the exported file (F07Z-47).
    value=value.replace('<','&lt;').replace('>','&gt;')
    # F07Z-100: every rendered line already carries the `- ` list prefix, so inline `.`, `-`,
    # `+`, `#`, `!` and parentheses cannot start block syntax. Escaping them produced 50+
    # backslashes in the exported resume (F07Z-99 evidence). Keep the characters that still
    # matter inline: backtick, emphasis, brackets, table pipe and the backslash itself.
    return re.sub(r'([\\`*_\[\]|])',r'\\\1',value).replace('\n',' ')


def render_draft(scope,result):
    result=validate_result(scope,result)
    if result.action!='draft':raise ValueError('draft_required')
    if scope.get('draft_style')=='rewrite':
        from .segments import (accounting, check_evidence, heading_less, missing_sections,
                               omission_audit, placement, scope_segments)
        segments=scope.get('segments') or scope_segments(scope)
        check_evidence(segments,result.rewrite_items,result.omitted_segments,result.structured_segment_ids)
        grouped=placement(segments,result.rewrite_items,result.structured_sections)
        # F07Z-118: a source with no heading and no inline declaration has no sections to
        # attribute to. A live draft on a 7,457-character transcript put 29 bullets under
        # 教育经历 because one guessed anchor existed there; rendering them as one honest
        # 经历要点 list beats mislabelling every line.
        plain=heading_less(segments)
        used={sid for item in result.rewrite_items for sid in item.source_ids}
        def position(ids):
            found=[int(sid[1:]) for sid in ids if sid.startswith('S')]
            return min(found) if found else 10**6
        def section_body(field,lines,indices):
            structured=(result.structured_sections or {}).get(field) or []
            # No usable map (older drafts, hand-built results): keep the given order and
            # put the structured lines first instead of dropping them.
            if len(structured)==len(lines):
                # `lines` comes from `resolve_sections`, which sorts by segment id, while the
                # map keeps the model's submission order. Pair them by id, not by position
                # (F07Z-100: out-of-order map ids pushed a sub-project title below its bullets).
                ordered=sorted(structured,key=lambda sid:int(sid[1:]) if sid[1:].isdigit() else 10**6)
                entries=[(position([sid]),escaped(line)) for sid,line in zip(ordered,lines)]
            else:
                entries=[(0,escaped(line)) for line in lines]
            # A project's own bullets belong right under its title, so entries follow the
            # order they had in the source instead of "all titles, then all bullets".
            for index in indices:
                item=result.rewrite_items[index]
                entries.append((position(item.evidence_segments),escaped(item.text)))
            entries.sort(key=lambda pair:pair[0])
            return ['- '+text for _,text in entries]
        blocks=[]
        def push(heading,field,lines,indices):
            body=section_body(field,lines,indices)
            if body:blocks.append(heading+'\n\n'+'\n'.join(body))
        if plain:
            # No section was ever opened, so every "section" here is the model's guess. Render
            # the structured lines and the bullets in source order under one neutral heading:
            # nothing is mislabelled and nothing disappears.
            entries=[]
            for field in ('basics','education','experience','projects','skills'):
                for sid in (result.structured_sections or {}).get(field) or []:
                    row=next((s for s in segments if s['id']==sid),None)
                    if row is not None:
                        entries.append((position([sid]),escaped(row['text'])))
            for item in result.rewrite_items:
                entries.append((position(item.evidence_segments),escaped(item.text)))
            entries.sort(key=lambda pair:pair[0])
            if entries:
                blocks.append('# 经历要点\n\n'+'\n'.join('- '+text for _,text in entries))
        else:
            push('# 基本信息','basics',result.basics,grouped['basics'])
            push('# 教育经历','education',result.education,grouped['education'])
            push('# 工作经历','experience',result.experience,grouped['experience'])
            # F07Z-114: an empty section used to render as a bare heading (seen in the live
            # draft: a resume with no projects produced "# 项目经历" and nothing under it).
            push('# 项目经历','projects',result.projects,grouped['projects'])
            push('# 技能','skills',result.skills,grouped['skills'])
        # F07Z-117: segments past the batch budget were never shown to the model, so counting
        # them as "unaccounted" would blame the draft for text it could not see. Coverage and
        # the omission audit only cover what was actually offered; truncation is reported
        # separately through `truncated_segment_ids`.
        cut=set(result.truncated_segment_ids)
        offered=[s for s in segments if s['id'] not in cut]
        coverage=accounting(offered,result.structured_segment_ids,
                            [sid for item in result.rewrite_items for sid in item.evidence_segments],
                            [entry.segment_id for entry in result.omitted_segments])
        audit=omission_audit(offered,result.rewrite_items,result.omitted_segments)
        unsupported=[row['segment_id'] for row in audit if row['supported'] is False]
        return '\n\n'.join(blocks)+'\n',{
            'facts':[],'target':scope['target'],'draft_style':'rewrite',
            'candidate_status':'coverage_incomplete' if coverage['unaccounted'] else 'editable_suggestion',
            'rewrite_items':[i.model_dump() for i in result.rewrite_items],
            'basics':result.basics,'education':result.education,'experience':result.experience,
            'projects':result.projects,'skills':result.skills,'suggestions':result.suggestions,
            'omitted_segments':[e.model_dump() for e in result.omitted_segments],
            'structured_segment_ids':result.structured_segment_ids,
            'structured_map':result.structured_sections,
            'truncated_segment_ids':result.truncated_segment_ids,
            'segments':segments,'coverage':coverage,
            'omission_audit':audit,'unsupported_omissions':unsupported,
            'structure_note':'heading_less' if plain else '',
            'missing_sections':[] if plain else missing_sections(segments),
            'sources':[s for s in scope['sources'] if s['id'] in used],
            'change_note':'按用户自述优化表达；补充候选独立列出，未自动确认为经历事实。','omitted_fact_ids':[]}
    facts={f['id']:f for f in scope['facts']}
    lines=['# 求职材料草稿','','基于用户确认的声明编排；不代表外部核验。','']
    used=[]
    for kind,label in LABELS.items():
        selected=[facts[i] for i in result.fact_ids if facts[i]['kind']==kind]
        if selected:
            lines.extend(['## '+label,''])
            for f in selected:
                lines.append('- '+escaped(f['quote'])+' 〔事实 '+f['id']+'〕');used.append(f)
            lines.append('')
    return '\n'.join(lines),{'facts':used,'target':scope['target'],'change_note':'按经历/贡献/结果分类并选择已确认原文；未新增数字、职责或经历。','omitted_fact_ids':[i for i in facts if i not in result.fact_ids]}
