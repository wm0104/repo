"""Read public HTTPS flat APT indexes without executing or repacking packages."""
import bz2
import functools
import gzip
import io
import lzma
import pathlib
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from build_repo import compare_versions, parse, validate_apt_url

MAX_INDEX = 32 * 1024 * 1024


def read_index(repo):
    base = validate_apt_url(repo)
    for suffix, opener in [('.xz', lzma.LZMAFile), ('.bz2', bz2.BZ2File), ('.gz', gzip.GzipFile), ('', None)]:
        try:
            request = urllib.request.Request(base + 'Packages' + suffix, headers={'User-Agent': 'wm0104-repo-sync'})
            with urllib.request.urlopen(request, timeout=60) as response:
                if urllib.parse.urlsplit(response.geturl()).scheme != 'https':
                    raise ValueError('APT 索引重定向到非 HTTPS 地址')
                data = response.read(MAX_INDEX + 1)
            if len(data) > MAX_INDEX:
                raise ValueError('APT 压缩索引超过大小限制')
            if opener is not None:
                stream = io.BytesIO(data)
                archive = opener(fileobj=stream) if suffix == '.gz' else opener(stream)
                with archive:
                    data = archive.read(MAX_INDEX + 1)
            if len(data) > MAX_INDEX:
                raise ValueError('APT 解压索引超过大小限制')
            return data.decode('utf-8-sig').replace('\r\n', '\n')
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        except (lzma.LZMAError, EOFError, UnicodeError) as exc:
            raise ValueError('APT 索引损坏: ' + str(exc)) from exc
    raise ValueError('找不到平铺 Packages 索引；当前不支持需额外 suite/component 的分发目录')


def latest_assets(repo, packages, architecture):
    grouped = {}
    for block in re.split(r'\n[ \t]*\n', read_index(repo).strip()):
        if not block:
            continue
        fields = parse(block)
        package = fields.get('Package')
        if (packages is not None and package not in packages) or fields.get('Architecture') != architecture:
            continue
        grouped.setdefault(package, []).append(fields)
    assets = []
    for package, entries in grouped.items():
        try:
            if any(not entry.get('Version') for entry in entries):
                raise ValueError('索引缺少 Version')
            # Validate even a single Version using dpkg before trusting it.
            for entry in entries:
                compare_versions(entry['Version'], entry['Version'])
            latest = max(entries, key=functools.cmp_to_key(
                lambda left, right: compare_versions(left['Version'], right['Version'])))
            if any(compare_versions(entry['Version'], latest['Version']) == 0 and entry != latest for entry in entries):
                raise ValueError('最高版本存在多条不一致索引')
            assets.append({'name': package + '.deb', 'record': latest})
        except (ValueError, subprocess.SubprocessError) as exc:
            assets.append({'name': package + '.deb', 'package': package,
                           'lookup_names': [entry.get('Name', '') for entry in entries], 'error': str(exc)})
    return {'tag_name': 'APT 最新索引', 'assets': assets}


def asset_metadata(repo, fields):
    filename = fields.get('Filename', '')
    parsed = urllib.parse.urlsplit(filename)
    decoded = urllib.parse.unquote(parsed.path)
    if (not filename or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
            or decoded.startswith('/') or '\\' in decoded or '..' in decoded.split('/')
            or any(ord(c) < 32 for c in decoded)):
        raise ValueError('APT Filename 必须是源内安全的相对路径')
    name = pathlib.PurePosixPath(decoded).name
    if not name.endswith('.deb'):
        raise ValueError('APT Filename 不是 deb')
    digest = fields.get('SHA256', '')
    if not re.fullmatch(r'[a-fA-F0-9]{64}', digest) or not fields.get('Size', '').isdigit():
        raise ValueError('APT 包缺少有效 SHA256 或 Size，拒绝下载')
    encoded = urllib.parse.quote(filename, safe='/%:@+')
    return urllib.parse.urljoin(validate_apt_url(repo), encoded), name, int(fields['Size']), digest.lower()
