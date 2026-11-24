"""Deterministic source segmentation: code owns which line a claim came from.

External review (F07Z-33) rejected "the model retypes the line and we check it is close":
that leaves content generation with the model and turns provenance into a semantic
equivalence question. Instead the source is cut into stable segments here, the model only
answers *which section a segment belongs to*, and the renderer copies `segment['text']`.

The IDs are a pure function of the source text, so they are reproducible inside one
material revision without storing anything: the draft is marked stale as soon as the
material revision changes.
"""
import re
import unicodedata

# A heading is recognised only when the whole line is a section name. Rows such as
# `技能：Node` carry content and stay ordinary segments.
HEADING_WORDS = frozenset({
    '教育经历', '教育背景', '学习经历', '学历', '教育',
    '工作经历', '工作经验', '实习经历', '任职', '工作单位', '实习',
    '项目经历', '项目经验', '个人项目', '项目',
    '技能', '技能清单', '专业技能', '个人技能', '技术栈', '技能栈', '核心技能', '能力', '工具',
    'education', 'experience', 'work experience', 'projects', 'project experience', 'skills',
})

_ID = re.compile(r'^S\d{3}$')
_NO_BLOCK = object()
# A period line is the anchor of a job/degree/project record: dropping one
# silently loses a whole history entry (F07Z-40 dropped a second job line).
DATED_SEGMENT = re.compile(r'\d{4}\s*[.\-/年]\s*\d{1,2}')
# A tech-stack row names the tools a project used: dropping it silently loses the
# only place those names appear (F07Z-50).
TECH_STACK = re.compile(r'技术栈|Tech\s*Stack', re.I)
MAX_SEGMENTS = 999

# Which source block a heading opens. Order matters: the first family whose marker
# appears wins, so `项目经历` is checked before the generic education markers.
BLOCK_MARKERS = (
    ('skills', ('技能', '技术栈', 'skills')),
    ('projects', ('项目经历', '项目经验', '个人项目', 'projects')),
    ('experience', ('工作经历', '工作经验', '实习经历', '任职', '工作单位', 'experience')),
    ('education', ('教育', '学历', '本科', '硕士', '博士', '大专', '大学', '学院',
                   'education', 'degree')),
)


def normalize_text(text):
    """Map compatibility ideographs (Kangxi radicals and CJK compatibility forms) to the
    standard characters, and leave everything else alone.

    F07Z-98: some PDF fonts encode headings with U+2F2F instead of U+5DE5 ("工"), so
    "⼯作经历" was not recognised as a heading and the whole work history fell into the
    previous section. A full NFKC pass also rewrites punctuation (full-width colon to ASCII)
    and changed quoting and fixtures; this targeted mapping fixes the heading problem without
    touching the author's punctuation.
    """
    out=[]
    for ch in text or '':
        code=ord(ch)
        out.append(unicodedata.normalize('NFKC',ch) if 0x2E80<=code<=0x2FDF or 0xF900<=code<=0xFAFF else ch)
    return ''.join(out)


def section_of(text):
    value = (text or '').lower()
    for name, markers in BLOCK_MARKERS:
        if any(marker in value for marker in markers):
            return name
    return None


def is_heading(text):
    value = line_text(text).lstrip('#*-•· ').strip().rstrip('：:').strip()
    return value.lower() in HEADING_WORDS


def line_text(text):
    """Keep the line as written; only trailing whitespace (and CR) is dropped."""
    return text.rstrip()


def segments_of(text):
    """Split one source into segments, in order, skipping blank lines.

    F07Z-98: PDF text can carry compatibility ideographs (for example the Kangxi radical
    U+2F2F instead of U+5DE5 "工"), which made headings like "⼯作经历" unrecognised and put a
    whole work history under education. NFKC maps them to the standard characters before any
    heading or section decision; the rendered draft uses the same normalised text.
    """
    text = normalize_text(text)
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        raw = line_text(line)
        if not raw.strip():
            continue
        out.append({'line': number, 'text': raw, 'heading': is_heading(raw)})
    return out


def scope_segments(scope):
    """Every non-empty line of every source in scope, numbered globally: S001, S002, ...

    Each segment also carries the block (`education`/`experience`/`projects`/`skills`) opened
    by the most recent heading, or `None` when the source has no headings at all. Blocks let
    the renderer put a narrative bullet back under the section its evidence came from.
    """
    out = []
    for source in scope.get('sources') or ():
        block = None
        for segment in segments_of(source['text']):
            if len(out) >= MAX_SEGMENTS:
                raise ValueError('material_scope_too_many_segments')
            if segment['heading']:
                block = section_of(segment['text'])
            out.append({'id': f'S{len(out) + 1:03d}', 'source_id': source['id'],
                        'block': block, **segment})
    return out


def observation(source, segments):
    """What `read_source` shows the model in analyze mode: the original text plus the
    addressable segments."""
    return {**source, 'text': normalize_text(source['text']),
            'untrusted_personal_statement': True,
            'segments': [{k: s[k] for k in ('id', 'text', 'heading', 'block')}
                         for s in segments if s['source_id'] == source['id']]}


def rewrite_observation(source, narrative, batches, index=0, detected=None):
    """What `read_source` shows the model in rewrite mode (F07Z-117).

    The old shape sent the full text plus every segment, so 6,000 Chinese characters already
    reached the 32 KiB model payload. Rewrite only ever cites the current batch, so the current
    batch is all the model may use, and the source text is already inside those segments.

    The envelope states the protocol explicitly. Without it a live run saw
    `total_segments: 50` but only nine IDs, called `read_source` a second time, got the same
    nine segments again, concluded the material was truncated and asked the user instead of
    submitting batch 1 (F07Z-117 handoff 3).

    `detected` (F07Z-118) is the code-owned section inference for heading-free material: it
    tells the model which record rows are education/experience/projects/skills, so a transcript
    without `##` headings still lands in the right fields.
    """
    current = batches[index] if 0 <= index < len(batches) else []
    wanted = set(current)
    payload = {'id': source['id'], 'name': source.get('name', ''),
               'untrusted_personal_statement': True,
               'batch': index + 1, 'total_batches': len(batches),
               'total_segments': len(narrative),
               'segments': [{k: s[k] for k in ('id', 'text', 'heading', 'block')}
                            for s in narrative if s['id'] in wanted],
               'rule': (f'这是第 {index + 1}/{len(batches)} 批，只列出本批可以引用的段落。'
                        '提交本次 submit_rewrite 后，下一批段落会随续接消息自动给出；'
                        '不要为了看后面的内容重复调用 read_source（它不会返回其他批次）。')}
    if detected:
        payload['detected_sections'] = detected
        payload['rule'] += ('原文没有小标题时，按 detected_sections 把对应段落填进 experience/'
                            'education/projects/skills；没有检出的字段留空，不要把所有条目塞进同一段。')
    return payload


def resolve_sections(segments, fields):
    """Turn `{section: [S001, ...]}` into verbatim lines, in source order.

    Every rejection here is deterministic: an unknown ID, a heading used as an entry, or
    the same line assigned to two sections. The model gets the reason back as a tool error
    and may correct itself inside the remaining request budget.
    """
    by_id = {s['id']: s for s in segments}
    used = {}
    resolved = {}
    for field, ids in fields.items():
        chosen = []
        for segment_id in ids:
            segment = by_id.get(segment_id)
            if segment is None:
                raise ValueError(f'{field}_segment_not_in_scope')
            if segment['heading']:
                raise ValueError(f'{field}_heading_not_allowed')
            if segment_id in used:
                # Checked before the block rule: a line assigned twice is the more specific
                # error, and the duplicate must be reported even when both sections disagree
                # with the block (F07Z-100 reordering).
                raise ValueError('segment_assigned_twice')
            if segment['block'] and segment['block'] != field:
                # F07Z-100 (decision D): a line under one heading must not be filed into a
                # different section; otherwise a sub-project title lands in `projects` while
                # its bullets stay under `experience`.
                raise ValueError(f'{field}_cross_block')
            used[segment_id] = field
            chosen.append(segment)
        chosen.sort(key=lambda s: int(s['id'][1:]))
        resolved[field] = [s['text'] for s in chosen]
    return resolved

def check_evidence(segments, items, omitted, structured_ids):
    """Deterministic contract checks for narrative evidence (F07Z-37).

    These are structural violations, so they become a tool error the model can correct
    inside its remaining budget — unlike an unaccounted segment, which is saved and
    surfaced rather than thrown away.
    """
    by_id = {s['id']: s for s in segments}
    structured = set(structured_ids)
    covered = {}
    for item in items:
        blocks = set()
        for segment_id in item.evidence_segments:
            segment = by_id.get(segment_id)
            if segment is None:
                raise ValueError('evidence_segment_not_in_scope')
            if segment_id in structured:
                raise ValueError('evidence_segment_is_structured')
            if segment_id in covered:
                raise ValueError('evidence_segment_reused')
            covered[segment_id] = True
            blocks.add(segment['block'])
        if len(blocks) > 1:
            raise ValueError('cross_block_evidence')
    for entry in omitted:
        segment = by_id.get(entry.segment_id)
        if segment is None:
            raise ValueError('omitted_segment_not_in_scope')
        if segment['heading']:
            raise ValueError('omitted_heading_not_allowed')
        if entry.segment_id in structured:
            raise ValueError('omitted_segment_is_structured')
        if entry.segment_id in covered:
            raise ValueError('omitted_segment_conflict')
        if DATED_SEGMENT.search(segment['text']):
            raise ValueError('dated_segment_omitted')
        if TECH_STACK.search(segment['text']):
            raise ValueError('tech_stack_segment_omitted')


def accounting(segments, structured_ids, evidence_ids, omitted_ids):
    """The hard invariant: every eligible narrative segment is covered or explicitly omitted.

    `eligible` excludes headings and the lines the model assigned to the four structured
    sections, because those are already owned by code. Percentages are deliberately not
    reported as quality: `explicitly_omitted > 0` is legitimate, `unaccounted > 0` is not.
    """
    structured = set(structured_ids)
    covered = set(evidence_ids)
    omitted = set(omitted_ids)
    eligible = [s['id'] for s in segments if not s['heading'] and s['id'] not in structured]
    missing = [sid for sid in eligible if sid not in covered and sid not in omitted]
    return {
        'eligible': len(eligible),
        'covered': len([sid for sid in eligible if sid in covered]),
        'omitted': len([sid for sid in eligible if sid in omitted]),
        'unaccounted': len(missing),
        'missing_segment_ids': missing,
    }


def placement(segments, items, structured=None):
    """Group narrative items by the block their evidence came from.

    F07Z-100: when the source has no headings, `block` is None for every line. In that case the
    nearest *preceding* structured assignment (the model's `structured_sections`) is the anchor,
    so bullets after "实习经历：..." go to experience and bullets after "项目经历：..." go to
    projects. Only a truly anchor-less line falls back to projects.
    """
    by_id = {s['id']: s for s in segments}
    anchors = []
    for field, ids in (structured or {}).items():
        if field not in ('education', 'experience', 'projects', 'skills', 'basics'):
            continue
        for segment_id in ids:
            if segment_id in by_id:
                anchors.append((int(segment_id[1:]), field))
    anchors.sort()
    grouped = {'basics': [], 'education': [], 'experience': [], 'projects': [], 'skills': []}
    for index, item in enumerate(items):
        block = next((by_id[sid]['block'] for sid in item.evidence_segments
                      if sid in by_id and by_id[sid]['block']), None)
        if block is None and anchors:
            first = min((int(sid[1:]) for sid in item.evidence_segments if sid.startswith('S')),
                        default=None)
            if first is not None:
                prior = [field for anchor, field in anchors if anchor < first]
                block = prior[-1] if prior else None
        grouped[block if block in grouped else 'projects'].append(index)
    return grouped

_AUDIT_TOKEN = re.compile(r'[a-z][a-z0-9.+#/_-]*|\d+(?:\.\d+)?')


def audit_tokens(text):
    """Content tokens for auditing an omission claim.

    `merged_elsewhere` is the only omission reason that makes a *checkable* claim: "this
    line's content is already in another bullet". We compare character bigrams of the
    segment with the text of the items that cite the same block, plus ASCII words and
    numbers so `socket.io` or `20%~50%` cannot hide behind Chinese paraphrase.
    """
    value = (text or '').lower()
    tokens = set(_AUDIT_TOKEN.findall(value))
    cjk = re.sub(r'[^\u4e00-\u9fff]', '', value)
    tokens.update(cjk[index:index + 2] for index in range(len(cjk) - 1))
    if len(cjk) == 1:
        tokens.add(cjk)
    return tokens


def omission_audit(segments, items, omitted, threshold=0.5):
    """Check the only omission claim that can be checked at all.

    This is deliberately not a coverage criterion — coverage stays "cited or explicitly
    omitted". It only answers "when the model said 'merged elsewhere', is any of that line
    actually visible in the bullets of the same block?" An unsupported claim is surfaced,
    never silently accepted, and never used to fail the run: a paid draft with one weak
    explanation is still worth more to the user than nothing.
    """
    by_id = {s['id']: s for s in segments}
    text_by_block = {}
    for position, item in enumerate(items):
        block = next((by_id[sid]['block'] for sid in item.evidence_segments if sid in by_id), None)
        text_by_block.setdefault(block, []).append(item.text)
    audit = []
    for entry in omitted:
        segment_id = entry.segment_id if hasattr(entry, 'segment_id') else entry['segment_id']
        reason = entry.reason if hasattr(entry, 'reason') else entry['reason']
        segment = by_id.get(segment_id)
        if segment is None:
            continue
        tokens = audit_tokens(segment['text'])
        if reason != 'merged_elsewhere':
            audit.append({'segment_id': segment_id, 'reason': reason,
                          'audited': False, 'overlap': None, 'supported': None})
            continue
        visible = audit_tokens('\n'.join(text_by_block.get(segment['block'], [])))
        overlap = round(len(tokens & visible) / len(tokens), 2) if tokens else 0.0
        audit.append({'segment_id': segment_id, 'reason': reason, 'audited': True,
                      'overlap': overlap, 'supported': overlap >= threshold})
    return audit

SECTION_LABELS = {'education': '教育经历', 'experience': '工作经历',
                  'projects': '项目经历', 'skills': '技能'}


def missing_sections(segments):
    """Which standard sections the source never opened.

    Heading-driven and therefore deterministic: a section counts as present when the source
    opens it with a heading (or a row placed under one). Nothing is inferred from the
    content, so the panel can ask the owner "do you have this?" instead of inventing it —
    which is the point: a resume missing a section is normal, and completing it is the
    owner's decision, not the model's (F07Z-39).
    """
    present = {s['block'] for s in segments if s['block']}
    return [name for name in SECTION_LABELS if name not in present]

def narrative_batches(segments, size=8):
    """Partition narrative segments into contiguous, non-overlapping batches.

    Whole source blocks are packed together while they fit; a block larger than `size` is
    cut into contiguous windows. Batches are code-owned, so the model can neither see the
    same source twice nor skip a block (external review F07Z-36 §11-§12).
    """
    blocks = []
    for segment in segments:
        if segment['heading']:
            continue
        if not blocks or blocks[-1][0]['block'] != segment['block']:
            blocks.append([])
        blocks[-1].append(segment)
    batches, current = [], []
    for block in blocks:
        if len(block) > size:
            if current:
                batches.append(current)
                current = []
            windows = [block[start:start + size] for start in range(0, len(block), size)]
            batches.extend(windows[:-1])
            current = windows[-1]        # the tail window may still take the next block
            continue
        if current and len(current) + len(block) > size:
            batches.append(current)
            current = []
        current.extend(block)
    if current:
        batches.append(current)
    return [[segment['id'] for segment in batch] for batch in batches]


def plan_batches(segments, max_batches=6, max_window=20):
    """Adaptive batch plan for long material (F07Z-117, decision THIRD_OPTION).

    Up to six batches keep the original `size=8` windows unchanged. Longer material grows the
    window (never above `max_window`) so it still fits two reads plus six batches plus the four
    correction slots - the single-authorization budget. Material that is still too long is cut
    at `max_batches`; the caller records the untouched segment IDs instead of failing.
    """
    first = narrative_batches(segments)
    if len(first) <= max_batches:
        return first, []
    count = sum(1 for segment in segments if not segment['heading'])
    size = min(max_window, max(9, -(-count // max_batches)))
    batches = narrative_batches(segments, size=size)
    if len(batches) <= max_batches:
        return batches, []
    kept = [sid for batch in batches[:max_batches] for sid in batch]
    kept_set = set(kept)
    truncated = [s['id'] for s in segments
                 if not s['heading'] and s['id'] not in kept_set]
    return batches[:max_batches], truncated


# F07Z-99: a 41-line resume needs ~6 analysis requests and up to 8 rewrite requests in one
# goal; 12 left no room for both. The per-run ceiling (`request_ceiling`) and the money fuse
# (authorization + policy) still bound spending, so this counter is capacity, not a budget.
MAX_REQUESTS = 16
"""Hard per-run request ceiling. The SQL counter CHECK and the goal fuse use the same number."""

REQUEST_HEADROOM = 4
"""Measured correction headroom on top of the fixed reads and one request per batch."""


def request_ceiling(batch_count):
    """The per-run request ceiling for a batch plan.

    One definition, three consumers: the agent loop, the number reported to the model in
    `read_material_scope`, and the SQL admission guard. Keeping them separate is what let a
    stale `6` survive migration 026 and fail a four-batch rewrite in the real window
    (F07Z-51 / ENG-24).
    """
    return min(MAX_REQUESTS, 2 + batch_count + REQUEST_HEADROOM)


def is_dated(text):
    """True when the line carries a start/end period, i.e. it anchors a history entry."""
    return bool(DATED_SEGMENT.search(text or ''))


# F07Z-118: a heading-free transcript ("我先说说我自己…") carries no section markers, so the
# four structured fields had nothing to stand on and a live draft dumped 29 bullets under
# 教育经历. These rules only accept *record-shaped* lines: an inline declaration
# (`教育经历：…`), a dated line with a company/school/project keyword, or a short skill/degree
# statement. Anything else stays narrative on purpose — guessing a section invents structure
# the source never had, and a wrong heading is worse than no heading.
RECORD_MAX_CHARS = 120
EDU_WORDS = re.compile(r'本科|硕士|博士|大专|研究生|在读|毕业|学历|学位')
SCHOOL_WORDS = re.compile(r'大学|学院|学校|专业')
WORK_WORDS = re.compile(r'公司|实习|任职|岗位|入职|在职|雇主')
PROJECT_WORDS = re.compile(r'项目|系统|平台|助手|重构')
SKILL_WORDS = re.compile(r'技能|技术栈|熟悉|掌握|用过|会用')


def section_candidates(segments):
    """Deterministic section anchors for a heading-free source (F07Z-118).

    Returns `{field: [S###, ...]}` in source order, capped at three per field. A line only
    counts when it already declares its own section or is short enough to be a record row;
    long narrative paragraphs are never promoted, because rendering them verbatim under a
    section heading produces exactly the wrong resume.
    """
    found = {'education': [], 'experience': [], 'projects': [], 'skills': []}
    for segment in segments:
        if segment['heading']:
            continue
        text = (segment['text'] or '').strip().lstrip('#*-•· ')
        declared = inline_section(segment['text'])
        field = declared or _record_field(text)
        if field and len(found[field]) < 3:
            found[field].append(segment['id'])
    return {field: ids for field, ids in found.items() if ids}


def _record_field(text):
    value = (text or '').strip()
    if not value or len(value) > RECORD_MAX_CHARS:
        return None
    dated = is_dated(value)
    if dated:
        if SCHOOL_WORDS.search(value) or EDU_WORDS.search(value):
            return 'education'
        if WORK_WORDS.search(value):
            return 'experience'
        if PROJECT_WORDS.search(value):
            return 'projects'
        return None
    if SCHOOL_WORDS.search(value) and EDU_WORDS.search(value):
        return 'education'
    if SKILL_WORDS.search(value):
        return 'skills'
    return None


def heading_less(segments):
    """True when the source opens no section at all — neither a heading nor `X：` inline."""
    return not any(segment['heading'] or inline_section(segment['text'])
                   for segment in segments)


def inline_section(text):
    """The block a heading-free line declares inline, e.g. `实习经历：某公司` (F07Z-100).

    Only an actual section name before the colon counts: `求职意向：Java` is a header line,
    not a section declaration, so it must not end the header block.
    """
    value = (text or '').strip().lstrip('#*-•· ')
    for separator in ('：', ':'):
        if separator in value:
            head = value.split(separator, 1)[0].strip().lower()
            return section_of(head) if head in HEADING_WORDS else None
    return None


def pre_heading_ids(segments, limit=6):
    """Body lines that appear before the first heading: the resume header block.

    Position is the only signal code can trust here (F07Z-48/49): asking the model to file
    "求职意向 / 在读情况 / 城市" under a field did not work — it merged them into a project
    bullet instead.
    """
    if not any(segment['heading'] for segment in segments):
        # F07Z-100: a heading-free *resume* that declares its sections inline
        # ("教育经历：...") still has a header block, or the name line is dropped by both the
        # code and the model (synthetic draft evidence). A plain note with no section markers
        # keeps the F07Z-49 behaviour: everything is content.
        if not any(inline_section(segment['text']) for segment in segments):
            return []
        found = []
        for segment in segments:
            text = segment['text']
            if inline_section(text) or is_dated(text) or TECH_STACK.search(text):
                break
            found.append(segment['id'])
            if len(found) == min(limit, 4):
                break
        return found
    found = []
    for segment in segments:
        if segment['heading']:
            break
        found.append(segment['id'])
        if len(found) == limit:
            break
    return found


def is_tech_line(text):
    """True when the line names a project's technology stack."""
    return bool(TECH_STACK.search(text or ''))
