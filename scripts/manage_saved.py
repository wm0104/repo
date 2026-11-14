"""Separate package preparation from the credential-bearing publication step."""
import hashlib
import json
import os
import pathlib
import sys
import urllib.parse

import sync_upstream as engine
from saved_packages import ALL, NONE, WORKFLOWS, refresh, select


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
                       {path: content.encode('utf-8') for path, content in result.get('workflow_update', {}).items()} or None)
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


def workflow_inputs(data):
    entry = os.environ.get('UPSTREAM_ENTRY', '')
    fields = {key: os.environ.get('UPSTREAM_' + key, '').strip() for key in
              ('OPERATION', 'SCOPE', 'TYPE', 'REPO', 'PACKAGES', 'SECTION', 'SAVED_PACKAGE')}
    allowed = {'add': {'TYPE', 'REPO', 'PACKAGES', 'SECTION'},
               'update': {'SAVED_PACKAGE'},
               'manage': {'OPERATION', 'SAVED_PACKAGE', 'REPO', 'SECTION'}}
    if entry not in allowed:
        raise ValueError('无效同步入口')
    if any(value for key, value in fields.items() if key not in allowed[entry]):
        raise ValueError('当前入口不接受范围、手填插件或其他无关字段，请清空后重试')
    repo, packages, section = (fields[key] for key in ('REPO', 'PACKAGES', 'SECTION'))
    source_type, scope = 'github', 'all'
    if entry == 'add':
        if fields['TYPE'] not in engine.SOURCE_TYPES:
            raise ValueError('请选择 GitHub发行版 或 APT软件源')
        source_type = engine.SOURCE_TYPES[fields['TYPE']]
        repo = engine.normalize_source(repo, source_type)
        action = 'add' if packages else 'search'
        if action == 'search' and section:
            raise ValueError('名称留空时仅查找候选，请将分类留空；添加时再填写分类')
        if action == 'add' and not section:
            raise ValueError('添加插件请填写分类')
    else:
        label = fields['SAVED_PACKAGE']
        if entry == 'update':
            action = 'update'
            if label == ALL:
                packages = ''
            else:
                if label in ('', NONE):
                    raise ValueError('请选择全部已登记插件或某个已登记插件')
                scope, packages = select(data, action, 'all', '', '', '', label)
        else:
            operations = {'查看清单': 'list', '修改登记': 'modify', '停止同步': 'remove'}
            if fields['OPERATION'] not in operations:
                raise ValueError('请选择查看清单、修改登记或停止同步')
            action = operations[fields['OPERATION']]
            if action == 'list':
                if repo or section or label not in ('', NONE):
                    raise ValueError('查看清单时插件、新来源和新分类均留空/未选择')
            else:
                if label in ('', NONE, ALL):
                    raise ValueError('请选择一个已登记插件')
                scope, packages = select(data, 'remove', 'all', '', '', '', label)
                if action == 'remove' and (repo or section):
                    raise ValueError('停止同步不接受新来源或新分类；保留原包和分类')
                if action == 'modify':
                    if not repo and not section:
                        raise ValueError('修改登记至少填写新来源或新分类之一')
                    old = next(source for source in data['sources'] if packages in source['packages'])
                    source_type = old.get('type', 'github')
                    if repo:
                        # Explicit owner/repo or github.com Release URLs mean GitHub; other HTTPS directories mean APT.
                        source_type = ('github' if '://' not in repo or
                                       urllib.parse.urlsplit(repo).hostname == 'github.com' else 'apt')
                        repo = engine.normalize_source(repo, source_type)
                    else:
                        repo = old['repo']
                    section = section or old['packages'][packages]
    if action not in ('list', 'search') and os.environ.get('WORKFLOW_WRITE_CONFIGURED', '').lower() != 'true':
        raise ValueError('需要仓库 Actions Secret WORKFLOW_WRITE_TOKEN（仅本仓库 Contents 和 Workflows 写权限）；尚未下载、修改或发布')
    return dict(action=action, scope=scope, repo=repo, packages=packages, section=section, source_type=source_type)


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
        if os.environ.get('UPSTREAM_ENTRY'):
            args = workflow_inputs(data)
            action = args['action']
            operation = next(label for label, value in engine.OPERATIONS.items() if value == action)
            scope, packages = args['scope'], args['packages']
        else:
            scope, packages = select(data, action, engine.SCOPES[scope], repo, packages, section,
                                     os.environ.get('UPSTREAM_SAVED_PACKAGE', ''))
            args = dict(action=action, scope=scope, repo=repo, packages=packages,
                        section=section, source_type=engine.SOURCE_TYPES[source_type])
        if action in ('update', 'remove') and packages and os.environ.get('UPSTREAM_SAVED_PACKAGE') not in (None, '', NONE, ALL):
            # A chosen ID must not be confused with another package's display Name.
            args['packages'] = next(item['code'] for item in engine.saved_candidates(root, data) if item['package'] == packages)
        result = engine.sync(root, **args)
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
             'hashes': {path: digest(root / path) for path in set(result['paths']) | {'upstreams.json', *WORKFLOWS}}}
    if state['result'].get('config_update') is not None:
        state['result']['config_update'] = state['result']['config_update'].decode('utf-8')
    if state['result'].get('workflow_update') is not None:
        state['result']['workflow_update'] = {path: content.decode('utf-8') for path, content in
                                             state['result']['workflow_update'].items()}
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
