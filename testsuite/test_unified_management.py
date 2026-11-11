import hashlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import sync_upstream
import build_repo


class ManagementTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        (self.root / 'debs').mkdir()
        shutil.copytree(ROOT / 'scripts', self.root / 'scripts')
        self.config = {'sources': [{'repo': 'owner/repo', 'packages': {'org.example.old': '乌龙'}}]}
        self.write_config()
        self.assets = {}

    def write_config(self):
        (self.root / 'upstreams.json').write_text(json.dumps(self.config), encoding='utf-8')

    def config_now(self):
        return json.loads((self.root / 'upstreams.json').read_text(encoding='utf-8'))

    def package(self, name, version='1.0', extra=''):
        import subprocess
        control = self.root / 'fixtures' / name / version / 'DEBIAN/control'
        control.parent.mkdir(parents=True, exist_ok=True)
        control.write_text(f'Package: {name}\nVersion: {version}\nArchitecture: iphoneos-arm64e\n'
                           f'Maintainer: test\nDescription: test\nSection: Development\n{extra}\n')
        target = self.root / f'{name}_{version}_iphoneos-arm64e.deb'
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(control.parents[1]), str(target)],
                       capture_output=True, check=True)
        return target

    def release(self, repo, paths):
        self.assets[repo] = {'tag_name': 'v1', 'assets': [{'name': p.name, 'fixture': p} for p in paths]}

    def run_sync(self, **kwargs):
        def download(asset, path):
            shutil.copyfile(asset['fixture'], path)
        with patch('sync_upstream.latest_release', side_effect=lambda repo: self.assets[repo]), \
                patch('sync_upstream.download_asset', side_effect=download):
            return sync_upstream.sync(self.root, **kwargs)

    def test_add_remembers_multiple_packages_without_collecting_unselected_assets(self):
        first = self.package('org.example.first')
        second = self.package('org.example.second')
        other = self.package('org.example.other')
        self.release('new/repo', [first, second, other])
        result = self.run_sync(action='add', repo='new/repo', packages='org.example.first, org.example.second', section='美化')
        self.assertFalse(result['errors'])
        self.assertEqual(set(result['paths']), {'debs/' + first.name, 'debs/' + second.name, 'upstreams.json'})
        registered = self.config_now()['sources']
        self.assertEqual(registered[0], self.config['sources'][0])
        self.assertEqual(registered[1], {'repo': 'new/repo', 'packages': {'org.example.first': '美化', 'org.example.second': '美化'}})
        self.assertFalse((self.root / 'debs' / other.name).exists())
        again = self.run_sync(scope='repo', repo='new/repo')
        self.assertEqual(again['paths'], [])
        self.assertEqual(len(again['rows']), 2)

    def test_add_merges_same_repo_and_duplicate_is_idempotent(self):
        item = self.package('org.example.new')
        self.release('owner/repo', [item])
        self.run_sync(action='add', repo='owner/repo', packages='org.example.new', section='工具')
        before = (self.root / 'upstreams.json').read_bytes()
        result = self.run_sync(action='add', repo='owner/repo', packages='org.example.new', section='工具')
        self.assertEqual(result['paths'], [])
        self.assertEqual((self.root / 'upstreams.json').read_bytes(), before)
        self.assertEqual(len(self.config_now()['sources']), 1)

    def test_invalid_add_or_unknown_scope_is_rejected_before_network(self):
        for kwargs in [
            {'action': 'add', 'repo': 'x/y;echo bad', 'packages': 'org.example.new', 'section': '工具'},
            {'action': 'add', 'repo': 'x/y', 'packages': '../escape', 'section': '工具'},
            {'action': 'add', 'repo': 'x/y', 'packages': 'org.example.new,', 'section': '工具'},
            {'action': 'add', 'repo': 'x/y', 'packages': 'org.example.new', 'section': '工具\nVersion: 9'},
            {'action': 'add', 'repo': 'different/repo', 'packages': 'org.example.old', 'section': '乌龙'},
            {'action': 'add', 'repo': 'owner/repo', 'packages': 'org.example.old', 'section': '其他'},
            {'scope': 'repo', 'repo': 'unknown/repo'},
            {'scope': 'packages', 'packages': 'org.example.unknown'},
            {'scope': 'all', 'packages': 'org.example.old'},
            {'action': 'remove', 'packages': ''},
        ]:
            with self.subTest(kwargs=kwargs), patch('sync_upstream.latest_release') as release:
                with self.assertRaises(ValueError):
                    sync_upstream.sync(self.root, **kwargs)
                release.assert_not_called()
        self.assertEqual(self.config_now(), self.config)

    def test_modify_moves_registration_and_updates_section_without_touching_old_bytes(self):
        item = self.package('org.example.old')
        shutil.copyfile(item, self.root / 'debs' / item.name)
        original = (self.root / 'debs' / item.name).read_bytes()
        self.release('next/repo', [item])
        result = self.run_sync(action='modify', repo='next/repo', packages='org.example.old', section='新分组')
        self.assertEqual(result['paths'], ['upstreams.json'])
        self.assertEqual(self.config_now()['sources'], [{'repo': 'next/repo', 'packages': {'org.example.old': '新分组'}}])
        self.assertEqual((self.root / 'debs' / item.name).read_bytes(), original)
        self.assertEqual(build_repo.load_sections(self.root)['org.example.old'], '新分组')

    def test_remove_keeps_debs_and_section_and_list_is_read_only(self):
        item = self.package('org.example.old')
        shutil.copyfile(item, self.root / 'debs' / item.name)
        before = hashlib.sha256(item.read_bytes()).hexdigest()
        with patch('sync_upstream.latest_release') as release:
            result = sync_upstream.sync(self.root, action='remove', packages='org.example.old')
            release.assert_not_called()
        self.assertEqual(result['paths'], ['upstreams.json'])
        self.assertEqual(self.config_now()['sources'], [])
        self.assertEqual(build_repo.load_sections(self.root)['org.example.old'], '乌龙')
        self.assertEqual(hashlib.sha256((self.root / 'debs' / item.name).read_bytes()).hexdigest(), before)
        config = (self.root / 'upstreams.json').read_bytes()
        with patch('sync_upstream.latest_release') as release, patch('sync_upstream.preflight') as preflight:
            listed = sync_upstream.sync(self.root, action='list')
            release.assert_not_called()
            preflight.assert_not_called()
        self.assertEqual(listed['paths'], [])
        self.assertEqual(listed['rows'][0]['status'], '已停止同步')
        self.assertEqual((self.root / 'upstreams.json').read_bytes(), config)

    def test_one_missing_package_does_not_prevent_another_package_registration(self):
        item = self.package('org.example.good')
        self.release('new/repo', [item])
        result = self.run_sync(action='add', repo='new/repo', packages='org.example.good,org.example.missing', section='工具')
        self.assertTrue(result['errors'])
        self.assertTrue((self.root / 'debs' / item.name).exists())
        self.assertEqual(self.config_now()['sources'][1]['packages'], {'org.example.good': '工具'})

    def test_bad_candidate_metadata_is_isolated_from_other_valid_packages(self):
        good = self.package('org.example.good')
        bad = self.package('org.example.bad', extra='X-WM0104-Changelog: broken')
        self.release('new/repo', [good, bad])
        result = self.run_sync(action='add', repo='new/repo', packages='org.example.good,org.example.bad', section='工具')
        self.assertTrue(result['errors'])
        self.assertEqual(result['paths'], ['debs/' + good.name, 'upstreams.json'])
        self.assertFalse((self.root / 'debs' / bad.name).exists())

    def test_global_preflight_failure_keeps_original_config_and_debs(self):
        good = self.package('org.example.good')
        self.release('new/repo', [good])
        with patch('sync_upstream.validate_candidate'), patch('sync_upstream.preflight', side_effect=ValueError('整体构建失败')):
            with self.assertRaisesRegex(ValueError, '整体构建失败'):
                self.run_sync(action='add', repo='new/repo', packages='org.example.good', section='工具')
        self.assertEqual(self.config_now(), self.config)
        self.assertEqual(list((self.root / 'debs').glob('*.deb')), [])

    def test_config_write_failure_rolls_back_packages_and_keeps_original_registration(self):
        good = self.package('org.example.good')
        self.release('new/repo', [good])
        before = (self.root / 'upstreams.json').read_bytes()
        with patch('pathlib.Path.replace', side_effect=OSError('config write failure')):
            with self.assertRaisesRegex(OSError, 'config write failure'):
                self.run_sync(action='add', repo='new/repo', packages='org.example.good', section='工具')
        self.assertEqual((self.root / 'upstreams.json').read_bytes(), before)
        self.assertEqual(list((self.root / 'debs').glob('*.deb')), [])

    def test_failed_modify_keeps_old_source_and_selected_scope_ignores_other_sources(self):
        self.release('next/repo', [])
        result = self.run_sync(action='modify', repo='next/repo', packages='org.example.old', section='新分组')
        self.assertTrue(result['errors'])
        self.assertEqual(self.config_now(), self.config)
        item = self.package('org.example.old')
        self.config['sources'].append({'repo': 'other/repo', 'packages': {'org.example.other': '工具'}})
        self.write_config()
        self.release('owner/repo', [item])
        result = self.run_sync(scope='packages', packages='org.example.old')
        self.assertFalse(result['errors'])
        self.assertEqual(result['paths'], ['debs/' + item.name])

    def test_repository_failure_does_not_block_another_registered_repository(self):
        good = self.package('org.example.good')
        self.config['sources'].append({'repo': 'new/repo', 'packages': {'org.example.good': '工具'}})
        self.write_config()
        self.release('new/repo', [good])
        with patch('sync_upstream.latest_release', side_effect=[OSError('上游不可用'), self.assets['new/repo']]), \
                patch('sync_upstream.download_asset', side_effect=lambda asset, path: shutil.copyfile(asset['fixture'], path)):
            result = sync_upstream.sync(self.root)
        self.assertTrue(result['errors'])
        self.assertTrue((self.root / 'debs' / good.name).exists())

    def test_summary_escapes_untrusted_text(self):
        target = self.root / 'summary.md'
        result = {'paths': [], 'errors': [], 'rows': [
            {'repo': '<script>', 'package': 'org.example.x', 'section': 'a|b', 'status': '跳过', 'detail': '<img>\nbad'}]}
        with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(target)}):
            sync_upstream.write_summary(result, '查看清单')
        text = target.read_text()
        self.assertNotIn('<script>', text)
        self.assertNotIn('<img>', text)
        self.assertIn('org.example.x', text)


if __name__ == '__main__':
    unittest.main()
