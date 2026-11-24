"""Learning resources: a short, verified list plus the links a user adds (F07Z-86).

Two rules shape this module.

1. The shipped entries are claims about the internet, so each one was fetched before being
   listed here; `CHECKED_ON` records when. A user's own link is *not* verified - the page says so
   instead of pretending otherwise.
2. Nothing is fetched by this service. It stores names and addresses and hands one to the
   browser when the user asks. That keeps the server free of outbound requests (no SSRF surface)
   and keeps the "does this link work" question where it belongs: with the user's browser.

Only user entries live in the file; the shipped list stays in code so a bad link can be fixed by
an update rather than by editing the user's data.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from interview_agent.api.dependencies import PrincipalDep, errors
from pydantic import Field

from interview_agent.contracts.common import Contract
from interview_agent.storage.errors import ApiError

from .site_sync import _data_dir

CONFIG_NAME = 'learning-resources.json'
# Every shipped address answered 200 on this date (see the node report for the fetch log).
CHECKED_ON = '2026-11-15'

DEFAULT_RESOURCES = [
    {'id': 'mdn', 'topic': '编程基础', 'name': 'MDN Web 文档',
     'blurb': '前端与浏览器 API 的权威参考，中文版覆盖 HTML/CSS/JavaScript',
     'url': 'https://developer.mozilla.org/zh-CN/'},
    {'id': 'python-tutorial', 'topic': '编程基础', 'name': 'Python 官方教程（中文）',
     'blurb': '从语法到标准库的官方入门，配合练习最稳',
     'url': 'https://docs.python.org/zh-cn/3/tutorial/'},
    {'id': 'runoob', 'topic': '编程基础', 'name': '菜鸟教程',
     'blurb': '查语法、查函数签名的快查站，适合临时对照',
     'url': 'https://www.runoob.com/'},
    {'id': 'liaoxuefeng', 'topic': '编程基础', 'name': '廖雪峰的官方网站',
     'blurb': '中文 Python / Java / Git 教程，讲解连贯',
     'url': 'https://www.liaoxuefeng.com/'},
    {'id': 'sqlbolt', 'topic': '编程基础', 'name': 'SQLBolt',
     'blurb': '交互式 SQL 入门，边写边看结果，适合补数据库基础',
     'url': 'https://sqlbolt.com/'},
    {'id': 'cs50', 'topic': '计算机基础', 'name': 'CS50（哈佛）',
     'blurb': '计算机科学导论，配套作业与讲解完整',
     'url': 'https://cs50.harvard.edu/x/'},
    {'id': 'mit-ocw', 'topic': '计算机基础', 'name': 'MIT OpenCourseWare',
     'blurb': '课程讲义与录像公开可看，适合系统补课',
     'url': 'https://ocw.mit.edu/'},
    {'id': 'khan', 'topic': '计算机基础', 'name': '可汗学院（中文）',
     'blurb': '数学与统计基础，笔试题里的数量关系常从这里补',
     'url': 'https://zh.khanacademy.org/'},
    {'id': 'icourse163', 'topic': '综合课程', 'name': '中国大学MOOC',
     'blurb': '国内高校课程，中文授课、按学期开课',
     'url': 'https://www.icourse163.org/'},
    {'id': 'imooc', 'topic': '综合课程', 'name': '慕课网',
     'blurb': '偏工程的实战课程，Java / 前端 / Python 都有体系课',
     'url': 'https://www.imooc.com/'},
    {'id': 'coursera', 'topic': '综合课程', 'name': 'Coursera',
     'blurb': '英文课程为主，部分可免费旁听',
     'url': 'https://www.coursera.org/'},
    {'id': 'freecodecamp', 'topic': '综合课程', 'name': 'freeCodeCamp（中文）',
     'blurb': '免费、带动手练习的编程课程，进度可保存',
     'url': 'https://www.freecodecamp.org/chinese/learn/'},
    {'id': 'roadmap', 'topic': '学习路线', 'name': 'roadmap.sh',
     'blurb': '各方向的学习路线图，用来判断"还缺哪块"',
     'url': 'https://roadmap.sh/'},
    {'id': 'github-skills', 'topic': '学习路线', 'name': 'GitHub Skills',
     'blurb': '用仓库练习 Git 与协作流程，几十分钟一课',
     'url': 'https://skills.github.com/'},
]

TOPIC_ORDER = ['编程基础', '计算机基础', '综合课程', '学习路线']
USER_TOPIC = '我添加的'

NAME_MAX = 60
URL_MAX = 300


class ResourceInput(Contract):
    name: Annotated[str, Field(min_length=1, max_length=NAME_MAX)]
    url: Annotated[str, Field(min_length=1, max_length=URL_MAX)]


def _config_path(request: Request) -> Path:
    override = os.environ.get('INTERVIEW_LEARNING_RESOURCES')
    if override:
        return Path(override)
    return _data_dir(request) / CONFIG_NAME


def _stored(request: Request) -> list[dict]:
    path = _config_path(request)
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    items = value.get('items') if isinstance(value, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict) and item.get('url') and item.get('name')]


def _write(request: Request, items: list[dict]) -> None:
    path = _config_path(request)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'items': items}, ensure_ascii=False, indent=1), encoding='utf-8')
    temporary.replace(path)


def _clean_url(raw: str) -> str:
    """http(s) only, a real host, no embedded credentials.

    The address goes to the user's browser, not to this service, so this is not an SSRF guard -
    it is there to keep `javascript:`, `file:` and `https://user:pass@host` out of the list.
    """
    value = raw.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https'):
        raise ApiError('learning_url_scheme_not_allowed', 400)
    if not parsed.hostname:
        raise ApiError('learning_url_host_missing', 400)
    if parsed.username or parsed.password:
        raise ApiError('learning_url_credentials_not_allowed', 400)
    return value


def _key(url: str) -> str:
    """Dedupe key: ignore case in the host and a trailing slash in the path."""
    parsed = urlsplit(url)
    path = parsed.path.rstrip('/') or '/'
    query = f'?{parsed.query}' if parsed.query else ''
    return f'{parsed.scheme}://{(parsed.hostname or "").lower()}{path}{query}'


def _items(request: Request) -> list[dict]:
    shipped = [{**item, 'source': 'default'} for item in DEFAULT_RESOURCES]
    stored = [{**item, 'topic': USER_TOPIC, 'source': 'user'} for item in _stored(request)]
    return shipped + stored


def routes():
    router = APIRouter(prefix='/api/desktop/learning-resources')
    @router.get('', responses=errors(401))
    async def listing(request: Request, user:PrincipalDep):
        return {'items': _items(request), 'topics': TOPIC_ORDER + [USER_TOPIC],
                'checked_on': CHECKED_ON,
                'note': '默认条目在 ' + CHECKED_ON + ' 当天逐个访问过；你自己添加的链接没有核对，'
                        '点开前请确认地址。应用只打开浏览器，不抓取页面内容、不保存账号。'}

    @router.post('')
    async def create(request: Request, value: ResourceInput, response: Response, user:PrincipalDep):
        name = value.name.strip()
        if not name:
            raise ApiError('learning_resource_name_missing', 400)
        url = _clean_url(value.url)
        key = _key(url)
        if any(_key(item['url']) == key for item in _items(request)):
            raise ApiError('learning_resource_exists', 409)
        stored = _stored(request)
        stored.append({'id': f'u-{uuid.uuid4().hex[:8]}', 'name': name, 'url': url})
        _write(request, stored)
        response.status_code = 201
        return {'items': _items(request), 'created': True}

    @router.delete('/{resource_id}')
    async def delete(resource_id: str, request: Request, user:PrincipalDep):
        stored = _stored(request)
        remaining = [item for item in stored if item.get('id') != resource_id]
        if len(remaining) == len(stored):
            # Shipped entries live in code; this endpoint only forgets the user's own links.
            raise ApiError('learning_resource_not_found', 404)
        _write(request, remaining)
        return {'items': _items(request), 'deleted': True}

    return router
