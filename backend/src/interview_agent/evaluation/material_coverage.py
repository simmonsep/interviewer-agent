"""Deterministic coverage check for material rewrites (external review §18).

Grounding and coverage are different questions and must be reported apart:

    grounding : did the rewrite add a fact the source never stated?
    coverage  : did the rewrite silently drop something the source did state?

`0 hallucination` does not imply `0 omission` — a real resume came back with zero grounding
violations and roughly a third of its content missing (F07Z-21).

Two layers, deliberately split by how much they can be trusted:

  * **structural tokens** (authoritative): dates, numbers, ranges, percentages and technology
    names that appear in the source must appear in the output. These are the facts a rewrite
    has no business dropping, and matching them is exact after whitespace/escape
    normalisation. Missing one is a failure.
  * **line coverage** (diagnostic): every substantive source line is compared against the
    output lines by character-bigram Jaccard. This only *flags* possible omissions for a
    human, because a legitimate rewrite may merge or reword lines. It is never a pass
    condition on its own.

The module never looks at the model's output for facts that are not in the source, so it
cannot be used as a grounding check by accident.
"""
import re

DATE = re.compile(r'\d{4}\s*[./-]\s*\d{1,2}|\d{4}\s*年|\b(?:19|20)\d{2}\b')
PERCENT = re.compile(r'\d+(?:\.\d+)?\s*%')
RANGE = re.compile(r'\d+(?:\.\d+)?\s*[-~至]\s*\d+(?:\.\d+)?')
NUMBER = re.compile(r'\d+(?:\.\d+)?')
TECH = ('redis', 'kafka', 'mysql', 'postgres', 'postgresql', 'elasticsearch', 'rabbitmq',
        'docker', 'kubernetes', 'langchain', 'langgraph', 'fastapi', 'react', 'vue', 'angular',
        'typescript', 'javascript', 'node', 'java', 'python', 'go', 'rust', 'spring', 'django',
        'flask', 'express', 'koa', 'egg', 'webpack', 'vite', 'mysql', 'echarts', 'socket.io',
        'websocket', 'element', 'iview', 'jquery', 'seajs', 'sequelize', 'eslint', 'fullpage',
        'vue-i18n', 'json2csv', 'node-mailer', 'sql', 'tauri', 'nginx')
HEADINGS = ('教育经历', '工作经历', '项目经历', '个人技能', '技能', '实习经历', '实践经历',
            '教育背景', '专业技能', '获奖情况', '自我评价')


def norm(text):
    """Drop whitespace and markdown escapes so `2014\\.09` matches `2014.09`."""
    return re.sub(r'[\s\\]', '', text or '')


def structural_tokens(text):
    """The facts a rewrite must not lose. Order-preserving, duplicates removed."""
    plain = norm(text)
    tokens = []
    for pattern in (DATE, PERCENT, RANGE, NUMBER):
        tokens.extend(match.group().replace(' ', '') for match in pattern.finditer(text or ''))
    lowered = (text or '').lower()
    tokens.extend(word for word in TECH if word in lowered)
    seen, unique = set(), []
    for token in tokens:
        key = norm(token)
        if key and key not in seen:
            seen.add(key)
            unique.append(token)
    return unique


def _bigrams(text):
    plain = norm(text)
    return {plain[i:i + 2] for i in range(len(plain) - 1)} if len(plain) > 1 else {plain}


def _similarity(line, output):
    left, right = _bigrams(line), _bigrams(output)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def source_lines(text, minimum=8):
    """Substantive lines only: no headings, no blanks, no one-word fragments."""
    lines = []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if len(norm(line)) < minimum or norm(line) in {norm(h) for h in HEADINGS}:
            continue
        lines.append(line)
    return lines


def coverage(source, output, *, line_threshold=0.35):
    """Return structural gaps (authoritative) and line coverage (diagnostic)."""
    # case-insensitive: tech tokens are compared in lower case, CJK is unaffected
    out = norm(output).lower()
    missing = [token for token in structural_tokens(source)
               if norm(token) not in out]
    lines, dropped = source_lines(source), []
    for line in lines:
        best = max((_similarity(line, segment) for segment in (output or '').splitlines()), default=0.0)
        if best < line_threshold:
            dropped.append({'line': line[:120], 'best_similarity': round(best, 3)})
    covered = len(lines) - len(dropped)
    return {
        'structural': {
            'tokens': len(structural_tokens(source)),
            'missing': missing,
            'passed': not missing,
        },
        'lines': {
            'total': len(lines),
            'covered': covered,
            'ratio': round(covered / len(lines), 3) if lines else None,
            'dropped': dropped,
            'threshold': line_threshold,
            'note': 'diagnostic only: a rewrite may merge or reword; human decides',
        },
        'passed': not missing,
        'semantic_validity': 'not_established',
    }
