import hashlib
import io
import json
import os
import pathlib
import shutil
import unittest
from unittest.mock import patch

import test_unified_management as management
import sync_upstream


class PluginNamesTest(unittest.TestCase):
    setUp = management.ManagementTest.setUp
    write_config = management.ManagementTest.write_config
    config_now = management.ManagementTest.config_now
    package = management.ManagementTest.package
    release = management.ManagementTest.release
    run_sync = management.ManagementTest.run_sync

    def test_github_name_add_resolves_and_saves_package_name_without_double_download(self):
        item = self.package('org.example.app', extra='Name: 测试插件')
        self.release('new/repo', [item])
        with patch('sync_upstream.latest_release', return_value=self.assets['new/repo']) as release, \
                patch('sync_upstream.download_asset', side_effect=lambda asset, path: shutil.copyfile(asset['fixture'], path)) as download:
            result = sync_upstream.sync(self.root, action='add', repo='new/repo', packages='测试插件', section='工具')
        self.assertFalse(result['errors'])
        self.assertEqual(self.config_now()['sources'][1]['packages'], {'org.example.app': '工具'})
        self.assertEqual(self.config_now()['names']['org.example.app'], '测试插件')
        self.assertEqual(download.call_count, 1)
        self.assertEqual(release.call_count, 1)

    def test_apt_search_reads_only_index_and_returns_name_version_architecture_and_code(self):
        record = {'Name': '电话助手 iOS16', 'Package': 'org.example.call', 'Version': '2.0', 'Architecture': 'iphoneos-arm64e'}
        before = (self.root / 'upstreams.json').read_bytes()
        with patch('sync_upstream.latest_assets', return_value={'assets': [{'name': 'org.example.call.deb', 'record': record}]}), \
                patch('sync_upstream.download_verified') as download, patch('sync_upstream.preflight') as preflight:
            result = sync_upstream.sync(self.root, action='search', repo='https://example.org/',
                                        source_type='apt', packages='电话')
        self.assertFalse(result['errors'])
        self.assertEqual(result['paths'], [])
        self.assertEqual(result['rows'][0]['name'], '电话助手 iOS16')
        self.assertEqual(result['rows'][0]['version'], '2.0')
        self.assertEqual(result['rows'][0]['architecture'], 'iphoneos-arm64e')
        self.assertRegex(result['rows'][0]['code'], r'^@[0-9a-f]{12}$')
        download.assert_not_called()
        preflight.assert_not_called()
        self.assertEqual((self.root / 'upstreams.json').read_bytes(), before)

    def test_duplicate_names_require_confirmation_code_and_never_select_first(self):
        first = self.package('org.example.one', extra='Name: 同名插件')
        second = self.package('org.example.two', extra='Name: 同名插件')
        self.release('new/repo', [first, second])
        result = self.run_sync(action='add', repo='new/repo', packages='同名插件', section='工具')
        self.assertTrue(result['errors'])
        self.assertEqual(result['paths'], [])
        self.assertEqual(self.config_now(), self.config)
        candidates = self.run_sync(action='search', repo='new/repo', packages='同名插件')
        self.assertEqual(len(candidates['rows']), 2)
        code = next(row['code'] for row in candidates['rows'] if row['package'] == 'org.example.two')
        self.release('new/repo', [second, first])
        result = self.run_sync(action='add', repo='new/repo', packages=code, section='工具')
        self.assertFalse(result['errors'])
        self.assertEqual(self.config_now()['sources'][1]['packages'], {'org.example.two': '工具'})
        self.assertFalse((self.root / 'debs' / first.name).exists())

    def test_unrelated_apt_index_conflicts_do_not_block_named_search_or_add(self):
        item = self.package('org.example.good', extra='Name: 目标插件')
        record = {'Name': '目标插件', 'Package': 'org.example.good', 'Version': '1.0', 'Architecture': 'iphoneos-arm64e',
                  'Filename': 'debs/' + item.name, 'Size': str(item.stat().st_size),
                  'SHA256': hashlib.sha256(item.read_bytes()).hexdigest()}
        release = {'assets': [{'name': 'org.example.bad.deb', 'package': 'org.example.bad',
                              'lookup_names': ['其他坏插件'], 'error': '最高版本索引冲突'},
                             {'name': 'org.example.good.deb', 'record': record}]}
        kwargs = {'repo': 'https://example.org/', 'source_type': 'apt', 'packages': '目标插件'}
        with patch('sync_upstream.latest_assets', return_value=release), \
                patch('sync_upstream.download_verified', side_effect=lambda url, path, *args: path.write_bytes(item.read_bytes())):
            result = sync_upstream.sync(self.root, action='search', **kwargs)
            self.assertFalse(result['errors'])
            self.assertEqual(len(result['rows']), 1)
            result = sync_upstream.sync(self.root, action='add', section='工具', **kwargs)
            self.assertFalse(result['errors'])
            self.assertTrue((self.root / 'debs' / item.name).exists())

    def test_partial_keyword_imports_only_when_unique(self):
        item = self.package('org.example.app', extra='Name: 测试插件完整版')
        self.release('new/repo', [item])
        result = self.run_sync(action='add', repo='new/repo', packages='测试插件', section='工具')
        self.assertFalse(result['errors'])
        self.assertIn('debs/' + item.name, result['paths'])
        self.assertEqual(self.config_now()['sources'][1]['packages'], {'org.example.app': '工具'})

    def test_name_colliding_with_another_package_id_requires_confirmation(self):
        first = self.package('org.example.one', extra='Name: 第一个插件')
        second = self.package('org.example.two', extra='Name: org.example.one')
        self.release('new/repo', [first, second])
        result = self.run_sync(action='add', repo='new/repo', packages='org.example.one', section='工具')
        self.assertTrue(result['errors'])
        self.assertEqual(result['paths'], [])
        self.assertEqual(len(result['rows']), 2)

    def test_candidate_code_is_bound_to_source_and_version_and_missing_name_can_be_selected(self):
        item = self.package('org.example.app')
        self.release('new/repo', [item])
        searched = self.run_sync(action='search', repo='new/repo', packages='org.example.app')
        code = searched['rows'][0]['code']
        self.assertEqual(searched['rows'][0]['name'], 'org.example.app')
        self.release('different/repo', [item])
        result = self.run_sync(action='add', repo='different/repo', packages=code, section='工具')
        self.assertTrue(result['errors'])
        newer = self.package('org.example.app', '2.0')
        self.release('new/repo', [newer])
        result = self.run_sync(action='add', repo='new/repo', packages=code, section='工具')
        self.assertTrue(result['errors'])
        self.assertEqual(result['paths'], [])

    def test_update_all_uses_identity_after_name_change_and_stop_by_saved_name_is_offline(self):
        old = self.package('org.example.old', extra='Name: 原来名称')
        shutil.copyfile(old, self.root / 'debs' / old.name)
        newer = self.package('org.example.old', '2.0', extra='Name: 最新名称')
        self.release('owner/repo', [newer])
        result = self.run_sync()
        self.assertFalse(result['errors'])
        self.assertIn('debs/' + newer.name, result['paths'])
        self.assertEqual(self.config_now()['names']['org.example.old'], '最新名称')
        with patch('sync_upstream.latest_release') as network:
            listed = sync_upstream.sync(self.root, action='list')
            self.assertEqual(listed['rows'][0]['name'], '最新名称')
            result = sync_upstream.sync(self.root, action='remove', packages='最新名称')
            network.assert_not_called()
        self.assertFalse(result['errors'])
        self.assertEqual(self.config_now()['sources'], [])
        self.assertTrue((self.root / 'debs' / old.name).exists())

    def test_main_search_does_not_publish_and_summary_includes_candidates(self):
        result = {'paths': [], 'config_update': None, 'errors': [], 'rows': [
            {'repo': 'new/repo', 'name': '<测试>', 'package': 'org.example.app', 'version': '1.0',
             'architecture': 'iphoneos-arm64e', 'code': '@123456789abc', 'section': '', 'status': '候选', 'detail': ''}]}
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REF': 'refs/heads/main', 'GITHUB_REPOSITORY': 'owner/repo',
               'UPSTREAM_OPERATION': '查找插件', 'UPSTREAM_REPO': 'new/repo', 'UPSTREAM_PACKAGES': '测试'}
        with patch.dict(os.environ, env, clear=True), patch('sync_upstream.checked', side_effect=['', 'sha']), \
                patch('sync_upstream.sync', return_value=result), patch('sync_upstream.publish') as publish, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            sync_upstream.main()
            publish.assert_not_called()
        self.assertIn('@123456789abc', output.getvalue())
        self.assertIn('&lt;测试&gt;', output.getvalue())


if __name__ == '__main__':
    unittest.main()
