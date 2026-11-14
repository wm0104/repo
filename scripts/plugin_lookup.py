"""Resolve human-facing names without changing the persistent Package identity."""
import hashlib
import json
import re


class ChoiceRequired(ValueError):
    def __init__(self, message, candidates):
        super().__init__(message)
        self.candidates = candidates


def selectors(text):
    if not text:
        return []
    values = [item.strip() for item in text.replace('\n', ',').split(',')]
    if any(not item or len(item) > 512 or item.startswith('../') or any(ord(c) < 32 for c in item) for item in values):
        raise ValueError('插件名称/Package/候选码不能为空或含控制字符；多个值用英文逗号分隔')
    if len(values) != len(set(values)):
        raise ValueError('插件选择不能重复')
    return values


def candidate(repo, source_type, fields, saved_name=''):
    identity = [source_type, repo if source_type == 'apt' else repo.casefold(),
                fields['Package'], fields['Version'], fields['Architecture']]
    code = '@' + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:12]
    return {'repo': repo, 'source_type': source_type, 'name': fields.get('Name') or saved_name or fields['Package'],
            'package': fields['Package'], 'version': fields['Version'], 'architecture': fields['Architecture'],
            'code': code, 'section': '', 'status': '候选', 'detail': '' if fields.get('Name') else '未提供显示名，使用标识兜底'}


def search(catalog, text):
    words = [word.casefold() for word in selectors(text)]
    return [item for item in catalog if not words or any(
        word in item['name'].casefold() or word in item['package'].casefold() or word == item['code'] for word in words)]


def resolve(catalog, text, keywords=False):
    selected = []
    for word in selectors(text):
        matches = [item for item in catalog if item['code'] == word] if word.startswith('@') else [
            item for item in catalog if item['package'] == word or item['name'].casefold() == word.casefold()]
        if not matches and keywords:
            matches = search(catalog, word) if not word.startswith('@') else []
            if not matches and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9+.-]*\.[A-Za-z0-9+.-]+', word):
                raise ChoiceRequired(f'没有匹配插件: {word}；请修改关键词或重新查找候选码', [])
        if not matches:
            suggestions = search(catalog, word)
            if suggestions or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9+.-]+', word):
                raise ChoiceRequired(f'未找到唯一完整名称或候选码: {word}；请查找并填写完整名称或当前候选码', suggestions)
            # Keep legacy Package input: the sync engine reports missing packages individually.
            selected.append(word)
            continue
        identities = {item['package'] for item in matches}
        if len(identities) != 1:
            raise ChoiceRequired(f'名称有多个候选: {word}；请使用候选码确认，不自动选择', matches)
        selected.append(matches[0]['package'])
    if len(selected) != len(set(selected)):
        raise ValueError('多个输入指向同一插件，请只选择一次')
    return ','.join(selected)
