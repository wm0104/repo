#!/usr/bin/env python3
"""Manage selected GitHub/APT packages, then request the existing publisher."""
import copy
import functools
import hashlib
import html
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from build_repo import compare_versions, load_sections, parse, validate_apt_url, validate_sections, validate_sources
from upstream_apt import asset_metadata, latest_assets

ROOT = pathlib.Path(__file__).resolve().parents[1]
ARCHITECTURE = 'iphoneos-arm64e'
MAX_DEB = 512 * 1024 * 1024


def checked(args, cwd=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or f'Command failed: {args[0]}').strip())
    return result.stdout.strip()


def latest_release(repo):
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'wm0104-repo-sync'}
    if os.environ.get('GH_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GH_TOKEN']
    request = urllib.request.Request(f'https://api.github.com/repos/{repo}/releases/latest', headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        release = json.load(response)
    if release.get('draft') or release.get('prerelease'):
        raise ValueError(f'{repo}: 只同步正式发布')
    return release


def download_asset(asset, target):
    url = urllib.parse.urlsplit(asset['browser_download_url'])
    if url.scheme != 'https' or url.netloc != 'github.com' or '/releases/download/' not in url.path:
        raise ValueError('下载地址不是 GitHub Release 资产')
    digest = asset.get('digest') or ''
    if not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', digest):
        raise ValueError(f'{asset["name"]}: 缺少 GitHub SHA256，拒绝未验证下载')
    download_verified(asset['browser_download_url'], target, asset.get('size'), digest.split(':', 1)[1], asset['name'])


def download_verified(url, target, size, digest, name):
    if type(size) is not int or not 0 < size <= MAX_DEB:
        raise ValueError(f'{name}: 安装包大小无效')
    sha, total = hashlib.sha256(), 0
    request = urllib.request.Request(url, headers={'User-Agent': 'wm0104-repo-sync'})
    with urllib.request.urlopen(request, timeout=60) as response, target.open('wb') as output:
        if urllib.parse.urlsplit(response.geturl()).scheme != 'https':
            raise ValueError('安装包重定向到非 HTTPS 地址')
        while chunk := response.read(256 * 1024):
            total += len(chunk)
            if total > size:
                raise ValueError(f'{name}: 下载超过声明大小')
            sha.update(chunk)
            output.write(chunk)
    if total != size or sha.hexdigest() != digest.lower():
        raise ValueError(f'{name}: 大小或 SHA256 不匹配')


def package_info(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_DEB:
        raise ValueError(f'{path.name}: 不是有效的普通 deb 文件')
    fields = parse(checked(['dpkg-deb', '--field', str(path)]))
    if any(not fields.get(key) for key in ('Package', 'Version', 'Architecture')):
        raise ValueError(f'{path.name}: 缺少包身份字段')
    return fields


def snapshot(root):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / 'debs').glob('*.deb')}


def preflight(root, workspace, plans, config_update=None):
    stage = workspace / 'checkout'
    stage.mkdir()
    for name in ('debs', 'scripts', 'depictions', 'assets'):
        if (root / name).is_dir():
            shutil.copytree(root / name, stage / name)
    for name in ('index.html', 'CydiaIcon.png', 'README.md', 'upstreams.json'):
        if (root / name).is_file():
            shutil.copy2(root / name, stage / name)
    if config_update is not None:
        (stage / 'upstreams.json').write_bytes(config_update)
    for path, name in plans:
        shutil.copyfile(path, stage / 'debs' / name)
    report_path = stage / 'audit.json'
    result = subprocess.run([sys.executable, str(stage / 'scripts/repair_deb_permissions.py'),
                             '--root', str(stage), '--report', str(report_path)],
                            capture_output=True, text=True)
    if not report_path.exists():
        raise ValueError(result.stderr or '候选工作区审计失败')
    report = json.loads(report_path.read_text(encoding='utf-8'))
    if result.returncode or not report['applied'] or report['blocking']:
        raise ValueError('候选工作区审计失败: ' + json.dumps(report['blocking'], ensure_ascii=False))
    if report['repaired_paths']:
        raise ValueError('候选工作区需要权限修复，停止同步: ' + ', '.join(report['repaired_paths']))
    for path, name in plans:
        if path.read_bytes() != (stage / 'debs' / name).read_bytes():
            raise ValueError(f'{name}: 审计改变了上游安装包')
    print(checked([sys.executable, str(stage / 'scripts/build_repo.py'), '--output', '_site']))


def package_list(text):
    if not text:
        return []
    packages = [item.strip() for item in text.split(',')]
    validate_sections({package: '_' for package in packages})
    if len(packages) != len(set(packages)):
        raise ValueError('Package 列表不能重复')
    return packages


def registrations(data):
    return {package: (source['repo'], section) for source in data['sources']
            for package, section in source['packages'].items()}


def source_key(repo):
    return validate_apt_url(repo) if repo.startswith('https://') else repo.casefold()


def register(data, repo, package, section, source_type='github'):
    source = next((item for item in data['sources'] if source_key(item['repo']) == source_key(repo)), None)
    for item in data['sources']:
        if item is not source:
            item['packages'].pop(package, None)
    data['sources'] = [item for item in data['sources'] if item['packages'] or item is source]
    if source is None:
        source = {'repo': repo, 'packages': {}}
        if source_type != 'github':
            source['type'] = source_type
        data['sources'].append(source)
    source['packages'][package] = section
    data.get('manual_sections', {}).pop(package, None)


def select_operation(data, action, scope, repo, packages, section, source_type):
    selected = package_list(packages)
    known = registrations(data)
    if action in ('add', 'modify'):
        validate_sources([{'repo': repo, 'type': source_type, 'packages': {p: section for p in selected}}])
        for package in selected:
            if action == 'modify' and package not in known:
                raise ValueError(f'插件未登记，请先添加: {package}')
            if action == 'add' and package in known:
                old_repo, old_section = known[package]
                if source_key(old_repo) != source_key(repo) or old_section != section:
                    raise ValueError(f'已登记的来源或分组不同，请使用“修改登记”: {package}')
        return [{'repo': repo, 'type': source_type, 'packages': {p: section for p in selected}}]
    if action == 'remove':
        if not selected or repo or section:
            raise ValueError('停止同步只填写 Package 列表，不填仓库和分组')
        for package in selected:
            if package not in known:
                raise ValueError(f'插件未登记: {package}')
        return []
    if action == 'list':
        if repo or selected or section:
            raise ValueError('查看清单不需要填写仓库、Package 或分组')
        return []
    if action != 'update' or section:
        raise ValueError('操作无效，更新时不填写分组；改分组请使用“修改登记”')
    if scope == 'all':
        if repo or selected:
            raise ValueError('更新全部时请留空仓库和 Package，以免误更新全部')
    elif scope == 'repo':
        if selected or not any(source_key(item['repo']) == source_key(repo) for item in data['sources']):
            raise ValueError('请填写已登记仓库，Package 留空')
    elif scope == 'packages':
        if not selected or repo or any(p not in known for p in selected):
            raise ValueError('请填写已登记 Package 列表，仓库留空')
    else:
        raise ValueError('无效同步范围')
    sources = []
    for item in data['sources']:
        chosen = {p: s for p, s in item['packages'].items()
                  if scope == 'all' or scope == 'repo' and source_key(item['repo']) == source_key(repo)
                  or scope == 'packages' and p in selected}
        if chosen:
            sources.append({**item, 'packages': chosen})
    return sources


def validate_candidate(root, workspace, path):
    # Reuse the existing audit/build on one package before the full candidate build.
    with tempfile.TemporaryDirectory(prefix='validate-', dir=workspace) as directory:
        candidate = pathlib.Path(directory)
        (candidate / 'debs').mkdir()
        shutil.copy2(path, candidate / 'debs' / path.name)
        shutil.copytree(root / 'scripts', candidate / 'scripts')
        preflight(candidate, candidate, [])


def sync(root, action='update', scope='all', repo='', packages='', section='', source_type='github'):
    load_sections(root)
    config_path = root / 'upstreams.json'
    original_config = config_path.read_bytes() if config_path.exists() else None
    data = json.loads(original_config) if original_config is not None else {'sources': []}
    if (source_type == 'apt' and repo) or repo.startswith('https://'):
        repo = validate_apt_url(repo)
    sources = select_operation(data, action, scope, repo, packages, section, source_type)
    updated = copy.deepcopy(data)
    result = {'paths': [], 'config_update': None, 'errors': [], 'rows': []}

    def row(source, package, status, detail=''):
        item = {'repo': source['repo'], 'package': package, 'section': source['packages'].get(package, ''),
                'status': status, 'detail': detail}
        result['rows'].append(item)
        print(f'{status}: {source["repo"]} {package} {detail}')
        if status == '失败':
            result['errors'].append(f'{source["repo"]} {package}: {detail}')

    if action == 'list':
        for item in data['sources']:
            for package in item['packages']:
                row(item, package, '已登记')
        for package, saved_section in data.get('manual_sections', {}).items():
            if package not in registrations(data):
                row({'repo': '—', 'packages': {package: saved_section}}, package, '已停止同步')
        return result
    if action == 'remove':
        known = registrations(data)
        for package in package_list(packages):
            old_repo, old_section = known[package]
            for item in updated['sources']:
                item['packages'].pop(package, None)
            updated.setdefault('manual_sections', {})[package] = old_section
            row({'repo': old_repo, 'packages': {package: old_section}}, package, '已停止同步', '保留安装包和分组')
        updated['sources'] = [item for item in updated['sources'] if item['packages']]

    existing, identities = {}, set()
    for path in sorted((root / 'debs').glob('*.deb')):
        fields = package_info(path)
        identity = tuple(fields[key] for key in ('Package', 'Version', 'Architecture'))
        if identity in identities:
            raise ValueError(f'仓库已有重复包身份: {identity}')
        identities.add(identity)
        if fields['Architecture'] == ARCHITECTURE:
            existing.setdefault(fields['Package'], {})[fields['Version']] = path
    original = snapshot(root)
    plans = []
    with tempfile.TemporaryDirectory(prefix='.upstream-sync-', dir=root) as directory:
        workspace = pathlib.Path(directory)
        for source_index, source in enumerate(sources):
            try:
                is_apt = source.get('type', 'github') == 'apt'
                release = (latest_assets(source['repo'], source['packages'], ARCHITECTURE) if is_apt
                           else latest_release(source['repo']))
                if not isinstance(release.get('assets'), list):
                    raise ValueError('Release 资产列表无效')
            except (ValueError, OSError, urllib.error.URLError) as exc:
                for package in source['packages']:
                    row(source, package, '失败', str(exc))
                continue
            print(f'检查 {source["repo"]}: {release.get("tag_name", "latest")}')
            found = {}
            for index, asset in enumerate(release['assets']):
                name = asset.get('name', '')
                if not isinstance(name, str) or not name.endswith('.deb'):
                    continue
                try:
                    if 'error' in asset:
                        raise ValueError(asset['error'])
                    if pathlib.PurePosixPath(name).name != name or '\\' in name or any(ord(c) < 32 for c in name):
                        raise ValueError('不安全的 Release 文件名')
                    candidate = workspace / f'{source_index}-{index}.deb'
                    if is_apt:
                        url, name, size, digest = asset_metadata(source['repo'], asset['record'])
                        download_verified(url, candidate, size, digest, name)
                    else:
                        download_asset(asset, candidate)
                    fields = package_info(candidate)
                    if is_apt and any(fields[key] != asset['record'][key] for key in ('Package', 'Version', 'Architecture')):
                        raise ValueError('下载包真实身份/版本/架构与 APT 索引不一致')
                    package = fields['Package']
                    if package in source['packages'] and fields['Architecture'] == ARCHITECTURE:
                        found.setdefault(package, []).append((candidate, name, fields['Version']))
                except (ValueError, OSError, urllib.error.URLError) as exc:
                    row(source, name, '失败', '资产校验失败: ' + str(exc))
            for package, selected_section in source['packages'].items():
                matches = found.get(package, [])
                if len(matches) != 1:
                    row(source, package, '失败', f'缺少 {ARCHITECTURE} 包' if not matches else '多个资产匹配，拒绝选择')
                    continue
                candidate, name, version = matches[0]
                try:
                    history = existing.get(package, {})
                    comparison = 1
                    detail = version
                    if history:
                        latest = max(history, key=functools.cmp_to_key(compare_versions))
                        comparison = compare_versions(version, latest)
                        if comparison < 0:
                            detail = f'跳过较低版本: {version} < {latest}'
                        elif comparison == 0:
                            sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
                            detail = ('同版本异文件，保留仓库原包' if sha != original[history[latest].name]
                                      else '无需更新') + f': {version}'
                    if comparison > 0:
                        if (root / 'debs' / name).exists() or name in {p[1] for p in plans}:
                            raise ValueError(f'文件名冲突，拒绝覆盖: {name}')
                        validate_candidate(root, workspace, candidate)
                        plans.append((candidate, name))
                    elif action in ('add', 'modify'):
                        validate_candidate(root, workspace, candidate)
                    if action in ('add', 'modify'):
                        register(updated, source['repo'], package, selected_section, source.get('type', 'github'))
                    row(source, package, '待入库' if comparison > 0 else '跳过', detail)
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    row(source, package, '失败', str(exc))
        config_update = None
        if updated != data:
            config_update = (json.dumps(updated, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        if not plans and config_update is None:
            print('没有需要入库的新版本或配置变更')
            return result
        # This is the repository-wide gate: a failure here prevents the entire commit.
        preflight(root, workspace, plans, config_update)
        current_config = config_path.read_bytes() if config_path.exists() else None
        if original != snapshot(root) or original_config != current_config:
            raise ValueError('预检期间仓库或配置发生变化，停止同步')
        created = []
        try:
            for candidate, name in plans:
                target = root / 'debs' / name
                with target.open('xb') as output:
                    created.append(target)
                    with candidate.open('rb') as content:
                        shutil.copyfileobj(content, output)
            if config_update is not None:
                replacement = workspace / 'upstreams.json'
                replacement.write_bytes(config_update)
                replacement.replace(config_path)
        except BaseException:
            for path in created:
                path.unlink()
            raise
        result['paths'] = [f'debs/{name}' for _, name in plans] + (['upstreams.json'] if config_update is not None else [])
        result['config_update'] = config_update
        for item in result['rows']:
            if item['status'] == '待入库':
                item['status'] = '待提交'
        return result


def write_summary(result, operation):
    def cell(value):
        return html.escape(str(value)).replace('|', '&#124;').replace('\r', '').replace('\n', '<br>')

    lines = [f'## 插件管理：{cell(operation)}', '',
             '有失败项，请查看下表；已成功提交的有效更新不因个别失败回退。' if result['errors'] else '本次处理完成。', '',
             '| 上游仓库 | Package | 分组 | 结果 | 说明 |', '| --- | --- | --- | --- | --- |']
    for item in result['rows']:
        lines.append('| ' + ' | '.join(cell(item[key]) for key in ('repo', 'package', 'section', 'status', 'detail')) + ' |')
    if not result['rows']:
        lines.append('| — | — | — | 无记录 | 尚未登记插件 |')
    if result.get('publication'):
        lines += ['', result['publication']]
    text = '\n'.join(lines) + '\n'
    print(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as output:
            output.write(text)


def publish(root, paths, captured, repository, config_update=None):
    def git(*args):
        return checked(['git', *args], cwd=root)

    def current_remote():
        return git('ls-remote', 'origin', 'refs/heads/main').split()[0]

    if current_remote() != captured:
        raise ValueError('远端 main 已更新，请重新运行；拒绝覆盖新提交')
    if paths:
        deb_paths = [p for p in paths if p != 'upstreams.json']
        if 'upstreams.json' in paths:
            if config_update is None or (root / 'upstreams.json').read_bytes() != config_update:
                raise ValueError('配置与本次已验证的变更不一致，停止提交')
            load_sections(root)
        if any(not p.startswith('debs/') or '/' in p[5:] or '\\' in p or
               not p.endswith('.deb') or any(ord(c) < 32 for c in p) for p in deb_paths):
            raise ValueError('不安全的同步提交路径')
        if deb_paths and git('ls-files', '--', *deb_paths):
            raise ValueError('同步只允许新增文件，拒绝提交已有包的修改')
        git('config', 'user.name', 'github-actions[bot]')
        git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
        git('add', '--', *paths)
        staged = set(filter(None, git('diff', '--cached', '--name-only', '-z').split('\0')))
        if staged != set(paths):
            raise ValueError('暂存区包含未授权路径，停止提交')
        git('commit', '-m', '同步上游插件更新（已校验，保留旧版本）')
        if current_remote() != captured:
            raise ValueError('提交期间远端 main 已更新，拒绝推送')
        git('push', 'origin', 'HEAD:refs/heads/main')
    # Token pushes do not trigger on:push. Also retry publishing after a prior dispatch failure.
    checked(['gh', 'workflow', 'run', 'refresh.yml', '--repo', repository, '--ref', 'main'], cwd=root)
    print('已请求原有 Publish APT repository 工作流；请查看该工作流的发布结果')


OPERATIONS = {'更新插件': 'update', '添加并同步': 'add', '修改登记': 'modify', '停止同步': 'remove', '查看清单': 'list'}
SCOPES = {'全部': 'all', '指定仓库': 'repo', '指定插件': 'packages'}
SOURCE_TYPES = {'GitHub发行版': 'github', 'APT软件源': 'apt'}


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise ValueError('请在 GitHub Actions 的 main 分支手动运行同步工作流')
    if checked(['git', 'status', '--porcelain'], cwd=ROOT):
        raise ValueError('工作区不干净，停止同步')
    captured = checked(['git', 'rev-parse', 'HEAD'], cwd=ROOT)
    operation = os.environ.get('UPSTREAM_OPERATION', '查看清单')
    scope = os.environ.get('UPSTREAM_SCOPE', '全部')
    source_type = os.environ.get('UPSTREAM_TYPE', 'GitHub发行版')
    if operation not in OPERATIONS or scope not in SCOPES or source_type not in SOURCE_TYPES:
        raise ValueError('无效操作或同步范围')
    try:
        result = sync(ROOT, action=OPERATIONS[operation], scope=SCOPES[scope],
                      repo=os.environ.get('UPSTREAM_REPO', '').strip(),
                      packages=os.environ.get('UPSTREAM_PACKAGES', '').strip(),
                      section=os.environ.get('UPSTREAM_SECTION', '').strip(), source_type=SOURCE_TYPES[source_type])
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        write_summary({'paths': [], 'errors': [str(exc)], 'rows': [
            {'repo': '—', 'package': '—', 'section': '—', 'status': '整体停止', 'detail': str(exc)}],
            'publication': '整体检查未通过，没有请求提交或发布。'}, operation)
        raise
    try:
        if operation != '查看清单' and (result['paths'] or not result['errors']):
            publish(ROOT, result['paths'], captured, os.environ['GITHUB_REPOSITORY'], result['config_update'])
            for item in result['rows']:
                if item['status'] == '待提交':
                    item['status'] = '已提交'
            result['publication'] = '已请求原索引发布；是否上线请查看独立的 Publish APT repository 运行。'
    except (ValueError, OSError) as exc:
        result['errors'].append(str(exc))
        for item in result['rows']:
            if item['status'] == '待提交':
                item['status'] = '待核对'
        result['publication'] = '提交或请求发布未完成：' + html.escape(str(exc))
        raise
    finally:
        write_summary(result, operation)
    if result['errors']:
        raise ValueError(f'本次有 {len(result["errors"])} 个失败项；有效更新已处理，请查看运行摘要')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, urllib.error.URLError) as exc:
        sys.exit(f'ERROR: {exc}')
