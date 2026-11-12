#!/usr/bin/env python3
"""Validate debs and build an atomic GitHub Pages/APT artifact."""
import argparse
import bz2
import datetime
import email.utils
import functools
import hashlib
import json
import lzma
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Existing upstream packages only. Never silently accept new incomplete packages.
LEGACY_MISSING = {
    ('cn.wkk.swipeextenderx', '0.0.2'): {'Description'},
    ('llld.keyboard', '2.0'): {'Maintainer'},
}


def run(*args):
    return subprocess.check_output(args, text=True)


def parse(text):
    fields = {}
    field = None
    for line in text.replace('\r\n', '\n').split('\n'):
        if line.startswith((' ', '\t')):
            if field == 'Description':
                continuation = line[1:]
                fields[field] += '\n' + ('' if continuation == '.' else continuation)
            continue
        if not line:
            field = None
            continue
        key, sep, value = line.partition(':')
        if not sep:
            raise ValueError(f'Invalid control line: {line!r}')
        fields[key] = value.strip()
        field = key
    return fields


def compare_versions(left, right):
    for operator, result in (('lt', -1), ('gt', 1)):
        status = subprocess.run(['dpkg', '--compare-versions', left, operator, right])
        if status.returncode == 0:
            return result
        if status.returncode != 1:
            status.check_returncode()
    return 0


def validate_sections(sections):
    if not isinstance(sections, dict):
        raise ValueError('upstreams.json: sections must map Package IDs to Sections')
    for package, section in sections.items():
        if not isinstance(package, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9+.-]+', package):
            raise ValueError(f'upstreams.json: invalid Package {package!r}')
        if not isinstance(section, str) or not section.strip() or any(ord(c) < 32 for c in section):
            raise ValueError(f'upstreams.json: invalid Section for {package}')


def validate_apt_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in url)):
        raise ValueError('APT 地址必须是公开 HTTPS 目录，不含账号、查询参数或片段')
    return url.rstrip('/') + '/'


def validate_sources(sources):
    if not isinstance(sources, list):
        raise ValueError('upstreams.json: sources must be a list')
    seen = set()
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get('repo'), str):
            raise ValueError('upstreams.json: invalid repository')
        if source.get('type', 'github') == 'apt':
            validate_apt_url(source['repo'])
        elif source.get('type', 'github') != 'github' or not re.fullmatch(
                r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', source['repo']):
            raise ValueError('upstreams.json: invalid GitHub repository or source type')
        if not isinstance(source.get('packages'), dict) or not source['packages']:
            raise ValueError('upstreams.json: packages must map Package IDs to Sections')
        validate_sections(source['packages'])
        for package in source['packages']:
            if package in seen:
                raise ValueError(f'upstreams.json: invalid or duplicate Package {package!r}')
            seen.add(package)
    return sources


def load_upstreams(root):
    path = root / 'upstreams.json'
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('upstreams.json: expected an object')
    return validate_sources(data.get('sources'))


def load_sections(root):
    sections = {package: section for source in load_upstreams(root)
                for package, section in source['packages'].items()}
    path = root / 'upstreams.json'
    manual = json.loads(path.read_text(encoding='utf-8')).get('manual_sections', {}) if path.exists() else {}
    validate_sections(manual)
    for package, section in manual.items():
        if package in sections and sections[package] != section:
            raise ValueError(f'分组与批量配置冲突: {package}')
    return {**sections, **manual}


def build(output):
    if output == ROOT or ROOT not in output.parents:
        raise ValueError('Output must be a subdirectory of the repository')
    packages = sorted((ROOT / 'debs').glob('*.deb'))
    if not packages:
        raise ValueError('No deb packages found')
    sections = load_sections(ROOT)
    seen = set()
    releases = {}
    for package in packages:
        fields = parse(run('dpkg-deb', '--field', str(package)))
        missing = {f for f in ('Package', 'Version', 'Architecture', 'Maintainer', 'Description') if not fields.get(f, '').strip()}
        allowed = LEGACY_MISSING.get((fields.get('Package'), fields.get('Version')), set())
        if missing - allowed:
            raise ValueError(f'{package.name}: missing {sorted(missing - allowed)}')
        if missing:
            print(f'WARNING: legacy package {package.name}: missing {sorted(missing)}', file=sys.stderr)
        if fields['Architecture'] != 'iphoneos-arm64e':
            raise ValueError(f'{package.name}: unexpected architecture {fields["Architecture"]}')
        identity = tuple(fields[f] for f in ('Package', 'Version', 'Architecture'))
        if identity in seen:
            raise ValueError(f'Duplicate package/version/architecture: {identity}')
        seen.add(identity)
        date_field = 'X-WM0104-Release-Date'
        release_date = fields.get(date_field)
        if release_date is not None:
            try:
                if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', release_date):
                    raise ValueError
                datetime.date.fromisoformat(release_date)
            except ValueError:
                raise ValueError(f'{package.name}: invalid {date_field}') from None
        field = 'X-WM0104-Changelog'
        if field not in fields and fields['Package'] == 'com.wm0104.repopocket':
            field = 'X-RepoPocket-Changelog'
        if release_date is not None or field in fields:
            if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]+', fields['Package']):
                raise ValueError(f'{package.name}: invalid Package')
            date, changes = release_date, ''
            if field in fields:
                entry = fields[field]
                version, sep, rest = entry.partition(' | ')
                date, sep2, changes = rest.partition(' | ')
                if version != fields['Version'] or not sep or not sep2 or not date or not changes:
                    raise ValueError(f'{package.name}: invalid {field}')
            releases.setdefault(fields['Package'], {})[fields['Version']] = (
                release_date or date, changes, fields['Description'])
        # Read the complete data archive to catch broken/truncated debs, not just control metadata.
        subprocess.run(['dpkg-deb', '--contents', str(package)], stdout=subprocess.DEVNULL, check=True)

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    shutil.copytree(ROOT / 'debs', output / 'debs')
    for name in ('index.html', 'CydiaIcon.png', 'README.md'):
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, output / name)
    for name in ('depictions', 'assets'):
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, output / name)
    (output / '.nojekyll').touch()
    for package_id, history in releases.items():
        depiction = output / 'depictions' / package_id / 'depiction.json'
        depiction.parent.mkdir(parents=True, exist_ok=True)
        versions = sorted(history, key=functools.cmp_to_key(compare_versions), reverse=True)
        views = []
        for version in versions:
            date, changes, _ = history[version]
            if not changes:
                continue
            views.append({'class': 'DepictionStackView', 'orientation': 'landscape', 'views': [
                {'class': 'DepictionLabelView', 'text': version, 'fontSize': 17,
                 'fontWeight': 'bold', 'alignment': 0},
                {'class': 'DepictionLabelView', 'text': date.replace('-', '.'), 'fontSize': 17,
                 'fontWeight': 'bold', 'textColor': '#707070', 'alignment': 2},
            ]})
            views.append({'class': 'DepictionMarkdownView', 'markdown':
                '\n'.join(f'- {item.strip()}' for item in changes.split('；') if item.strip())})
        if not views:
            views.append({'class': 'DepictionLabelView', 'text': '无法查看此软件包的更新日志。'})
        depiction.write_text(json.dumps({
            'class': 'DepictionTabView', 'minVersion': '0.4', 'tabs': [
                {'class': 'DepictionStackView', 'tabname': '插件详细', 'views': [
                    {'class': 'DepictionMarkdownView', 'markdown': history[versions[0]][2], 'useSpacing': True}]},
                {'class': 'DepictionStackView', 'tabname': '更新', 'views': views},
            ]}, ensure_ascii=False, indent=2) + '\n')
    result = subprocess.run(['dpkg-scanpackages', '--multiversion', 'debs', '/dev/null'], cwd=output, capture_output=True, text=True, check=True)
    print(result.stderr, file=sys.stderr, end='')
    # Compute hashes with Python for identical results across host toolchains.
    blocks = []
    for block in result.stdout.strip().split('\n\n'):
        record = parse(block)
        file = output / record['Filename']
        if file.parent != output / 'debs':
            raise ValueError('Unexpected index path')
        content = file.read_bytes()
        if len(content) != int(record['Size']):
            raise ValueError(f'Index size mismatch: {file.name}')
        lines = [line for line in block.replace('\r\n', '\n').split('\n') if not line.lower().startswith(
            ('md5sum:', 'sha1:', 'sha256:', 'x-repopocket-changelog:', 'x-wm0104-changelog:',
             'x-wm0104-release-date:'))]
        if record['Version'] in releases.get(record['Package'], {}):
            lines = [line for line in lines if not line.lower().startswith('sileodepiction:')]
            lines.append(f'SileoDepiction: https://wm0104.github.io/repo/depictions/{record["Package"]}/depiction.json')
        section = sections.get(record['Package'])
        if section is not None and record.get('Section') != section:
            lines = [line for line in lines if not line.lower().startswith('section:')]
            lines.append(f'Section: {section}')
        for field, algorithm in [('MD5sum', 'md5'), ('SHA1', 'sha1'), ('SHA256', 'sha256')]:
            lines.append(f'{field}: {hashlib.new(algorithm, content).hexdigest()}')
        blocks.append('\n'.join(lines))
    data = ('\n\n'.join(blocks) + '\n\n').encode()
    if len(blocks) != len(packages):
        raise ValueError('Index/package count mismatch')
    (output / 'Packages').write_bytes(data)
    (output / 'Packages.bz2').write_bytes(bz2.compress(data, compresslevel=9))
    (output / 'Packages.xz').write_bytes(lzma.compress(data, preset=9))
    subprocess.run(['zstd', '-q', '-19', '-k', str(output / 'Packages'), '-o', str(output / 'Packages.zst')], check=True)
    if bz2.decompress((output / 'Packages.bz2').read_bytes()) != data or lzma.decompress((output / 'Packages.xz').read_bytes()) != data:
        raise ValueError('Compressed index mismatch')
    if subprocess.check_output(['zstd', '-q', '-d', '-c', str(output / 'Packages.zst')]) != data:
        raise ValueError('Zstandard index mismatch')
    release = [
        'Origin: wm0104(roothide)', 'Label: wm0104', 'Suite: stable',
        'Version: 1.0', 'Codename: ios', 'Architectures: iphoneos-arm64e',
        'Components: main', 'Description: wm0104 RootHide plugin backup repository',
        'Date: ' + email.utils.formatdate(usegmt=True), 'SHA256:',
    ]
    for name in ('Packages', 'Packages.bz2', 'Packages.xz', 'Packages.zst'):
        content = (output / name).read_bytes()
        release.append(f' {hashlib.sha256(content).hexdigest()} {len(content)} {name}')
    (output / 'Release').write_text('\n'.join(release) + '\n')
    print(f'Validated {len(packages)} packages; site ready at {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='_site')
    args = parser.parse_args()
    try:
        build((ROOT / args.output).resolve())
    except (ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(f'ERROR: {exc}')
