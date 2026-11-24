"""Verify the code references a record claims (F07Z-56).

A record line like ``main.rs:131-142`` or ``contracts.py:114/138/194`` is a factual claim
about this repository. Writing one from memory is how a node ends up citing the wrong
module, a deleted file, or a line number that moved three commits ago. This script turns
that class of mistake into a red build instead of a reviewer's job.

    python scripts/check-claims.py docs/records/desktop-handoff/F07Z-55-*.md
    python scripts/check-claims.py --all

Rules:
- Only backticked tokens are claims; prose numbers are ignored.
- A token must look like ``path`` or ``path:spec`` where spec is ``N``, ``N-M``, or a
  list separated by ``/`` or ``,`` (``114/138-140``).
- ``path`` is resolved from the repository root first; if that fails, by unique basename
  in the code roots below. Ambiguous basenames are reported, not silently accepted.
- A referenced line must exist (1 <= n <= line count). Content is not compared: this
  catches drift and wrong files, not wrong reasoning. Semantic claims (which module owns
  an error code) still need the grep-both-ends rule in AGENTS.md.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE_ROOTS = [
    ROOT / 'backend/src',
    ROOT / 'backend/tests',
    ROOT / 'backend/migrations',
    ROOT / 'apps/web/src',
    ROOT / 'apps/web/tests',
    ROOT / 'apps/desktop/src-tauri/src',
    ROOT / 'apps/desktop/src-tauri',
    ROOT / 'configs',
    ROOT / 'scripts',
    ROOT / 'e2e',
    ROOT / 'fixtures',
    ROOT / 'docs/records/desktop-handoff',
]
SUFFIXES = {'.py', '.ts', '.tsx', '.mts', '.mjs', '.js', '.rs', '.json', '.sql', '.md',
            '.css', '.toml', '.ps1'}
# Runtime/packaged paths and cross-record links appear in prose but are not repository
# files; they warn instead of failing (the class that matters is code drift).
WARN_SUFFIXES = {'.json', '.md'}

TOKEN = re.compile(r'`([^`\n]+)`')
PATH_SPEC = re.compile(r'^(?P<path>[^\s`]+?\.(?:py|ts|tsx|mts|mjs|js|rs|json|sql|md|css|toml|ps1))'
                       r'(?::(?P<spec>[0-9][0-9/,.\- ]*))?$')

SKIP_DIRS = {'__pycache__', 'node_modules', 'target', 'dist', '.git', '.venv',
             '.bootstrap', '.private', '.pytest_cache'}
INDEX: dict[str, list[Path]] = {}


def build_index() -> None:
    """Walk the code roots once; resolving by basename per token would rescan the tree."""
    for base in CODE_ROOTS:
        if not base.exists():
            continue
        for path in base.rglob('*'):
            if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
                continue
            INDEX.setdefault(path.name, []).append(path)


def candidates(raw: str) -> list[Path]:
    direct = ROOT / raw
    if direct.is_file():
        return [direct]
    name = Path(raw).name
    hits = INDEX.get(name, [])
    # A token that carries a directory prefix must match on that suffix, so
    # `written/programming.py` does not resolve to `question_banks/programming.py`.
    if '/' in raw:
        needle = raw.replace('\\', '/')
        hits = [h for h in hits if h.as_posix().endswith('/' + needle)]
    # Overlapping roots (src-tauri and src-tauri/src) produce the same file twice.
    return sorted({h.resolve() for h in hits})


def parse_spec(spec: str) -> list[tuple[int, int]]:
    ranges = []
    for part in re.split(r'[/,]', spec.replace(' ', '')):
        if not part:
            continue
        if '-' in part:
            start, _, end = part.partition('-')
            if start.isdigit() and end.isdigit():
                ranges.append((int(start), int(end)))
        elif part.isdigit():
            ranges.append((int(part), int(part)))
    return ranges


def check_file(path: Path, strict: bool = False) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    warnings: list[str] = []
    text = path.read_text(encoding='utf-8', errors='replace')
    for lineno, line in enumerate(text.splitlines(), 1):
        for token in TOKEN.findall(line):
            match = PATH_SPEC.match(token.strip())
            if not match:
                continue
            raw, spec = match.group('path'), match.group('spec')
            found = candidates(raw)
            suffix = Path(raw).suffix.lower()
            if not found:
                message = f'{path.name}:{lineno}: missing file `{token}`'
                (warnings if suffix in WARN_SUFFIXES else problems).append(message)
                continue
            if len(found) > 1:
                message = (f'{path.name}:{lineno}: ambiguous `{raw}` ('
                           + ','.join(str(h.relative_to(ROOT)) for h in found[:3]) + ')')
                # A bare `agent.py` is how a record ends up citing the wrong module;
                # strict mode is what a new record must pass. History keeps a warning.
                (problems if strict else warnings).append(message)
            if not spec:
                continue
            ranges = parse_spec(spec)
            # With an ambiguous name, accept the claim if any candidate file can hold it:
            # range drift is only reported when no plausible module matches.
            total = None
            for target in found:
                count = len(target.read_text(encoding='utf-8', errors='replace').splitlines())
                if all(1 <= start <= end <= count for start, end in ranges):
                    total = count
                    break
            if total is None:
                target = found[0]
                count = len(target.read_text(encoding='utf-8', errors='replace').splitlines())
                problems.append(f'{path.name}:{lineno}: `{token}` out of range '
                                f'({target.relative_to(ROOT)} has {count} lines)')
    return problems, warnings


def main(argv: list[str]) -> int:
    strict = '--strict' in argv
    if '--all' in argv:
        files = sorted((ROOT / 'docs/records/desktop-handoff').glob('*.md'))
    else:
        files = [Path(a) if Path(a).is_absolute() else ROOT / a
                 for a in argv if not a.startswith('-')]
    if not files:
        print('usage: check-claims.py [--strict] <file.md> [more.md] | --all')
        return 2
    problems: list[str] = []
    warnings: list[str] = []
    build_index()
    for file in files:
        if not file.is_file():
            problems.append(f'missing record {file}')
            continue
        found, warned = check_file(file, strict)
        problems.extend(found)
        warnings.extend(warned)
    for warning in warnings:
        print('WARN ', warning)
    for problem in problems:
        print('ERROR', problem)
    print(f'checked {len(files)} record(s): {len(problems)} error(s), {len(warnings)} warning(s)'
          + (' [strict]' if strict else ''))
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
