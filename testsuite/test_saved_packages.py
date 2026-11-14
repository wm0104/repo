import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest.mock import patch

import test_upstream_sync as fixtures
import saved_packages
import sync_upstream
import manage_saved

ROOT = pathlib.Path(__file__).resolve().parents[1]
REAL_CHECKED = sync_upstream.checked


def prepared_commands(args, cwd=None):
    if args[:2] == ['git', 'status']:
        return ''
    if args[:2] == ['git', 'rev-parse']:
        return 'sha'
    return REAL_CHECKED(args, cwd=cwd)


class SavedChoiceTest(unittest.TestCase):
    def test_renderer_changes_only_marked_options_and_escapes_names(self):
        original = (ROOT / saved_packages.WORKFLOW).read_bytes().replace(b'\r\n', b'\n')
        catalog = [{'name': '相同名', 'package': 'org.example.b'},
                   {'name': '相同名', 'package': 'org.example.a'},
                   {'name': 'a\n\" : ${{ bad }}', 'package': 'org.example.escape'}]
        rendered = saved_packages.render(original, catalog)
        self.assertIn('org.example.a', rendered.decode())
        self.assertIn('org.example.b', rendered.decode())
        self.assertNotIn('${{ bad }}', rendered.decode())
        self.assertEqual(saved_packages.render(rendered, catalog), rendered)
        self.assertEqual(saved_packages.render(rendered, []), saved_packages.render(original, []))
        before, after = original.split(saved_packages.BEGIN)
        self.assertTrue(rendered.startswith(before + saved_packages.BEGIN))
        self.assertTrue(rendered.endswith(saved_packages.END + after.split(saved_packages.END)[1]))

    def test_selection_is_by_registered_id_not_name_and_never_updates_all(self):
        data = {'sources': [{'repo': 'x/y', 'packages': {'org.example.a': '工具'}}]}
        self.assertEqual(saved_packages.select(data, 'update', 'all', '', '', '',
                         '旧名称 [org.example.a]'), ('packages', 'org.example.a'))
        self.assertEqual(saved_packages.select(data, 'remove', 'all', '', '', '',
                         '新名称 [org.example.a]'), ('packages', 'org.example.a'))
        for action, repo, packages, section, label in [
                ('update', '', '', '', '旧名称 [org.example.removed]'),
                ('update', '', 'org.example.a', '', '名称 [org.example.a]'),
                ('update', 'x/y', '', '', '名称 [org.example.a]'),
                ('update', '', '', '工具', '名称 [org.example.a]'),
                ('add', '', '', '', '名称 [org.example.a]'),
                ('list', '', '', '', '名称 [org.example.a]'),
                ('update', '', '', '', 'invalid')]:
            with self.subTest(action=action, label=label), self.assertRaises(ValueError):
                saved_packages.select(data, action, 'all', repo, packages, section, label)
        self.assertEqual(saved_packages.select(data, 'update', 'all', '', '', '', saved_packages.NONE), ('all', ''))


class SavedIntegrationTest(unittest.TestCase):
    write_config = fixtures.UpstreamSyncTest.write_config
    package = fixtures.UpstreamSyncTest.package
    snapshot = fixtures.UpstreamSyncTest.snapshot
    def setUp(self):
        fixtures.UpstreamSyncTest.setUp(self)
        for path in saved_packages.WORKFLOWS:
            workflow = self.root / path
            workflow.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, workflow)

    def test_add_remove_refreshes_options_without_changing_debs(self):
        self.package('org.example.first', '1.0', self.root / 'debs', extra='Name: 显示名称\n')
        result = sync_upstream.sync(self.root, action='remove', packages='org.example.first')
        before = self.snapshot()
        saved_packages.refresh(self.root, result, token_available=True)
        text = (self.root / saved_packages.WORKFLOW).read_text()
        self.assertNotIn('[org.example.first]', text)
        self.assertIn('[org.example.second]', text)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(saved_packages.WORKFLOW, result['paths'])
        self.assertEqual(json.loads((self.root / 'upstreams.json').read_text())['manual_sections']['org.example.first'], '乌龙')

    def test_new_registration_automatically_appears_in_choices(self):
        candidate = self.package('org.example.new', '1.0', self.root / 'upstream', extra='Name: 新插件\n')
        fixtures.UpstreamSyncTest.release(self, 'third/source', [candidate])
        with patch('sync_upstream.latest_release', return_value=self.releases['third/source']), \
                patch('sync_upstream.download_asset', side_effect=lambda asset, target: shutil.copyfile(asset['fixture'], target)):
            result = sync_upstream.sync(self.root, action='add', repo='third/source', packages='新插件', section='分组')
        self.assertFalse(result['errors'])
        saved_packages.refresh(self.root, result, token_available=True)
        self.assertIn('新插件 [org.example.new]', (self.root / saved_packages.WORKFLOW).read_text())

    def test_missing_token_does_not_silently_publish_stale_options(self):
        result = {'paths': [], 'config_update': None, 'errors': [], 'rows': []}
        before = (self.root / saved_packages.WORKFLOW).read_bytes()
        with self.assertRaisesRegex(ValueError, 'WORKFLOW_WRITE_TOKEN'):
            saved_packages.refresh(self.root, result, token_available=False)
        self.assertEqual((self.root / saved_packages.WORKFLOW).read_bytes(), before)

    def test_prepare_and_publish_stop_use_saved_id_and_keep_old_archives(self):
        self.package('org.example.first', '1.0', self.root / 'debs', extra='Name: 可变名称\n')
        before = self.snapshot()
        output = self.root / 'output'
        env = {'PATH': os.environ['PATH'], 'PERL5LIB': os.environ.get('PERL5LIB', ''),
               'RUNNER_TEMP': str(self.root), 'GITHUB_OUTPUT': str(output),
               'UPSTREAM_ENTRY': 'manage', 'UPSTREAM_OPERATION': '停止同步',
               'UPSTREAM_SAVED_PACKAGE': '旧名称 [org.example.first]',
               'WORKFLOW_WRITE_CONFIGURED': 'true', 'GITHUB_REPOSITORY': 'owner/repo'}
        with patch.dict(os.environ, env, clear=True), patch('sync_upstream.checked', side_effect=prepared_commands):
            manage_saved.prepare(self.root)
            state = json.loads(manage_saved.state_path().read_text())
        self.assertEqual(before, self.snapshot())
        self.assertEqual(output.read_text(), 'publish=true\n')
        self.assertIn(saved_packages.WORKFLOW, state['result']['paths'])
        with patch.dict(os.environ, env, clear=True), patch('sync_upstream.checked', return_value='sha'), \
                patch('sync_upstream.publish') as publish, patch('sync_upstream.write_summary'):
            manage_saved.finish(self.root, state)
        self.assertEqual(publish.call_args.args[1], state['result']['paths'])

    def test_read_only_list_does_not_write_state_or_request_publication(self):
        env = {'RUNNER_TEMP': str(self.root), 'UPSTREAM_ENTRY': 'manage', 'UPSTREAM_OPERATION': '查看清单'}
        before = (self.root / saved_packages.WORKFLOW).read_bytes()
        with patch.dict(os.environ, env, clear=True), patch('sync_upstream.checked', side_effect=prepared_commands), \
                patch('sync_upstream.write_summary'):
            manage_saved.prepare(self.root)
            self.assertFalse(manage_saved.state_path().exists())
        self.assertEqual(before, (self.root / saved_packages.WORKFLOW).read_bytes())

    def test_add_blank_only_lists_candidates_without_secret_or_publish(self):
        env = {'RUNNER_TEMP': str(self.root), 'UPSTREAM_ENTRY': 'add', 'UPSTREAM_TYPE': 'APT软件源',
               'UPSTREAM_REPO': 'https://example.org/repo'}
        record = {'Name': '候选', 'Package': 'org.example.candidate', 'Version': '1.0',
                  'Architecture': 'iphoneos-arm64e'}
        before = (self.root / 'upstreams.json').read_bytes()
        workflows = {path: (self.root / path).read_bytes() for path in saved_packages.WORKFLOWS}
        with patch.dict(os.environ, env, clear=True), patch('sync_upstream.checked', side_effect=prepared_commands), \
                patch('sync_upstream.latest_assets', return_value={'assets': [{'name': 'candidate.deb', 'record': record}]}), \
                patch('sync_upstream.download_verified') as download, patch('sync_upstream.preflight') as preflight, \
                patch('sync_upstream.write_summary') as summary:
            manage_saved.prepare(self.root)
            self.assertFalse(manage_saved.state_path().exists())
        self.assertEqual(summary.call_args.args[0]['rows'][0]['package'], 'org.example.candidate')
        download.assert_not_called()
        preflight.assert_not_called()
        self.assertEqual((self.root / 'upstreams.json').read_bytes(), before)
        self.assertEqual({path: (self.root / path).read_bytes() for path in workflows}, workflows)

    def test_missing_secret_fails_before_any_sync_or_summary_leaks(self):
        env = {'RUNNER_TEMP': str(self.root), 'UPSTREAM_ENTRY': 'update',
               'UPSTREAM_SAVED_PACKAGE': saved_packages.ALL}
        with patch.dict(os.environ, env, clear=True), patch('sync_upstream.checked', side_effect=prepared_commands), \
                patch('sync_upstream.sync') as sync, patch('sync_upstream.write_summary') as summary:
            with self.assertRaisesRegex(ValueError, 'WORKFLOW_WRITE_TOKEN'):
                manage_saved.prepare(self.root)
        sync.assert_not_called()
        self.assertIn('尚未下载', summary.call_args.args[0]['errors'][0])

    def test_selected_update_uses_confirmation_code_so_names_cannot_shadow_id(self):
        self.package('org.example.first', '1.0', self.root / 'debs')
        env = {'RUNNER_TEMP': str(self.root), 'GITHUB_OUTPUT': str(self.root / 'out'),
               'UPSTREAM_ENTRY': 'update', 'UPSTREAM_SAVED_PACKAGE': '名称 [org.example.first]',
               'WORKFLOW_WRITE_CONFIGURED': 'true'}
        result = {'paths': [], 'config_update': None, 'errors': [], 'rows': []}
        with patch.dict(os.environ, env, clear=True), patch('sync_upstream.checked', side_effect=prepared_commands), \
                patch('sync_upstream.sync', return_value=result) as sync:
            manage_saved.prepare(self.root)
        self.assertEqual(sync.call_args.kwargs['scope'], 'packages')
        self.assertTrue(sync.call_args.kwargs['packages'].startswith('@'))

    def test_changed_file_between_prepare_publish_is_rejected(self):
        state = {'captured': 'sha', 'operation': '更新插件', 'hashes': {'upstreams.json': 'wrong'},
                 'result': {'paths': [], 'errors': [], 'rows': [], 'config_update': None}}
        with patch('sync_upstream.checked', return_value='sha'), patch('sync_upstream.publish') as publish, \
                patch('sync_upstream.write_summary'), self.assertRaisesRegex(ValueError, '文件已变化'):
            manage_saved.finish(self.root, state)
        publish.assert_not_called()


class SavedPublishTest(unittest.TestCase):
    command = fixtures.PublishTest.command
    git = fixtures.PublishTest.git
    def setUp(self):
        fixtures.PublishTest.setUp(self)
        (self.root / 'debs/new.deb').unlink()
        for path in saved_packages.WORKFLOWS:
            workflow = self.root / path
            workflow.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, workflow)
        (self.root / 'upstreams.json').write_text('{"sources": []}')
        for path in saved_packages.WORKFLOWS:
            workflow = self.root / path
            workflow.write_bytes(saved_packages.render(workflow.read_bytes(), [{'name': '已移除', 'package': 'org.example.removed'}]))
        self.git('add', *saved_packages.WORKFLOWS, 'upstreams.json')
        self.git('commit', '-m', '登记工作流测试基线')
        self.git('push', 'origin', 'main')
        self.captured = self.git('rev-parse', 'HEAD')

    def render_updates(self):
        updates = {}
        for path in saved_packages.WORKFLOWS:
            workflow = self.root / path
            updates[path] = saved_packages.render(workflow.read_bytes(), [])
            workflow.write_bytes(updates[path])
        return updates

    def test_only_exact_generated_workflow_is_allowed_and_pat_push_does_not_dispatch(self):
        updates = self.render_updates()
        with patch.dict(os.environ, {'WORKFLOW_WRITE_TOKEN': 'fixture-token'}), \
                patch('sync_upstream.push_changes', side_effect=lambda root, token: self.git('push', 'origin', 'HEAD:refs/heads/main')), \
                patch('sync_upstream.checked', wraps=sync_upstream.checked) as command:
            sync_upstream.publish(self.root, list(updates), self.captured, 'owner/repo', workflow_update=updates)
        self.assertFalse(any(call.args[0][0] == 'gh' for call in command.call_args_list))

    def test_non_option_workflow_change_is_rejected_even_if_manifest_matches(self):
        for path in saved_packages.WORKFLOWS:
            with self.subTest(path=path):
                updates = self.render_updates()
                updates[path] = updates[path].replace(b'timeout-minutes: 90', b'timeout-minutes: 91')
                (self.root / path).write_bytes(updates[path])
                with patch.dict(os.environ, {'WORKFLOW_WRITE_TOKEN': 'fixture-token'}), self.assertRaisesRegex(ValueError, '选项'):
                    sync_upstream.publish(self.root, list(updates), self.captured, 'owner/repo', workflow_update=updates)

    def test_workflow_write_requires_dedicated_token(self):
        updates = self.render_updates()
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'WORKFLOW_WRITE_TOKEN'):
            sync_upstream.publish(self.root, list(updates), self.captured, 'owner/repo', workflow_update=updates)

    def test_other_workflow_path_and_manifest_mismatch_are_rejected(self):
        updates = self.render_updates()
        with patch.dict(os.environ, {'WORKFLOW_WRITE_TOKEN': 'fixture-token'}):
            with self.assertRaisesRegex(ValueError, '路径'):
                sync_upstream.publish(self.root, list(updates) + ['.github/workflows/refresh.yml'],
                                      self.captured, 'owner/repo', workflow_update=updates)
            with self.assertRaisesRegex(ValueError, '清单'):
                sync_upstream.publish(self.root, list(updates), self.captured, 'owner/repo', workflow_update={})

    def test_missing_related_workflow_update_is_rejected(self):
        updates = self.render_updates()
        updates.pop(saved_packages.WORKFLOWS[1])
        with patch.dict(os.environ, {'WORKFLOW_WRITE_TOKEN': 'fixture-token'}), self.assertRaisesRegex(ValueError, '选项'):
            sync_upstream.publish(self.root, list(updates), self.captured, 'owner/repo', workflow_update=updates)

    def test_no_change_with_pat_still_dispatches_for_recovery(self):
        with patch.dict(os.environ, {'WORKFLOW_WRITE_TOKEN': 'fixture-token'}), \
                patch('sync_upstream.checked', wraps=sync_upstream.checked) as command:
            # Stub only gh; the bare remote is a real Git fixture.
            def run(args, cwd=None):
                return '' if args[0] == 'gh' else REAL_CHECKED(args, cwd=cwd)
            command.side_effect = run
            sync_upstream.publish(self.root, [], self.captured, 'owner/repo')
        self.assertEqual(command.call_args.args[0][:4], ['gh', 'workflow', 'run', 'refresh.yml'])

    def test_push_uses_ephemeral_environment_and_redacts_failure(self):
        from types import SimpleNamespace
        with patch('sync_upstream.subprocess.run', return_value=SimpleNamespace(returncode=1, stderr='denied fixture-token')) as run:
            with self.assertRaisesRegex(ValueError, 'denied \\*\\*\\*'):
                sync_upstream.push_changes(self.root, 'fixture-token')
        self.assertNotIn('fixture-token', str(run.call_args.args))
        self.assertEqual(run.call_args.kwargs['env']['WM0104_PUSH_TOKEN'], 'fixture-token')
        self.assertFalse(pathlib.Path(run.call_args.kwargs['env']['GIT_ASKPASS']).exists())


class SavedWorkflowTest(unittest.TestCase):
    def test_dedicated_secret_is_only_in_publish_step(self):
        for filename in ('add-plugin.yml', 'sync-upstream.yml', 'manage-sync.yml'):
            with self.subTest(filename=filename):
                text = (ROOT / '.github/workflows' / filename).read_text()
                prepare, publish = text.split('      - name: Commit verified changes')
                self.assertIn('persist-credentials: false', prepare)
                self.assertNotIn('WORKFLOW_WRITE_TOKEN: ${{', prepare)
                self.assertIn("WORKFLOW_WRITE_CONFIGURED: ${{ secrets.WORKFLOW_WRITE_TOKEN != '' }}", prepare)
                self.assertIn('WORKFLOW_WRITE_TOKEN: ${{ secrets.WORKFLOW_WRITE_TOKEN }}', publish)
                self.assertIn("if: steps.prepare.outputs.publish == 'true'", publish)


if __name__ == '__main__':
    unittest.main()
