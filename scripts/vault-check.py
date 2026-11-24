"""Documentation consistency check (no model calls, no DB).

Why this exists: the repo keeps node status in one table, the plan keeps a node board,
and the Obsidian vault holds the "why". Three places drift by hand; this script is the
machine check that AGENTS.md node discipline #4 requires.

Checks
  1. every report file under docs/records/desktop-handoff has a row in 00-进度索引.md
  2. every report path named in the index actually exists
  3. the F07N node board and the index agree on status (normalised)
  4. vault notes have valid frontmatter (type/status)
  5. [[links]] inside the vault resolve to an existing note
  6. the vault contains no node status table (single source of truth)

Exit code 1 when any check fails.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDOFF = ROOT / 'docs/records/desktop-handoff'
INDEX = HANDOFF / '00-进度索引.md'
PLAN = HANDOFF / 'F07N-后续计划.md'
VAULT = ROOT / 'vault'

NODE_KEY = re.compile(r'^([A-Z][A-Z0-9]{1,}(?:-[0-9A-Z]+)*)')
REPORT_FILE = re.compile(r'^(F[0-9]{2}[A-Z]?|CORE|D-[0-9]{2}|JA|PY|PI)')
BACKTICK_MD = re.compile(r'`([^`]+\.md)`')

DONE = {'已完成', '通过', '✅', '规划完成'}
PARTIAL = {'部分完成', '部分通过'}
WIP = {'进行中', '方案评审中'}
PLANNED = {'已定稿', '定稿'}
TODO = {'待做'}
NOTE_TYPES = {'system', 'decision', 'implementation', 'incident', 'debugging', 'eval',
              'experiment', 'technology', 'metric', 'security', 'testing', 'retrospective',
              'interview', 'glossary', 'moc', 'project', 'timeline'}
NOTE_STATUS = {'draft', 'verified', 'stale', 'superseded', 'archived'}

errors, warnings = [], []


def normalise(status):
    text = status.strip().strip('*').strip()
    text = re.sub(r'[（(].*?[）)]', '', text).strip()   # "已定稿（待实现）" -> "已定稿"
    for group, name in ((DONE, 'done'), (PARTIAL, 'partial'), (WIP, 'wip'),
                        (PLANNED, 'planned'), (TODO, 'todo')):
        if text in group:
            return name
    return 'unknown:' + text


def table_rows(path):
    """Rows of every markdown table whose first cell is node-shaped."""
    rows, inside = [], False
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.startswith('```'):
            inside = not inside
            continue
        if inside or not line.startswith('|'):
            continue
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if len(cells) < 2:
            continue
        match = NODE_KEY.match(cells[0])
        if match:
            rows.append((match.group(1), cells))
    return rows


def check_index():
    index_rows = table_rows(INDEX)
    index_status = {}
    for key, cells in index_rows:
        index_status.setdefault(key, cells[1])
        if len(cells) >= 4:
            for name in BACKTICK_MD.findall(cells[3]):
                # Reports are named relative to the handoff dir; root-level docs (AGENTS.md,
                # HANDOFF.md) and repo-relative paths (benchmarks/...) are also legitimate.
                candidates = [HANDOFF / name, ROOT / name] if '/' not in name else [ROOT / name]
                if not any(candidate.exists() for candidate in candidates):
                    errors.append('index names a missing report: %s (node %s)' % (name, key))
    known = set(index_status)
    for report in sorted(HANDOFF.glob('*.md')):
        if not REPORT_FILE.match(report.name):
            continue
        # A node row covers every report whose filename starts with that node id
        # (e.g. "F07Z" covers "F07Z-01-prompt…", "F07X" covers "F07X-03-gold…").
        if not any(report.name.startswith(key) for key in known):
            errors.append('report without an index row: %s' % report.name)
    return index_status


def check_plan(index_status):
    board = {}
    for key, cells in table_rows(PLAN):
        board.setdefault(key, cells[1])
    for key, status in board.items():
        if key not in index_status:
            warnings.append('plan board lists %s but the index has no row' % key)
            continue
        left, right = normalise(status), normalise(index_status[key])
        if left != right and 'unknown' not in (left, right):
            errors.append('status drift for %s: plan=%s index=%s' % (key, status, index_status[key]))


def frontmatter(path):
    lines = path.read_text(encoding='utf-8').splitlines()
    if not lines or lines[0].strip() != '---':
        return None
    meta = {}
    for line in lines[1:]:
        if line.strip() == '---':
            return meta
        if ':' in line:
            name, _, value = line.partition(':')
            meta[name.strip()] = value.strip()
    return None


def check_vault():
    notes = {}
    for path in sorted(VAULT.rglob('*.md')):
        if '.obsidian' in path.parts:
            continue
        notes[path.stem.lower()] = path
    for path in notes.values():
        meta = frontmatter(path)
        if meta is None:
            errors.append('vault note without frontmatter: %s' % path.relative_to(ROOT))
            continue
        if meta.get('type') not in NOTE_TYPES:
            errors.append('bad/missing type in %s: %r' % (path.relative_to(ROOT), meta.get('type')))
        if meta.get('status') not in NOTE_STATUS:
            errors.append('bad/missing status in %s: %r' % (path.relative_to(ROOT), meta.get('status')))
        fenced = False
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if line.startswith('```'):
                fenced = not fenced
                continue
            if fenced:
                continue
            if re.match(r'^\|\s*(节点|Node)\s*\|', line):
                errors.append('vault must not duplicate the node status table: %s:%d' % (path.relative_to(ROOT), number))
            if 'Templates' in path.parts:
                continue
            prose = re.sub(r'`[^`]*`', '', line)
            for target in re.findall(r'\[\[([^\]]+)\]\]', prose):
                name = target.split('|')[0].split('#')[0].strip().lower()
                if not name or '...' in name:
                    continue
                if name not in notes:
                    warnings.append('unresolved link [[%s]] in %s' % (target, path.relative_to(ROOT)))
    return len(notes)


def main():
    if not INDEX.exists() or not PLAN.exists():
        print('missing index or plan file', file=sys.stderr)
        return 2
    index_status = check_index()
    check_plan(index_status)
    notes = check_vault()
    for message in errors:
        print('ERROR  ' + message)
    for message in warnings:
        print('warn   ' + message)
    print('checked: %d index nodes, %d vault notes, %d errors, %d warnings'
          % (len(index_status), notes, len(errors), len(warnings)))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
