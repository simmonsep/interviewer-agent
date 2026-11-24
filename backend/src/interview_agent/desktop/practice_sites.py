"""External algorithm-site entry and manual practice records (P3.3 / F07H §4).

The product does **not** judge algorithm questions. It opens 牛客 / 力扣 in the same
isolated browser profile the tracking panel already uses (the user logs in once by hand,
cookies stay on this machine), and it stores what the user reports afterwards. No page
scraping, no credentials, no automatic status reading: an unverified selector is worse
than an honest "record it yourself".
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Request, Response
from interview_agent.api.dependencies import PrincipalDep, errors
from pydantic import Field

from interview_agent.contracts.common import Contract
from interview_agent.storage.errors import ApiError

from .site_sync import _browser, _data_dir, _debug_port, _headless, PROFILE_NAME

CONFIG_NAME = 'practice-sites.json'

# The configured list is the only thing that may be opened: an arbitrary URL would turn a
# practice shortcut into a general browser launcher.
DEFAULT_SITES = [
    {
        'id': 'nowcoder',
        'label': '牛客',
        'blurb': '国内公司真题与面试题最多，校招笔试常在这里练',
        'urls': [
            {'label': '牛客 · 题库', 'url': 'https://www.nowcoder.com/exam/oj'},
            {'label': '牛客 · 公司真题', 'url': 'https://www.nowcoder.com/exam/company'},
            {'label': '牛客 · 面试题', 'url': 'https://www.nowcoder.com/interview/center'},
        ],
    },
    {
        'id': 'leetcode',
        'label': '力扣',
        'blurb': '题解与讨论最全，适合按专题刷 + 周赛',
        'urls': [
            {'label': '力扣 · 题库', 'url': 'https://leetcode.cn/problemset/'},
            {'label': '力扣 · 热题 100', 'url': 'https://leetcode.cn/studyplan/top-100-liked/'},
        ],
    },
    {
        'id': 'luogu',
        'label': '洛谷',
        'blurb': '算法基础到 NOI 向，题面严谨',
        'urls': [{'label': '洛谷 · 题目列表', 'url': 'https://www.luogu.com.cn/problem/list'}],
    },
    {
        'id': 'acwing',
        'label': 'AcWing',
        'blurb': '课程 + 题库，入门体系比较完整',
        'urls': [{'label': 'AcWing · 题库', 'url': 'https://www.acwing.com/problem/'}],
    },
    {
        'id': 'hackerrank',
        'label': 'HackerRank',
        'blurb': '英文题面，外企与海外岗位常见',
        'urls': [{'label': 'HackerRank · Algorithms', 'url': 'https://www.hackerrank.com/domains/algorithms'}],
    },
    {
        'id': 'acmcoder',
        'label': '赛码',
        'blurb': '不少公司在线笔试用的平台，界面接近真实考试',
        'urls': [{'label': '赛码 · 首页', 'url': 'https://www.acmcoder.com/'}],
    },
    {
        'id': 'lanqiao',
        'label': '蓝桥',
        'blurb': '蓝桥杯与校招笔试真题，偏工程与模拟',
        'urls': [{'label': '蓝桥 · 题库', 'url': 'https://www.lanqiao.cn/problems/'}],
    },
    {
        'id': 'patest',
        'label': 'PAT',
        'blurb': '浙大 PAT，部分企业认可的成绩',
        'urls': [{'label': 'PAT · 首页', 'url': 'https://www.patest.cn/'}],
    },
    {
        'id': 'codeforces',
        'label': 'Codeforces',
        # Cloudflare answers 403 to scripted clients (checked 2026-11-13); a real browser
        # window opens it normally, and this product only launches the window.
        'blurb': '国际竞赛，难度高、题解质量好',
        'urls': [{'label': 'Codeforces · Problemset', 'url': 'https://codeforces.com/problemset'}],
    },
    {
        'id': 'atcoder',
        'label': 'AtCoder',
        'blurb': '日本竞赛，题目干净、难度分层清晰',
        'urls': [{'label': 'AtCoder · Contests', 'url': 'https://atcoder.jp/contests/'}],
    },
    {
        'id': 'leetcode-global',
        'label': 'LeetCode 国际',
        'blurb': '英文原题与面经，海外岗位常用',
        'urls': [{'label': 'LeetCode · Problemset', 'url': 'https://leetcode.com/problemset/'}],
    },
]

SITES_CHECKED = '2026-11-13'

OUTCOMES = ('SOLVED', 'PARTIAL', 'UNSOLVED', 'REVIEWED')


class OpenInput(Contract):
    site_id: Annotated[str, Field(min_length=1, max_length=40)]
    url: Annotated[str, Field(min_length=1, max_length=300)]


class RecordInput(Contract):
    request_id: Annotated[str, Field(min_length=1, max_length=80)]
    site_id: Annotated[str, Field(min_length=1, max_length=40)]
    url: Annotated[str, Field(min_length=1, max_length=300)]
    title: Annotated[str, Field(min_length=1, max_length=120)]
    # Contract is strict, so a JSON string is not coerced into `date`; parse explicitly.
    practiced_on: Annotated[str, Field(pattern=r'^\d{4}-\d{2}-\d{2}$')]
    outcome: Literal['SOLVED', 'PARTIAL', 'UNSOLVED', 'REVIEWED']
    note: Annotated[str, Field(max_length=1000)] = ''
    # Pasted by the user, because the product never reads the site page. 4000 chars is a
    # typical problem statement; longer descriptions should be shortened by the user.
    statement: Annotated[str, Field(max_length=4000)] = ''


def _config_path(request: Request) -> Path:
    override = os.environ.get('INTERVIEW_PRACTICE_SITES')
    if override:
        return Path(override)
    return _data_dir(request) / CONFIG_NAME


def _sites(request: Request) -> list[dict]:
    path = _config_path(request)
    if not path.is_file():
        return [dict(site) for site in DEFAULT_SITES]
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return [dict(site) for site in DEFAULT_SITES]
    items = value.get('sites') if isinstance(value, dict) else None
    return items if isinstance(items, list) and items else [dict(site) for site in DEFAULT_SITES]


def _find(sites: list[dict], site_id: str) -> dict:
    for site in sites:
        if site.get('id') == site_id:
            return site
    raise ApiError('practice_site_not_found', 404)


def _approved(site: dict, url: str) -> bool:
    return any(item.get('url') == url for item in site.get('urls') or [])


def _launch(url: str, profile: Path) -> None:
    """Open one configured page in the shared profile. Nothing is read from the page."""
    executable = _browser()
    args = [executable, '--remote-debugging-port=0', '--remote-allow-origins=*',
            '--no-first-run', '--no-default-browser-check',
            f'--user-data-dir={profile}', url]
    if _headless():
        args.insert(1, '--headless=new')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, creationflags=flags)
    for _ in range(50):
        if _debug_port(profile) is not None:
            return
        time.sleep(0.2)
    raise ApiError('practice_browser_timeout', 504)


def routes():
    router = APIRouter(prefix='/api/desktop/practice-sites')
    @router.get('', responses=errors(401))
    async def listing(request: Request, user:PrincipalDep):
        profile = (_data_dir(request) / PROFILE_NAME).resolve()
        return {'items': _sites(request), 'profile': str(profile),
                'headless': _headless(), 'outcomes': list(OUTCOMES),
                'checked_on': SITES_CHECKED,
                'note': '不代替登录：首次打开请在窗口里手动登录一次，Cookie 只留在本机独立配置目录；'
                        '应用不读取题库、不判题、不保存账号密码。'}

    @router.post('/open')
    async def open_site(request: Request, value: OpenInput, user:PrincipalDep):
        site = _find(_sites(request), value.site_id)
        if not _approved(site, value.url):
            raise ApiError('practice_url_not_allowed', 400)
        profile = (_data_dir(request) / PROFILE_NAME).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        _launch(value.url, profile)
        return {'opened': True, 'site_id': value.site_id, 'url': value.url}

    return router


# --------------------------------------------------------------------------- records

async def _goal(conn, user_id, goal_id):
    row = await (await conn.execute(
        'SELECT id,user_id,status FROM app.career_goals WHERE id=%s::uuid', (goal_id,))).fetchone()
    if row is None or str(row['user_id']) != str(user_id):
        raise ApiError('goal_not_found', 404)
    return row


def record_routes():
    router = APIRouter(prefix='/api/career-goals')
    @router.get('/{goal_id}/practice-records')
    async def listing(goal_id: str, request: Request, user:PrincipalDep):
        sites = _sites(request)
        pool = request.app.state.pool
        async with pool.connection() as conn:
            await _goal(conn, user.user_id, goal_id)
            # F07Z-60: show the last coaching walkthrough for each record. The explanation lives
            # in the interview events (immutable), not in the record, so this is a read-only join
            # keyed by the practice topic id `pm-<record uuid>`; the session join keeps it inside
            # the same goal.
            rows = await (await conn.execute(
                'SELECT r.id,r.site_id,r.site_label,r.url,r.title,r.practiced_on,r.outcome,r.note,'
                'r.statement,r.created_at,e.payload->>\'text\' AS explanation_text,e.payload->>\'origin\' AS explanation_origin,'
                'e.created_at AS explained_at '
                'FROM app.career_practice_records r '
                "LEFT JOIN LATERAL (SELECT ev.payload,ev.created_at,ev.session_id "
                'FROM app.career_interview_events ev '
                'JOIN app.career_interviews s ON s.id=ev.session_id AND s.goal_id=r.goal_id '
                "WHERE ev.kind='explanation' AND ev.payload->>'topic_id'='pm-'||r.id::text "
                'ORDER BY ev.created_at DESC,ev.seq DESC LIMIT 1) e ON true '
                'WHERE r.goal_id=%s::uuid ORDER BY r.practiced_on DESC,r.created_at DESC LIMIT 50',
                (goal_id,))).fetchall()
        items=[]
        for row in rows:
            item={**{k: row[k] for k in ('site_id', 'site_label', 'url', 'title', 'outcome',
                                         'note', 'statement')},
                  'id': str(row['id']),
                  'practiced_on': row['practiced_on'].isoformat(),
                  'created_at': row['created_at'].isoformat(),
                  'explanation': ({'text': row['explanation_text'],
                                   'origin': row['explanation_origin'] or 'unknown',
                                   'created_at': row['explained_at'].isoformat()}
                                  if row['explanation_text'] else None)}
            items.append(item)
        return {'items': items,
                'sites': [{'id': s['id'], 'label': s['label']} for s in sites]}

    @router.post('/{goal_id}/practice-records')
    async def create(goal_id: str, value: RecordInput, request: Request,
                     response: Response, user:PrincipalDep):
        site = _find(_sites(request), value.site_id)
        if not _approved(site, value.url):
            raise ApiError('practice_url_not_allowed', 400)
        try:
            practiced_on = date.fromisoformat(value.practiced_on)
        except ValueError:
            raise ApiError('practice_date_invalid', 400) from None
        if practiced_on > date.today():
            raise ApiError('practice_date_in_future', 400)
        pool = request.app.state.pool
        async with pool.connection() as conn, conn.transaction():
            await _goal(conn, user.user_id, goal_id)
            existing = await (await conn.execute(
                'SELECT id FROM app.career_practice_records WHERE goal_id=%s::uuid AND request_id=%s',
                (goal_id, value.request_id))).fetchone()
            if existing:
                return {'record_id': str(existing['id']), 'created': False}
            row = await (await conn.execute(
                'INSERT INTO app.career_practice_records'
                '(goal_id,request_id,site_id,site_label,url,title,practiced_on,outcome,note,statement) '
                'VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                (goal_id, value.request_id, value.site_id, site['label'], value.url, value.title.strip(),
                 practiced_on, value.outcome, value.note, value.statement.strip()))).fetchone()
        response.status_code = 201
        return {'record_id': str(row['id']), 'created': True}

    @router.delete('/{goal_id}/practice-records/{record_id}')
    async def delete(goal_id: str, record_id: str, request: Request,
                     user:PrincipalDep):
        # A practice log is the user's own note, so deleting it removes the row; the
        # immutability trigger still forbids editing one in place.
        pool = request.app.state.pool
        async with pool.connection() as conn, conn.transaction():
            await _goal(conn, user.user_id, goal_id)
            row = await (await conn.execute(
                'DELETE FROM app.career_practice_records '
                'WHERE id=%s::uuid AND goal_id=%s::uuid RETURNING id',
                (record_id, goal_id))).fetchone()
        if row is None:
            raise ApiError('practice_record_not_found', 404)
        return {'deleted': True, 'record_id': str(row['id'])}

    return router
