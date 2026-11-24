"""Collect exact notice bytes for installed locked Python distributions, offline.

This produces review/distribution materials; it does not approve a release.
Supplemental upstream files require a matching package/version and SHA256.
"""
import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import runpy
from uuid import uuid4


def package_notices(root, output, supplements=()):
    inventory = runpy.run_path(str(root / 'scripts/license-inventory.py'))['inventory'](root)
    installed = {re.sub(r'[-_.]+', '-', d.metadata['Name']).lower(): d for d in metadata.distributions()}
    extra = {}
    for item in supplements:
        key = (item['package'], item['version'])
        if key in extra:
            raise ValueError('duplicate_notice_supplement')
        data = Path(item['local_path']).read_bytes()
        if hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('notice_supplement_checksum_mismatch')
        extra[key] = (item, data)
    expected = {(p['name'], p['locked_version']) for p in inventory['packages'] if p['version_matches']}
    if not set(extra) <= expected:
        raise ValueError('notice_supplement_package_mismatch')
    # New directory only: never overwrite an earlier evidence or user directory.
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    copied = 0
    for package in inventory['packages']:
        row = {k: package.get(k) for k in ('name', 'locked_version', 'installed_version', 'version_matches',
            'license_expression', 'license_declaration', 'license_classifiers', 'incoming_dependency_markers')}
        row['files'] = []
        name = re.sub(r'[^a-zA-Z0-9_.-]', '_', package['name'] + '-' + package['locked_version'])
        for index, source in enumerate(package['discovered_notice_files']):
            dist = installed[re.sub(r'[-_.]+', '-', package['name']).lower()]
            data = Path(dist.locate_file(source['path_in_distribution'])).read_bytes()
            if hashlib.sha256(data).hexdigest() != source['sha256']:
                raise ValueError('notice_file_changed_since_inventory')
            filename = re.sub(r'[^a-zA-Z0-9_.-]', '_', Path(source['path_in_distribution']).name)
            target = output / 'licenses' / name / (str(index).zfill(2) + '-' + filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            row['files'].append({'path': target.relative_to(output).as_posix(), 'sha256': source['sha256'],
                'source_path_in_distribution': source['path_in_distribution'], 'source_kind': 'installed_distribution'})
            copied += 1
        supplement = extra.get((package['name'], package['locked_version']))
        if supplement:
            source, data = supplement
            target = output / 'licenses' / name / 'upstream-LICENSE.txt'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            row['files'].append({'path': target.relative_to(output).as_posix(), 'sha256': source['sha256'],
                'source_kind': 'pinned_upstream_supplement', 'source_url': source['source_url'], 'source_commit': source['source_commit']})
            copied += 1
        rows.append(row)
    report = {'scope': inventory['scope'], 'lock_sha256': inventory['lock_sha256'],
        'distribution_approved': False, 'project_license_selected_by_this_tool': False,
        'packages': rows, 'counts': {'locked_distributions': len(rows),
            'installed_matching': sum(p['version_matches'] for p in rows),
            'installed_with_notice_materials': sum(p['version_matches'] and bool(p['files']) for p in rows),
            'copied_notice_files': copied},
        'excluded_scope': ['project license decision', 'frontend npm tree', 'Python interpreter', 'container OS/JDK',
                           'upstream copied source/history', 'uninstalled platform-specific dependencies', 'complete native dependency/legal clearance']}
    (output / 'manifest.json').write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode())
    lines = ['# Python依赖通知材料包', '', '本包复制当前锁定并已安装分发包中的通知原始字节，另有注明来源的上游补充。',
        '这是分发准备材料，不是完整发行放行或许可证兼容结论。项目许可、前端、Python解释器及容器等范围另核。', '',
        '| 包 | 版本 | metadata声明 | 通知文件数 |', '| --- | --- | --- | --- |']
    for row in rows:
        lines.append(f"| {row['name']} | {row['locked_version']} | {row['license_expression'] or '见manifest原始声明'} | {len(row['files'])} |")
    (output / 'README.md').write_bytes(('\n'.join(lines) + '\n').encode())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path)
    parser.add_argument('--supplements', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output or root / '.private/ja-09-05/python-notices' / uuid4().hex
    items = json.loads(args.supplements.read_text(encoding='utf-8')) if args.supplements else []
    result = package_notices(root, output, items)
    print(json.dumps({'output': str(output), 'counts': result['counts'], 'distribution_approved': False}))


if __name__ == '__main__':
    main()
