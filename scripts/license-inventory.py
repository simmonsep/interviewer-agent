"""Inventory installed locked Python distributions; declarations are not legal clearance.

Run from the repository venv. This reads package metadata/license files, not keys,
and creates a new private review directory. It does not choose the project license.
"""
import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import sys
import tomllib
from uuid import uuid4


def normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def inventory(root):
    lock = root / 'uv.lock'
    packages = tomllib.loads(lock.read_text(encoding='utf-8'))['package']
    installed = {normalized(d.metadata['Name']): d for d in metadata.distributions()}
    rows = []
    for package in packages:
        name = package['name']
        if package.get('source', {}).get('editable') == '.':
            continue
        dist = installed.get(normalized(name))
        row = {'name': name, 'locked_version': package['version'], 'installed_version': dist.version if dist else None,
               'version_matches': dist is not None and dist.version == package['version']}
        row['incoming_dependency_markers'] = [{'from': p['name'], 'marker': edge.get('marker')}
            for p in packages for edge in p.get('dependencies', []) if normalized(edge['name']) == normalized(name)]
        files = []
        if dist:
            row.update(license_expression=dist.metadata.get('License-Expression'),
                       license_declaration=dist.metadata.get('License'),
                       license_classifiers=[x for x in dist.metadata.get_all('Classifier', []) if x.startswith('License ::')],
                       declared_license_files=dist.metadata.get_all('License-File', []))
            for entry in dist.files or []:
                relative = Path(str(entry))
                if any(x in relative.name.lower() for x in ('license', 'copying', 'notice')) or 'licenses' in relative.parts:
                    path = Path(dist.locate_file(entry))
                    if path.is_file():
                        files.append({'path_in_distribution': str(entry).replace('\\', '/'),
                                      'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size})
        row['discovered_notice_files'] = files
        rows.append(row)
    return {'scope': 'installed locked Python distributions only; no frontend, container OS/JDK, Git history or upstream source clearance',
        'python': sys.version, 'lock_sha256': hashlib.sha256(lock.read_bytes()).hexdigest(),
        'project_root_license_present': (root / 'LICENSE').is_file(), 'project_license_chosen': False,
        'distribution_approved': False, 'packages': rows,
        'counts': {'locked_distributions': len(rows), 'installed_matching': sum(r['version_matches'] for r in rows),
                   'with_discovered_notice_files': sum(bool(r['discovered_notice_files']) for r in rows)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    report = inventory(root)
    out = root / '.private/ja-09-04/license-inventory' / uuid4().hex
    out.mkdir(parents=True)
    (out / 'inventory.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(out), 'counts': report['counts'], 'distribution_approved': False}))


if __name__ == '__main__':
    main()
