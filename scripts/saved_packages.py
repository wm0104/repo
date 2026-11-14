"""Maintain only the generated saved-package choices in the existing workflow."""
import json
import re

WORKFLOW = '.github/workflows/sync-upstream.yml'
NONE = '请选择已登记插件'
ALL = '全部已登记插件'
WORKFLOWS = (WORKFLOW, '.github/workflows/manage-sync.yml')
BEGIN = b'        # BEGIN SAVED PACKAGE OPTIONS\n'
END = b'        # END SAVED PACKAGE OPTIONS\n'


def render(original, catalog):
    newline = b'\r\n' if b'\r\n' in original else b'\n'
    begin, end = (marker.replace(b'\n', newline) for marker in (BEGIN, END))
    if original.count(begin) != 1 or original.count(end) != 1:
        raise ValueError('工作流缺少唯一的已登记插件选项标记')
    before, rest = original.split(begin)
    _, after = rest.split(end)
    labels = [ALL if ('default: ' + ALL).encode('utf-8') in original else NONE]
    for item in sorted(catalog, key=lambda item: (item['name'].casefold(), item['package'])):
        name = ' '.join(item['name'].split()).replace('${{', '＄{{')
        labels.append(f'{name} [{item["package"]}]')
    options = ''.join('          - ' + json.dumps(label, ensure_ascii=False) + '\n' for label in labels)
    return before + begin + options.encode('utf-8').replace(b'\n', newline) + end + after


def select(data, action, scope, repo, packages, section, label):
    if not label or label == NONE:
        return scope, packages
    if action not in ('update', 'remove') or repo or packages or section or scope == 'repo':
        raise ValueError('下拉选择仅用于更新或停止同步；仓库、手动插件和分组留空，范围不要选指定仓库')
    match = re.search(r' \[([A-Za-z0-9][A-Za-z0-9+.-]+)\]$', label)
    known = {package for source in data['sources'] for package in source['packages']}
    if not match or match[1] not in known:
        raise ValueError('下拉选项已失效或插件已停止同步，请刷新表单后重新选择')
    return 'packages', match[1]


def catalog(root):
    from sync_upstream import saved_candidates, registrations
    data = json.loads((root / 'upstreams.json').read_bytes())
    known = registrations(data)
    return [item for item in saved_candidates(root, data) if item['package'] in known]


def refresh(root, result, token_available):
    choices = catalog(root)
    updates = {path: render((root / path).read_bytes(), choices) for path in WORKFLOWS}
    updates = {path: updated for path, updated in updates.items() if (root / path).read_bytes() != updated}
    if not updates:
        return
    if not token_available:
        raise ValueError('需要配置仓库 Actions Secret WORKFLOW_WRITE_TOKEN（仅本仓库 Contents 和 Workflows 写权限），才能自动维护插件下拉选项；尚未提交或发布')
    for path, updated in updates.items():
        if (root / path).is_symlink():
            raise ValueError('工作流选项文件不能是符号链接')
        (root / path).write_bytes(updated)
        result['paths'].append(path)
    result['workflow_update'] = updates
