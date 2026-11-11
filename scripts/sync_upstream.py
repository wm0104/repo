#!/usr/bin/env python3
"""Manually sync configured GitHub release debs, then request the existing publisher."""
import argparse
import functools
import hashlib
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

from build_repo import compare_versions, load_sections, load_upstreams, parse, validate_sources

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
    size = asset.get('size')
    if type(size) is not int or not 0 < size <= MAX_DEB:
        raise ValueError(f'{asset["name"]}: 安装包大小无效')
    sha, total = hashlib.sha256(), 0
    request = urllib.request.Request(asset['browser_download_url'], headers={'User-Agent': 'wm0104-repo-sync'})
    with urllib.request.urlopen(request, timeout=60) as response, target.open('wb') as output:
        while chunk := response.read(256 * 1024):
            total += len(chunk)
            if total > size:
                raise ValueError(f'{asset["name"]}: 下载超过声明大小')
            sha.update(chunk)
            output.write(chunk)
    if total != size or sha.hexdigest() != digest.split(':', 1)[1].lower():
        raise ValueError(f'{asset["name"]}: 大小或 SHA256 不匹配')


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


def sync(root, source=None):
    sources = load_upstreams(root)
    sections = load_sections(root)
    config_path = root / 'upstreams.json'
    config = config_path.read_bytes() if config_path.exists() else None
    config_update = None
    if source is not None:
        validate_sources([source])
        if len(source['packages']) != 1:
            raise ValueError('单插件入口每次只允许填写一个 Package')
        package, section = next(iter(source['packages'].items()))
        batch_sections = {p: s for item in sources for p, s in item['packages'].items()}
        if package in batch_sections and batch_sections[package] != section:
            raise ValueError(f'分组与批量配置冲突: {package}，请填写 {batch_sections[package]}')
        if sections.get(package) != section:
            data = json.loads(config) if config is not None else {'sources': []}
            data.setdefault('manual_sections', {})[package] = section
            config_update = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        sources = [source]
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
        for source in sources:
            release = latest_release(source['repo'])
            print(f'检查 {source["repo"]}: {release.get("tag_name", "latest")}')
            found = {}
            for index, asset in enumerate(release['assets']):
                name = asset['name']
                if not name.endswith('.deb'):
                    continue
                if pathlib.PurePosixPath(name).name != name or '\\' in name or any(ord(c) < 32 for c in name):
                    raise ValueError('不安全的 Release 文件名')
                candidate = workspace / f'{len(plans)}-{index}.deb'
                download_asset(asset, candidate)
                fields = package_info(candidate)
                package = fields['Package']
                if package not in source['packages'] or fields['Architecture'] != ARCHITECTURE:
                    continue
                if package in found:
                    raise ValueError(f'{source["repo"]}: 多个资产匹配 {package}')
                found[package] = (candidate, name, fields['Version'])
            missing = set(source['packages']) - set(found)
            if missing:
                raise ValueError(f'{source["repo"]}: 缺少 {ARCHITECTURE} 包: {", ".join(sorted(missing))}')
            for package, (candidate, name, version) in found.items():
                history = existing.get(package, {})
                if history:
                    latest = max(history, key=functools.cmp_to_key(compare_versions))
                    comparison = compare_versions(version, latest)
                    if comparison < 0:
                        print(f'跳过较低版本: {package} {version} < {latest}')
                        continue
                    if comparison == 0:
                        sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
                        if sha != original[history[latest].name]:
                            print(f'同版本异文件，保留仓库原包: {package} {version}')
                        else:
                            print(f'无需更新: {package} {version}')
                        continue
                if (root / 'debs' / name).exists() or name in {p[1] for p in plans}:
                    raise ValueError(f'文件名冲突，拒绝覆盖: {name}')
                plans.append((candidate, name))
        if not plans:
            print('没有需要入库的新版本')
            if config_update is None:
                return []
        preflight(root, workspace, plans, config_update)
        current_config = config_path.read_bytes() if config_path.exists() else None
        if original != snapshot(root) or config != current_config:
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
        return [f'debs/{name}' for _, name in plans] + (['upstreams.json'] if config_update is not None else [])


def publish(root, paths, captured, repository, config_package=None):
    def git(*args):
        return checked(['git', *args], cwd=root)

    def current_remote():
        return git('ls-remote', 'origin', 'refs/heads/main').split()[0]

    if current_remote() != captured:
        raise ValueError('远端 main 已更新，请重新运行；拒绝覆盖新提交')
    if paths:
        deb_paths = [p for p in paths if p != 'upstreams.json']
        if 'upstreams.json' in paths:
            if config_package is None:
                raise ValueError('不安全的同步提交路径: upstreams.json')
            old = {'sources': []}
            if git('ls-files', '--', 'upstreams.json'):
                old = json.loads(git('show', 'HEAD:upstreams.json'))
            new = json.loads((root / 'upstreams.json').read_text(encoding='utf-8'))
            load_sections(root)
            manual = new.get('manual_sections', {})
            expected_manual = {**old.get('manual_sections', {}), config_package: manual.get(config_package)}
            expected = {**old, 'manual_sections': expected_manual}
            if config_package not in manual or new != expected:
                raise ValueError('配置修改超出当次指定插件的分组，停止提交')
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--single', action='store_true', help='只同步工作流表单指定的单个插件')
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise ValueError('请在 GitHub Actions 的 main 分支手动运行同步工作流')
    if checked(['git', 'status', '--porcelain'], cwd=ROOT):
        raise ValueError('工作区不干净，停止同步')
    captured = checked(['git', 'rev-parse', 'HEAD'], cwd=ROOT)
    source = None
    package = None
    if args.single:
        package = os.environ.get('UPSTREAM_PACKAGE', '')
        source = {'repo': os.environ.get('UPSTREAM_REPO', ''),
                  'packages': {package: os.environ.get('UPSTREAM_SECTION', '')}}
    paths = sync(ROOT, source)
    publish(ROOT, paths, captured, os.environ['GITHUB_REPOSITORY'], package)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, urllib.error.URLError) as exc:
        sys.exit(f'ERROR: {exc}')
