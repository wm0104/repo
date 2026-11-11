import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from build_repo import build, parse, run


class DescriptionParsingTest(unittest.TestCase):
    def test_description_keeps_continuations_and_paragraphs(self):
        fields = parse(
            'Package: org.example.plugin\nDescription: 简介\n'
            ' 第一行详细介绍\n\t第二行详细介绍\n .\n 下一段\nVersion: 1.0\n')
        self.assertEqual(fields['Description'], '简介\n第一行详细介绍\n第二行详细介绍\n\n下一段')
        self.assertEqual(fields['Version'], '1.0')

    def test_other_field_continuations_do_not_enter_description(self):
        fields = parse(
            'Description: 简介\n 详细介绍\nX-Other: kept\n ignored\n'
            'Icon: https://example.org/icon.png\n')
        self.assertEqual(fields['Description'], '简介\n详细介绍')
        self.assertEqual(fields['X-Other'], 'kept')
        self.assertEqual(fields['Icon'], 'https://example.org/icon.png')

    def test_single_line_metadata_stays_unchanged(self):
        self.assertEqual(parse('Package: org.example.plugin\nDescription: 简介\nVersion: 1.0\n'), {
            'Package': 'org.example.plugin', 'Description': '简介', 'Version': '1.0'})


class ChangelogDepictionsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name).resolve()
        (self.root / 'debs').mkdir()

    def package(self, name, version, extra):
        control = self.root / f'staging/{name}_{version}/DEBIAN/control'
        control.parent.mkdir(parents=True, exist_ok=True)
        control.write_text(
            f'Package: {name}\nVersion: {version}\nArchitecture: iphoneos-arm64e\n'
            f'Maintainer: wm0104\nDescription: {name} {version}\n{extra}\n', encoding='utf-8')
        package = self.root / f'debs/{name}_{version}.deb'
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group',
                        str(control.parents[1]), str(package)], capture_output=True, check=True)
        return package

    def build(self):
        output = self.root / '_site'
        with patch('build_repo.ROOT', self.root):
            build(output)
        return output

    def test_packages_keep_separate_histories_and_use_debian_version_order(self):
        packages = []
        for name, version, changes in [
            ('com.wm0104.first', '1.9-1', '旧版本'),
            ('com.wm0104.first', '1.10-1', '修复问题；优化下载'),
            ('org.example.second', '2.0', '旧设置'),
            ('org.example.second', '1:1.0-1', '更新设置'),
        ]:
            packages.append(self.package(name, version,
                f'X-WM0104-Changelog: {version} | 2026-11-11 | {changes}\n'
                'SileoDepiction: https://example.org/previous.json'))
        originals = {p.name: p.read_bytes() for p in packages}
        output = self.build()
        for name, versions, markdown in [
            ('com.wm0104.first', ['1.10-1', '1.9-1'], '- 修复问题\n- 优化下载'),
            ('org.example.second', ['1:1.0-1', '2.0'], '- 更新设置'),
        ]:
            path = output / f'depictions/{name}/depiction.json'
            self.assertTrue(path.is_file())
            depiction = json.loads(path.read_text())
            self.assertEqual(depiction['tabs'][0]['tabname'], '插件详细')
            self.assertEqual(depiction['tabs'][0]['views'], [{
                'class': 'DepictionMarkdownView', 'markdown': f'{name} {versions[0]}', 'useSpacing': True}])
            views = depiction['tabs'][1]['views']
            self.assertEqual([v['views'][0]['text'] for v in views[::2]], versions)
            self.assertEqual(views[1]['markdown'], markdown)
        index = (output / 'Packages').read_text()
        self.assertNotIn('x-wm0104-changelog:', index.lower())
        for block in index.strip().split('\n\n'):
            fields = dict((k.lower(), v.strip()) for line in block.splitlines()
                          for k, _, v in [line.partition(':')])
            self.assertEqual([line for line in block.splitlines() if line.lower().startswith('sileodepiction:')],
                [f'SileoDepiction: https://wm0104.github.io/repo/depictions/{fields["package"]}/depiction.json'])
            original = originals[pathlib.Path(fields['filename']).name]
            self.assertEqual(fields['sha256'], hashlib.sha256(original).hexdigest())
            self.assertEqual((output / fields['filename']).read_bytes(), original)

    def test_native_details_include_full_description_without_repacking(self):
        name = 'org.example.full-description'
        package = self.package(name, '1.0',
            ' 第一行详细介绍\n 第二行详细介绍\n .\n 下一段\n'
            'X-WM0104-Changelog: 1.0 | 2026-11-12 | 修复问题')
        original = package.read_bytes()
        output = self.build()
        depiction = json.loads((output / f'depictions/{name}/depiction.json').read_text())
        self.assertEqual(depiction['tabs'][0], {
            'class': 'DepictionStackView', 'tabname': '插件详细', 'views': [{
                'class': 'DepictionMarkdownView',
                'markdown': f'{name} 1.0\n第一行详细介绍\n第二行详细介绍\n\n下一段',
                'useSpacing': True}]})
        self.assertEqual(depiction['tabs'][1]['tabname'], '更新')
        self.assertEqual(depiction['tabs'][1]['views'][1]['markdown'], '- 修复问题')
        self.assertEqual(package.read_bytes(), original)
        self.assertEqual((output / 'debs' / package.name).read_bytes(), original)

    def test_version_without_changelog_keeps_existing_depictions(self):
        name = 'org.example.original'
        self.package(name, '1.0', 'X-WM0104-Changelog: 1.0 | 2026-11-11 | 初始版本')
        self.package(name, '2.0', 'SileoDepiction: https://example.org/native.json\n'
                     'Depiction: https://example.org/details.html')
        output = self.build()
        index = (output / 'Packages').read_text()
        block = next(b for b in index.split('\n\n') if '\nVersion: 2.0\n' in b)
        self.assertIn('sileodepiction: https://example.org/native.json', block.lower())
        self.assertIn('depiction: https://example.org/details.html', block.lower())
        self.assertEqual(block.lower().count('sileodepiction:'), 1)

    def test_new_field_takes_precedence_over_legacy_repopocket_field(self):
        self.package('com.wm0104.repopocket', '1.3.1',
            'X-WM0104-Changelog: 1.3.1 | 2026-11-11 | 新字段内容\n'
            'X-RepoPocket-Changelog: 1.3.1 | 2026-11-10 | 旧字段内容')
        output = self.build()
        depiction = json.loads((output / 'depictions/com.wm0104.repopocket/depiction.json').read_text())
        self.assertEqual(depiction['tabs'][1]['views'][1]['markdown'], '- 新字段内容')
        self.assertNotIn('changelog:', (output / 'Packages').read_text().lower())

    def test_missing_changelog_leaves_package_unchanged(self):
        self.package('com.wm0104.repopocket', '1.4.0',
                     'SileoDepiction: https://example.org/native.json')
        output = self.build()
        self.assertFalse((output / 'depictions').exists())
        self.assertIn('sileodepiction: https://example.org/native.json',
                      (output / 'Packages').read_text().lower())

    def test_invalid_new_field_does_not_fall_back_to_legacy(self):
        self.package('com.wm0104.repopocket', '1.3.1',
            'X-WM0104-Changelog: 1.0 | 2026-11-11 | 错误版本\n'
            'X-RepoPocket-Changelog: 1.3.1 | 2026-11-11 | 旧字段内容')
        with self.assertRaisesRegex(ValueError, 'invalid X-WM0104-Changelog'):
            self.build()

    def test_invalid_metadata_is_rejected(self):
        for entry in ['1.1 | 2026-11-11 | 更新', '1.0 |  | 更新',
                      '1.0 | 2026-11-11 | ']:
            with self.subTest(entry=entry):
                package = self.package('org.example.invalid', '1.0', f'X-WM0104-Changelog: {entry}')
                with self.assertRaisesRegex(ValueError, 'invalid X-WM0104-Changelog'):
                    self.build()
                package.unlink()

    def test_package_name_cannot_escape_depiction_directory(self):
        package = self.package('org.example.safe', '1.0',
            'X-WM0104-Changelog: 1.0 | 2026-11-11 | 更新')
        fields = run('dpkg-deb', '--field', str(package)).replace(
            'Package: org.example.safe', 'Package: ../../outside')
        with patch('build_repo.run', return_value=fields):
            with self.assertRaisesRegex(ValueError, 'invalid Package'):
                self.build()


if __name__ == '__main__':
    unittest.main()
