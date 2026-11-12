"""Separate package preparation from the credential-bearing publication step."""
import hashlib
import json
import os
import pathlib
import sys

import sync_upstream as engine
from saved_packages import NONE, WORKFLOW, refresh, select


def state_path():
    return pathlib.Path(os.environ['RUNNER_TEMP']) / 'wm0104-sync-prepared.json'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def finish(root, state):
    result, operation = state['result'], state['operation']
    try:
        if engine.checked(['git', 'rev-parse', 'HEAD'], cwd=root) != state['captured']:
            raise ValueError('准备后本地 HEAD 已变化，停止提交')
        for path, expected in state['hashes'].items():
            if digest(root / path) != expected:
                raise ValueError('准备后文件已变化，停止提交: ' + path)
        engine.publish(root, result['paths'], state['captured'], os.environ['GITHUB_REPOSITORY'],
                       result['config_update'].encode('utf-8') if result['config_update'] is not None else None,
                       result.get('workflow_update', '').encode('utf-8') or None)
        for item in result['rows']:
            if item['status'] == '待提交':
                item['status'] = '已提交'
        result['publication'] = '已请求原索引发布；是否上线请查看独立的 Publish APT repository 运行。'
    except (ValueError, OSError) as exc:
        result['errors'].append(str(exc))
        for item in result['rows']:
            if item['status'] == '待提交':
                item['status'] = '待核对'
        result['publication'] = '提交或请求发布未完成。'
        raise
    finally:
        engine.write_summary(result, operation)
    if result['errors']:
        raise ValueError(f'本次有 {len(result["errors"])} 个失败项；有效更新已处理，请查看运行摘要')


def prepare(root):
    state_path().unlink(missing_ok=True)
    if engine.checked(['git', 'status', '--porcelain'], cwd=root):
        raise ValueError('工作区不干净，停止同步')
    captured = engine.checked(['git', 'rev-parse', 'HEAD'], cwd=root)
    operation = os.environ.get('UPSTREAM_OPERATION', '查看清单')
    scope = os.environ.get('UPSTREAM_SCOPE', '全部')
    source_type = os.environ.get('UPSTREAM_TYPE', 'GitHub发行版')
    if operation not in engine.OPERATIONS or scope not in engine.SCOPES or source_type not in engine.SOURCE_TYPES:
        raise ValueError('无效操作或同步范围')
    try:
        action = engine.OPERATIONS[operation]
        repo, packages, section = (os.environ.get(key, '').strip() for key in
                                   ('UPSTREAM_REPO', 'UPSTREAM_PACKAGES', 'UPSTREAM_SECTION'))
        data = json.loads((root / 'upstreams.json').read_bytes())
        scope, packages = select(data, action, engine.SCOPES[scope], repo, packages, section,
                                 os.environ.get('UPSTREAM_SAVED_PACKAGE', ''))
        if os.environ.get('UPSTREAM_SAVED_PACKAGE') not in (None, '', NONE):
            # A chosen ID must not be confused with another package's display Name.
            packages = next(item['code'] for item in engine.saved_candidates(root, data) if item['package'] == packages)
        result = engine.sync(root, action=action, scope=scope, repo=repo, packages=packages,
                             section=section, source_type=engine.SOURCE_TYPES[source_type])
        should_publish = action not in ('list', 'search') and (result['paths'] or not result['errors'])
        if should_publish:
            refresh(root, result, os.environ.get('WORKFLOW_WRITE_CONFIGURED', '').lower() == 'true')
    except (ValueError, OSError) as exc:
        engine.write_summary({'paths': [], 'errors': [str(exc)], 'rows': [],
                              'publication': '整体检查未通过，没有提交或请求发布。'}, operation)
        raise
    if not should_publish:
        engine.write_summary(result, operation)
        if result['errors']:
            raise ValueError('查找或插件检查失败，请查看运行摘要')
        return
    state = {'operation': operation, 'captured': captured, 'result': dict(result),
             'hashes': {path: digest(root / path) for path in set(result['paths']) | {'upstreams.json', WORKFLOW}}}
    for key in ('config_update', 'workflow_update'):
        if state['result'].get(key) is not None:
            state['result'][key] = state['result'][key].decode('utf-8')
    state_path().write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    state_path().chmod(0o600)
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        output.write('publish=true\n')


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise ValueError('请在 GitHub Actions 的 main 分支手动运行同步工作流')
    if sys.argv[1:] == ['prepare']:
        prepare(engine.ROOT)
    elif sys.argv[1:] == ['publish']:
        finish(engine.ROOT, json.loads(state_path().read_text(encoding='utf-8')))
    else:
        raise ValueError('请选择 prepare 或 publish 阶段')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as exc:
        sys.exit('ERROR: ' + str(exc))
