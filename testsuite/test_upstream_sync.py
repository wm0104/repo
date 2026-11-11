import hashlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import build_repo
import sync_upstream


class UpstreamSyncTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name).resolve()
        (self.root / 'debs').mkdir()
        shutil.copytree(ROOT / 'scripts', self.root / 'scripts')
        self.config = {'sources': [
            {'repo': 'owner/first', 'packages': {'org.example.first': '乌龙'}},
            {'repo': 'other/second', 'packages': {'org.example.second': '工具'}},
        ]}
        self.write_config()
        self.releases = {}

    def write_config(self):
        (self.root / 'upstreams.json').write_text(json.dumps(self.config), encoding='utf-8')

    def package(self, name, version, directory, arch='iphoneos-arm64e', extra=''):
        control = self.root / f'fixtures/{directory.name}_{name}_{version}/DEBIAN/control'
        control.parent.mkdir(parents=True, exist_ok=True)
        control.write_text(
            f'Package: {name}\nVersion: {version}\nArchitecture: {arch}\n'
            f'Maintainer: test\nDescription: {name}\nSection: Development\n{extra}\n', encoding='utf-8')
        directory.mkdir(parents=True, exist_ok=True)
        package = directory / f'{name}_{version}_{arch}.deb'
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group',
                        str(control.parents[1]), str(package)], capture_output=True, check=True)
        return package

    def release(self, repo, paths):
        self.releases[repo] = {'assets': [{
            'name': p.name, 'size': p.stat().st_size,
            'digest': 'sha256:' + hashlib.sha256(p.read_bytes()).hexdigest(),
            'browser_download_url': f'https://github.com/{repo}/releases/download/v1/{p.name}',
            'fixture': p,
        } for p in paths]}

    def sync(self, source=None):
        def download(asset, target):
            shutil.copyfile(asset['fixture'], target)
        with patch('sync_upstream.latest_release', side_effect=lambda repo: self.releases[repo]), \
                patch('sync_upstream.download_asset', side_effect=download):
            return sync_upstream.sync(self.root, source)

    def snapshot(self):
        return {p.name: p.read_bytes() for p in (self.root / 'debs').glob('*.deb')}

    def test_multiple_sources_preserve_old_packages_and_set_independent_sections(self):
        old = self.package('org.example.first', '1.0', self.root / 'debs')
        fila = self.package('wiki.qaq.fila', '0.8.4', self.root / 'debs')
        before = self.snapshot()
        first = self.package('org.example.first', '1.1', self.root / 'upstream')
        second = self.package('org.example.second', '2.0-1', self.root / 'upstream')
        self.release('owner/first', [first, fila])
        self.release('other/second', [second])
        paths = self.sync()
        self.assertEqual(set(paths), {f'debs/{first.name}', f'debs/{second.name}'})
        after = self.snapshot()
        for name, data in before.items():
            self.assertEqual(after[name], data)
        for package in [first, second]:
            self.assertEqual(after[package.name], package.read_bytes())
        with patch('build_repo.ROOT', self.root):
            build_repo.build(self.root / '_site')
        index = (self.root / '_site/Packages').read_text()
        for name, section in [('org.example.first', '乌龙'), ('org.example.second', '工具'),
                              ('wiki.qaq.fila', 'Development')]:
            for block in index.split('\n\n'):
                if block.startswith(f'Package: {name}\n'):
                    self.assertIn(f'\nSection: {section}\n', block + '\n')
        self.assertEqual(old.read_bytes(), before[old.name])

    def test_same_version_same_bytes_is_skipped_and_lower_version_is_not_added(self):
        first = self.package('org.example.first', '1.1', self.root / 'debs')
        second = self.package('org.example.second', '2.0', self.root / 'debs')
        older = self.package('org.example.second', '1.0', self.root / 'upstream')
        self.release('owner/first', [first])
        self.release('other/second', [older])
        before = self.snapshot()
        self.assertEqual(self.sync(), [])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(second.exists())

    def test_same_version_different_bytes_is_reported_without_blocking_higher_versions(self):
        self.package('org.example.first', '1.0', self.root / 'debs')
        self.package('org.example.second', '2.0', self.root / 'debs')
        first = self.package('org.example.first', '1.1', self.root / 'upstream')
        conflict = self.package('org.example.second', '2.0', self.root / 'upstream', extra='Name: Changed')
        self.release('owner/first', [first])
        self.release('other/second', [conflict])
        before = self.snapshot()
        output = io.StringIO()
        with patch('sys.stdout', output):
            self.assertEqual(self.sync(), [f'debs/{first.name}'])
        self.assertIn('同版本异文件，保留仓库原包: org.example.second 2.0', output.getvalue())
        self.assertEqual(self.snapshot(), {**before, first.name: first.read_bytes()})

    def test_only_same_version_different_bytes_keeps_all_original_packages(self):
        originals, changed = [], []
        for name, version in [('org.example.first', '1.0'), ('org.example.second', '2.0')]:
            originals.append(self.package(name, version, self.root / 'debs'))
            changed.append(self.package(name, version, self.root / 'upstream', extra='Name: Changed'))
        for repo, package in zip(['owner/first', 'other/second'], changed):
            self.release(repo, [package])
        before = self.snapshot()
        self.assertTrue(all(old.read_bytes() != new.read_bytes() for old, new in zip(originals, changed)))
        output = io.StringIO()
        with patch('sys.stdout', output), patch('sync_upstream.preflight') as preflight:
            self.assertEqual(self.sync(), [])
        preflight.assert_not_called()
        self.assertEqual(output.getvalue().count('同版本异文件，保留仓库原包:'), 2)
        self.assertIn('没有需要入库的新版本', output.getvalue())
        self.assertEqual(self.snapshot(), before)

    def test_missing_or_wrong_architecture_package_does_not_import_other_updates(self):
        first = self.package('org.example.first', '1.1', self.root / 'upstream')
        wrong = self.package('org.example.second', '2.0', self.root / 'upstream', arch='iphoneos-arm64')
        self.release('owner/first', [first])
        self.release('other/second', [wrong])
        with self.assertRaisesRegex(ValueError, '缺少.*org.example.second'):
            self.sync()
        self.assertEqual(self.snapshot(), {})

    def test_existing_destination_is_never_overwritten(self):
        first = self.package('org.example.first', '1.1', self.root / 'upstream')
        collision = self.root / 'debs' / first.name
        unrelated = self.package('org.example.unrelated', '1.0', self.root / 'fixtures2')
        collision.write_bytes(unrelated.read_bytes())
        second = self.package('org.example.second', '2.0', self.root / 'upstream')
        self.release('owner/first', [first])
        self.release('other/second', [second])
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, '文件名冲突'):
            self.sync()
        self.assertEqual(self.snapshot(), before)

    def test_preflight_rejects_package_that_requires_permission_repair(self):
        first = self.package('org.example.first', '1.1', self.root / 'upstream')
        second = self.package('org.example.second', '2.0', self.root / 'upstream')
        self.release('owner/first', [first])
        self.release('other/second', [second])
        with patch('sync_upstream.preflight', side_effect=ValueError('需要权限修复')):
            with self.assertRaisesRegex(ValueError, '需要权限修复'):
                self.sync()
        self.assertEqual(self.snapshot(), {})

    def test_config_rejects_duplicate_ownership_and_section_injection(self):
        for config in [
            {'sources': [{'repo': 'x/y', 'packages': {'org.example.x': 'a'}},
                         {'repo': 'x/z', 'packages': {'org.example.x': 'b'}}]},
            {'sources': [{'repo': 'x/y', 'packages': {'../escape': 'a'}}]},
            {'sources': [{'repo': 'x/y', 'packages': {'org.example.x': 'a\nPackage: bad'}}]},
        ]:
            with self.subTest(config=config):
                self.config = config
                self.write_config()
                with self.assertRaises(ValueError):
                    build_repo.load_upstreams(self.root)

    def test_download_requires_matching_github_digest_and_size(self):
        data = b'package'
        asset = {'name': 'test.deb', 'size': len(data), 'digest': 'sha256:' + '0' * 64,
                 'browser_download_url': 'https://github.com/x/y/releases/download/v1/test.deb'}
        target = self.root / 'download.deb'
        with patch('sync_upstream.urllib.request.urlopen', return_value=io.BytesIO(data)):
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                sync_upstream.download_asset(asset, target)

    def test_download_accepts_verified_bytes_and_rejects_truncation(self):
        data = b'package'
        asset = {'name': 'test.deb', 'size': len(data),
                 'digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
                 'browser_download_url': 'https://github.com/x/y/releases/download/v1/test.deb'}
        target = self.root / 'download.deb'
        with patch('sync_upstream.urllib.request.urlopen', return_value=io.BytesIO(data)):
            sync_upstream.download_asset(asset, target)
        self.assertEqual(target.read_bytes(), data)
        with patch('sync_upstream.urllib.request.urlopen', return_value=io.BytesIO(data[:-1])):
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                sync_upstream.download_asset(asset, target)

    def test_corrupt_asset_does_not_partially_import_valid_update(self):
        first = self.package('org.example.first', '1.1', self.root / 'upstream')
        corrupt = self.root / 'upstream/broken.deb'
        corrupt.write_bytes(b'not a deb')
        self.release('owner/first', [first])
        self.release('other/second', [corrupt])
        with self.assertRaises(ValueError):
            self.sync()
        self.assertEqual(self.snapshot(), {})

    def test_repo_change_during_preflight_prevents_import(self):
        first = self.package('org.example.first', '1.1', self.root / 'upstream')
        second = self.package('org.example.second', '2.0', self.root / 'upstream')
        self.release('owner/first', [first])
        self.release('other/second', [second])

        def concurrent_change(*args):
            self.package('org.example.manual', '1.0', self.root / 'debs')

        with patch('sync_upstream.preflight', side_effect=concurrent_change):
            with self.assertRaisesRegex(ValueError, '预检期间仓库或配置发生变化'):
                self.sync()
        self.assertEqual(list(self.snapshot()), ['org.example.manual_1.0_iphoneos-arm64e.deb'])

    def test_failed_promotion_rolls_back_only_new_files(self):
        first = self.package('org.example.first', '1.1', self.root / 'upstream')
        second = self.package('org.example.second', '2.0', self.root / 'upstream')
        self.release('owner/first', [first])
        self.release('other/second', [second])
        original_open = pathlib.Path.open

        def failing_open(path, mode='r', *args, **kwargs):
            if path == self.root / 'debs' / second.name and mode == 'xb':
                raise OSError('simulated disk error')
            return original_open(path, mode, *args, **kwargs)

        with patch('pathlib.Path.open', new=failing_open):
            with self.assertRaisesRegex(OSError, 'disk error'):
                self.sync()
        self.assertEqual(self.snapshot(), {})

    def test_single_source_imports_only_selected_package_and_persists_section(self):
        wanted = self.package('org.example.single', '1.0', self.root / 'upstream')
        other = self.package('org.example.other', '2.0', self.root / 'upstream')
        self.release('third/single', [wanted, other])
        source = {'repo': 'third/single', 'packages': {'org.example.single': '美化'}}
        paths = self.sync(source)
        self.assertEqual(set(paths), {f'debs/{wanted.name}', 'upstreams.json'})
        self.assertEqual(self.snapshot(), {wanted.name: wanted.read_bytes()})
        self.assertEqual(build_repo.load_upstreams(self.root), self.config['sources'])
        data = json.loads((self.root / 'upstreams.json').read_text())
        self.assertEqual(data['manual_sections'], {'org.example.single': '美化'})
        with patch('build_repo.ROOT', self.root):
            build_repo.build(self.root / '_site')
        self.assertIn('\nSection: 美化\n', (self.root / '_site/Packages').read_text())

    def test_single_selection_does_not_request_other_configured_packages(self):
        first = self.package('org.example.first', '1.1', self.root / 'upstream')
        self.release('owner/first', [first])
        before = (self.root / 'upstreams.json').read_bytes()
        paths = self.sync(self.config['sources'][0])
        self.assertEqual(paths, [f'debs/{first.name}'])
        self.assertEqual((self.root / 'upstreams.json').read_bytes(), before)

    def test_single_same_version_can_save_section_without_replacing_deb(self):
        old = self.package('org.example.single', '1.0', self.root / 'debs')
        changed = self.package('org.example.single', '1.0', self.root / 'upstream', extra='Name: Changed')
        self.release('third/single', [changed])
        source = {'repo': 'third/single', 'packages': {'org.example.single': '工具'}}
        before = old.read_bytes()
        self.assertEqual(self.sync(source), ['upstreams.json'])
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(self.sync(source), [])
        self.assertEqual(build_repo.load_sections(self.root)['org.example.single'], '工具')

    def test_single_failed_preflight_or_missing_asset_does_not_save_section(self):
        wanted = self.package('org.example.single', '1.0', self.root / 'upstream')
        source = {'repo': 'third/single', 'packages': {'org.example.single': '工具'}}
        before = (self.root / 'upstreams.json').read_bytes()
        self.release('third/single', [wanted])
        with patch('sync_upstream.preflight', side_effect=ValueError('预检失败')):
            with self.assertRaisesRegex(ValueError, '预检失败'):
                self.sync(source)
        self.release('third/single', [])
        with self.assertRaisesRegex(ValueError, '缺少'):
            self.sync(source)
        self.assertEqual((self.root / 'upstreams.json').read_bytes(), before)
        self.assertEqual(self.snapshot(), {})

    def test_single_invalid_input_and_batch_section_conflict_are_rejected_before_download(self):
        before = (self.root / 'upstreams.json').read_bytes()
        for source in [
            {'repo': 'x/y;echo bad', 'packages': {'org.example.single': '工具'}},
            {'repo': 'x/y', 'packages': {'../bad': '工具'}},
            {'repo': 'x/y', 'packages': {'org.example.single': 'a\nPackage: bad'}},
            {'repo': 'owner/first', 'packages': {'org.example.first': '其他'}},
            self.config['sources'][0] | {'packages': {'org.example.first': '乌龙', 'org.example.x': '工具'}},
        ]:
            with self.subTest(source=source), patch('sync_upstream.latest_release') as release:
                with self.assertRaises(ValueError):
                    sync_upstream.sync(self.root, source)
                release.assert_not_called()
        self.assertEqual((self.root / 'upstreams.json').read_bytes(), before)

    def test_single_config_promotion_failure_rolls_back_new_packages(self):
        wanted = self.package('org.example.single', '1.0', self.root / 'upstream')
        self.release('third/single', [wanted])
        before = (self.root / 'upstreams.json').read_bytes()
        source = {'repo': 'third/single', 'packages': {'org.example.single': '工具'}}
        with patch('pathlib.Path.replace', side_effect=OSError('simulated config write failure')):
            with self.assertRaisesRegex(OSError, 'config write failure'):
                self.sync(source)
        self.assertEqual(self.snapshot(), {})
        self.assertEqual((self.root / 'upstreams.json').read_bytes(), before)

    def test_manual_sections_are_validated_and_never_override_batch_sections(self):
        for manual in [[], {'../bad': '工具'}, {'org.example.single': 'a\nPackage: bad'},
                       {'org.example.first': '不同分组'}]:
            with self.subTest(manual=manual):
                self.config['manual_sections'] = manual
                self.write_config()
                with self.assertRaises(ValueError):
                    build_repo.load_sections(self.root)


class PublishTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = pathlib.Path(directory.name)
        self.remote = self.directory / 'remote.git'
        self.root = self.directory / 'work'
        self.root.mkdir()
        self.command(['git', 'init', '--bare', str(self.remote)])
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'test')
        self.git('config', 'user.email', 'test@example.org')
        (self.root / 'seed.txt').write_text('existing file')
        self.git('add', 'seed.txt')
        self.git('commit', '-m', '初始测试提交')
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', '-u', 'origin', 'main')
        self.captured = self.git('rev-parse', 'HEAD')
        (self.root / 'debs').mkdir()
        (self.root / 'debs/new.deb').write_bytes(b'validated fixture')

    def command(self, args, cwd=None):
        return subprocess.check_output(args, cwd=cwd, stderr=subprocess.PIPE, text=True).strip()

    def git(self, *args):
        return self.command(['git', *args], cwd=self.root)

    def publish(self, paths, captured=None, fail_dispatch=False, config_package=None):
        checked = sync_upstream.checked
        calls = []

        def command(args, cwd=None):
            calls.append(args)
            if args[0] == 'gh':
                if fail_dispatch:
                    raise ValueError('simulated dispatch failure')
                return ''
            return checked(args, cwd=cwd)

        with patch('sync_upstream.checked', side_effect=command):
            sync_upstream.publish(self.root, paths, captured or self.captured, 'owner/repo', config_package)
        return calls

    def test_only_new_debs_are_committed_with_chinese_message_then_publisher_is_requested(self):
        calls = self.publish(['debs/new.deb'])
        self.assertEqual(self.git('show', '--format=', '--name-only', 'HEAD'), 'debs/new.deb')
        self.assertIn('同步上游插件更新', self.git('log', '-1', '--format=%s'))
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/main').split()[0],
                         self.git('rev-parse', 'HEAD'))
        self.assertEqual(calls[-1], ['gh', 'workflow', 'run', 'refresh.yml',
                                     '--repo', 'owner/repo', '--ref', 'main'])
        self.assertFalse(any('--force' in call for call in calls))

    def test_dispatch_failure_can_be_recovered_without_another_package_commit(self):
        with self.assertRaisesRegex(ValueError, 'dispatch failure'):
            self.publish(['debs/new.deb'], fail_dispatch=True)
        committed = self.git('rev-parse', 'HEAD')
        self.assertNotEqual(committed, self.captured)
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/main').split()[0], committed)
        calls = self.publish([], captured=committed)
        self.assertEqual(self.git('rev-parse', 'HEAD'), committed)
        self.assertEqual(calls[-1][0], 'gh')

    def test_unrelated_staged_file_prevents_commit_and_dispatch(self):
        (self.root / 'other.txt').write_text('unrelated change')
        self.git('add', 'other.txt')
        with self.assertRaisesRegex(ValueError, '未授权路径'):
            self.publish(['debs/new.deb'])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.captured)
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/main').split()[0], self.captured)

    def test_manual_upload_to_remote_prevents_stale_sync_push(self):
        peer = self.directory / 'peer'
        self.command(['git', 'clone', '-b', 'main', str(self.remote), str(peer)])
        self.command(['git', 'config', 'user.name', 'test'], cwd=peer)
        self.command(['git', 'config', 'user.email', 'test@example.org'], cwd=peer)
        (peer / 'manual.txt').write_text('manual upload')
        self.command(['git', 'add', 'manual.txt'], cwd=peer)
        self.command(['git', 'commit', '-m', '用户手动上传'], cwd=peer)
        self.command(['git', 'push', 'origin', 'main'], cwd=peer)
        advanced = self.command(['git', 'rev-parse', 'HEAD'], cwd=peer)
        with self.assertRaisesRegex(ValueError, '远端 main 已更新'):
            self.publish(['debs/new.deb'])
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/main').split()[0], advanced)

    def test_unsafe_manifest_path_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '不安全'):
            self.publish(['debs/../../other.deb'])

    def test_single_publish_allows_only_selected_manual_section_and_new_debs(self):
        config = self.root / 'upstreams.json'
        config.write_text(json.dumps({'sources': [], 'manual_sections': {'org.example.single': '工具'}}))
        self.publish(['debs/new.deb', 'upstreams.json'], config_package='org.example.single')
        self.assertEqual(set(self.git('show', '--format=', '--name-only', 'HEAD').splitlines()),
                         {'debs/new.deb', 'upstreams.json'})
        config.write_text(json.dumps({'sources': [], 'manual_sections': {'org.example.single': '美化'}}))
        self.publish(['upstreams.json'], captured=self.git('rev-parse', 'HEAD'), config_package='org.example.single')
        self.assertEqual(self.git('show', '--format=', '--name-only', 'HEAD'), 'upstreams.json')

    def test_single_publish_rejects_batch_config_or_unselected_section_changes(self):
        config = self.root / 'upstreams.json'
        for data in [
            {'sources': [{'repo': 'x/y', 'packages': {'org.example.other': 'a'}}],
             'manual_sections': {'org.example.single': '工具'}},
            {'sources': [], 'manual_sections': {'org.example.single': '工具', 'org.example.other': 'a'}},
        ]:
            with self.subTest(data=data):
                config.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, '配置'):
                    self.publish(['upstreams.json'], config_package='org.example.single')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.captured)

    def test_batch_publish_rejects_config_in_manifest(self):
        with self.assertRaisesRegex(ValueError, '不安全'):
            self.publish(['upstreams.json'])


class WorkflowTest(unittest.TestCase):
    def test_single_cli_passes_only_form_selection_and_batch_cli_ignores_form_values(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_REPOSITORY': 'owner/repo', 'UPSTREAM_REPO': 'third/single',
               'UPSTREAM_PACKAGE': 'org.example.single', 'UPSTREAM_SECTION': '工具'}
        source = {'repo': 'third/single', 'packages': {'org.example.single': '工具'}}
        for arguments, expected_source, expected_package in [
            (['--single'], source, 'org.example.single'), ([], None, None),
        ]:
            with self.subTest(arguments=arguments), patch.dict(os.environ, env, clear=True), \
                    patch('sys.argv', ['sync_upstream.py', *arguments]), \
                    patch('sync_upstream.checked', side_effect=['', 'captured-sha']), \
                    patch('sync_upstream.sync', return_value=['debs/new.deb']) as sync, \
                    patch('sync_upstream.publish') as publish:
                sync_upstream.main()
                sync.assert_called_once_with(ROOT, expected_source)
                publish.assert_called_once_with(ROOT, ['debs/new.deb'], 'captured-sha', 'owner/repo', expected_package)

    def test_single_workflow_uses_inputs_as_env_and_shares_sync_lock(self):
        text = (ROOT / '.github/workflows/sync-single-upstream.yml').read_text(encoding='utf-8')
        self.assertIn('name: 同步单个上游插件', text)
        self.assertIn('workflow_dispatch:', text)
        for field in ('repo', 'package', 'section'):
            self.assertIn(f'{field}:', text)
            self.assertIn('${{ inputs.' + field + ' }}', text)
        self.assertIn('group: upstream-sync', text)
        self.assertNotIn('schedule:', text)
        self.assertNotIn('deploy-pages', text)
        self.assertIn('run: python3 scripts/sync_upstream.py --single', text)
        for line in text.splitlines():
            if 'run:' in line:
                self.assertNotIn('${{', line)

    def test_new_workflow_is_manual_only_and_has_no_second_pages_deployer(self):
        text = (ROOT / '.github/workflows/sync-upstream.yml').read_text(encoding='utf-8')
        self.assertIn('workflow_dispatch:', text)
        self.assertNotIn('schedule:', text)
        self.assertNotIn('deploy-pages', text)
        self.assertIn('actions: write', text)
        self.assertIn('group: upstream-sync', text)
        self.assertIn('python3 scripts/sync_upstream.py', text)


if __name__ == '__main__':
    unittest.main()
