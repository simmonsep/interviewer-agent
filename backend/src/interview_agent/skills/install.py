"""Agent Skills text subset: parse one uploaded directory into a validated document.

Only declarative text is accepted. Anything that would need execution or filesystem reach
(scripts, binaries, paths outside `references/`) is rejected with a specific code instead of being
silently ignored, so the UI can say why a package is incompatible.
"""
import re
import unicodedata

from interview_agent.storage.errors import ApiError

from .contracts import SkillFile

FORMAT = 'agent-skill-text-v1'
MAX_BODY_CHARS = 1200
MAX_TOTAL_BYTES = 64 * 1024
MAX_REFERENCES = 8
ALLOWED_REFERENCE_SUFFIXES = ('.md', '.txt')
SLUG = re.compile(r'^[a-z0-9][a-z0-9-]{0,59}$')


def _safe_path(raw: str) -> str:
    path = raw.replace('\\', '/').strip()
    while path.startswith('./'):
        path = path[2:]
    if not path or path.startswith('/') or re.match(r'^[A-Za-z]:', path):
        raise ApiError('skill_path_unsafe', 400)
    parts = path.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ApiError('skill_path_unsafe', 400)
    if any(ord(char) < 32 for char in path):
        raise ApiError('skill_path_unsafe', 400)
    return path


def _frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith('---'):
        raise ApiError('skill_frontmatter_missing', 400)
    lines = text.splitlines()
    if lines[0].strip() != '---':
        raise ApiError('skill_frontmatter_missing', 400)
    end = next((index for index, line in enumerate(lines[1:], 1)
                if line.strip() in ('---', '...')), None)
    if end is None:
        raise ApiError('skill_frontmatter_unterminated', 400)
    meta = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if ':' not in line:
            raise ApiError('skill_frontmatter_invalid', 400)
        key, value = line.split(':', 1)
        meta[key.strip().lower()] = value.strip()
    return meta, '\n'.join(lines[end + 1:]).strip()


def _slug(value: str) -> str:
    text = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return text[:60].strip('-')


def parse_bundle(files: list[SkillFile]) -> dict:
    paths = [_safe_path(item.path) for item in files]
    if len(set(paths)) != len(paths):
        raise ApiError('skill_path_duplicate', 400)
    total = sum(len(item.text.encode('utf-8')) for item in files)
    if total > MAX_TOTAL_BYTES:
        raise ApiError('skill_package_too_large', 400)
    entry = [item for item, path in zip(files, paths) if path == 'SKILL.md']
    if len(entry) != 1:
        raise ApiError('skill_entry_missing', 400)
    for item, path in zip(files, paths):
        if path != 'SKILL.md' and not path.startswith('references/'):
            raise ApiError('skill_path_not_supported', 400)
        if path != 'SKILL.md' and not path.lower().endswith(ALLOWED_REFERENCE_SUFFIXES):
            raise ApiError('skill_file_type_not_supported', 400)

    meta, body = _frontmatter(entry[0].text)
    name = meta.get('name', '').strip()
    description = meta.get('description', '').strip()
    if not name or len(name) > 80:
        raise ApiError('skill_name_invalid', 400)
    if not description or len(description) > 200:
        raise ApiError('skill_description_invalid', 400)
    skill_id = (meta.get('id') or '').strip() or _slug(name)
    if not SLUG.match(skill_id):
        raise ApiError('skill_id_invalid', 400)
    if not body:
        raise ApiError('skill_body_missing', 400)
    if len(body) > MAX_BODY_CHARS:
        raise ApiError('skill_body_too_long', 400)

    references = [{'path': path, 'text': item.text}
                  for item, path in zip(files, paths) if path.startswith('references/')]
    if len(references) > MAX_REFERENCES:
        raise ApiError('skill_reference_limit', 400)
    declared = [tool.strip() for tool in (meta.get('allowed-tools') or '').split(',') if tool.strip()]
    document = {
        'format': FORMAT,
        'skill_id': skill_id,
        'name': name,
        'description': description,
        'author': meta.get('author', '').strip()[:80],
        # Metadata only: the host grants tools, never the package.
        'declared_tools': declared[:12],
        'body': body,
        'references': references,
        'source': 'user_installed_text_only',
    }
    return document
