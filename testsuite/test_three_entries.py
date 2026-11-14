import os
import unittest
from unittest.mock import patch

import test_plugin_names as names
import manage_saved
import saved_packages
import sync_upstream as engine


class ThreeEntryTest(unittest.TestCase):
    def test_github_addresses_normalize_and_version_links_warn(self):
        for address in ('owner/repo', 'https://github.com/owner/repo',
                        'https://github.com/owner/repo/releases',
                        'https://github.com/owner/repo/releases/tag/v1.0'):
            with self.subTest(address=address), patch('builtins.print') as output:
                self.assertEqual(engine.normalize_source(address, 'github'), 'owner/repo')
                if '/tag/' in address:
                    self.assertIn('最新正式版', str(output.call_args))

    def test_unsafe_github_addresses_fail_closed(self):
        for address in ('http://github.com/a/b', 'https://github.com.evil/a/b',
                        'https://user@github.com/a/b', 'https://github.com:443/a/b',
                        'https://github.com/a/b?x=1', 'https://github.com/a/b#x',
                        'https://github.com/a/b/tree/main', 'https://github.com/a/../b',
                        'https://github.com/a/b/releases/tag/%2e%2e', 'a/..',
                        'a/b/c', 'a/b\\evil', 'https://github.com/a/b\n'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                engine.normalize_source(address, 'github')

    def inputs(self, data=None, **env):
        with patch.dict(os.environ, {'WORKFLOW_WRITE_CONFIGURED': 'true', **env}, clear=True):
            return manage_saved.workflow_inputs(data or {'sources': []})

    def test_add_blank_is_search_and_source_type_is_explicit(self):
        args = self.inputs(UPSTREAM_ENTRY='add', UPSTREAM_TYPE='APT软件源',
                           UPSTREAM_REPO='https://example.org/repo')
        with self.assertRaisesRegex(ValueError, '分类留空'):
            self.inputs(UPSTREAM_ENTRY='add', UPSTREAM_TYPE='APT软件源',
                        UPSTREAM_REPO='https://example.org/repo', UPSTREAM_SECTION='工具')
        self.assertEqual((args['action'], args['repo'], args['section']),
                         ('search', 'https://example.org/repo/', ''))
        with self.assertRaises(ValueError):
            self.inputs(UPSTREAM_ENTRY='add', UPSTREAM_TYPE='bad', UPSTREAM_REPO='a/b')

    def test_choices_conflicts_and_stale_selection(self):
        data = {'sources': [{'repo': 'a/b', 'packages': {'org.test.app': '工具'}}]}
        args = self.inputs(data, UPSTREAM_ENTRY='update', UPSTREAM_SAVED_PACKAGE=saved_packages.ALL)
        self.assertEqual((args['action'], args['scope']), ('update', 'all'))
        for env in ({'UPSTREAM_REPO': 'a/b'}, {'UPSTREAM_PACKAGES': 'org.test.app'},
                    {'UPSTREAM_SECTION': '工具'}, {'UPSTREAM_SCOPE': '全部'},
                    {'UPSTREAM_TYPE': 'GitHub发行版'}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                self.inputs(data, UPSTREAM_ENTRY='update', UPSTREAM_SAVED_PACKAGE=saved_packages.ALL, **env)
        with self.assertRaises(ValueError):
            self.inputs(data, UPSTREAM_ENTRY='update', UPSTREAM_SAVED_PACKAGE='旧 [org.test.removed]')

    def test_manage_keeps_blank_values_and_requires_change(self):
        data = {'sources': [{'repo': 'a/b', 'packages': {'org.test.app': '工具'}}]}
        base = dict(UPSTREAM_ENTRY='manage', UPSTREAM_OPERATION='修改登记',
                    UPSTREAM_SAVED_PACKAGE='插件 [org.test.app]')
        args = self.inputs(data, **base, UPSTREAM_SECTION='新分类')
        self.assertEqual((args['repo'], args['section'], args['packages'], args['source_type']),
                         ('a/b', '新分类', 'org.test.app', 'github'))
        args = self.inputs(data, **base, UPSTREAM_REPO='https://example.org/')
        self.assertEqual((args['source_type'], args['section']), ('apt', '工具'))
        with self.assertRaises(ValueError):
            self.inputs(data, **base)
        for operation in ('查看清单', '停止同步'):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.inputs(data, UPSTREAM_ENTRY='manage', UPSTREAM_OPERATION=operation,
                            UPSTREAM_SAVED_PACKAGE='插件 [org.test.app]', UPSTREAM_SECTION='不能忽略')

    def test_missing_secret_blocks_mutation_before_engine(self):
        with patch.dict(os.environ, {'UPSTREAM_ENTRY': 'add', 'UPSTREAM_REPO': 'a/b',
                                    'UPSTREAM_TYPE': 'GitHub发行版', 'UPSTREAM_PACKAGES': '插件',
                                    'UPSTREAM_SECTION': '工具'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'WORKFLOW_WRITE_TOKEN'):
                manage_saved.workflow_inputs({'sources': []})


class KeywordAndIdentityTest(unittest.TestCase):
    setUp = names.PluginNamesTest.setUp
    write_config = names.PluginNamesTest.write_config
    config_now = names.PluginNamesTest.config_now
    package = names.PluginNamesTest.package
    release = names.PluginNamesTest.release
    run_sync = names.PluginNamesTest.run_sync
    def test_keyword_many_and_none_never_change_registration(self):
        one = self.package('org.example.one', extra='Name: 测试插件一')
        two = self.package('org.example.two', extra='Name: 测试插件二')
        self.release('new/repo', [one, two])
        for query, count in [('测试', 2), ('不存在', 0)]:
            result = self.run_sync(action='add', repo='new/repo', packages=query, section='工具')
            self.assertTrue(result['errors'])
            self.assertEqual(len(result['rows']), count)
            self.assertTrue(all(row['code'].startswith('@') for row in result['rows']))
            self.assertEqual(self.config_now(), self.config)

    def test_modify_source_never_follows_another_packages_display_name(self):
        other = self.package('org.example.other', extra='Name: org.example.old')
        self.release('next/repo', [other])
        result = self.run_sync(action='modify', repo='next/repo', packages='org.example.old', section='新分类')
        self.assertTrue(result['errors'])
        self.assertEqual(self.config_now(), self.config)
        self.assertEqual(result['paths'], [])

    def test_manage_source_change_same_id_and_blank_classification(self):
        item = self.package('org.example.old', extra='Name: 作者改了显示名')
        self.release('next/repo', [item])
        with patch.dict(os.environ, {'UPSTREAM_ENTRY': 'manage', 'UPSTREAM_OPERATION': '修改登记',
                                    'UPSTREAM_SAVED_PACKAGE': '旧名 [org.example.old]',
                                    'UPSTREAM_REPO': 'https://github.com/next/repo/releases',
                                    'WORKFLOW_WRITE_CONFIGURED': 'true'}, clear=True):
            args = manage_saved.workflow_inputs(self.config)
        result = self.run_sync(**args)
        self.assertFalse(result['errors'])
        sources = self.config_now()['sources']
        self.assertEqual(sources, [{'repo': 'next/repo', 'packages': {'org.example.old': '乌龙'}}])

    def test_manage_classification_change_keeps_source(self):
        item = self.package('org.example.old')
        self.release('owner/repo', [item])
        with patch.dict(os.environ, {'UPSTREAM_ENTRY': 'manage', 'UPSTREAM_OPERATION': '修改登记',
                                    'UPSTREAM_SAVED_PACKAGE': '旧名 [org.example.old]',
                                    'UPSTREAM_SECTION': '新分类', 'WORKFLOW_WRITE_CONFIGURED': 'true'}, clear=True):
            args = manage_saved.workflow_inputs(self.config)
        result = self.run_sync(**args)
        self.assertFalse(result['errors'])
        self.assertEqual(self.config_now()['sources'],
                         [{'repo': 'owner/repo', 'packages': {'org.example.old': '新分类'}}])
