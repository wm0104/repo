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

    def test_crlf_normalizes_without_splitting_unicode_description_content(self):
        for separator in ('\u2028', '\u2029', '\u0085'):
            with self.subTest(separator=repr(separator)):
                body = f'正文{separator}X-WM0104-Release-Date: 2026-11-12{separator}X-Other: 正文'
                fields = parse(f'Package: org.example.plugin\r\nDescription: 简介\r\n {body}\r\nVersion: 1.0\r\n')
                self.assertEqual(fields, {'Package': 'org.example.plugin',
                    'Description': f'简介\n{body}', 'Version': '1.0'})

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

    def test_date_only_history_shows_native_unavailable_label_without_empty_headers(self):
        name = 'org.example.no-changes'
        for version in ('1.9-1', '1.10-1'):
            self.package(name, version, 'X-WM0104-Release-Date: 2026-11-12')
        output = self.build()
        depiction = json.loads((output / f'depictions/{name}/depiction.json').read_text())
        self.assertEqual(depiction['tabs'][0]['views'][0]['markdown'], f'{name} 1.10-1')
        self.assertEqual(depiction['tabs'][1]['views'], [{
            'class': 'DepictionLabelView', 'text': '无法查看此软件包的更新日志。'}])
        for package in (self.root / 'debs').glob('*.deb'):
            self.assertEqual(parse(run('dpkg-deb', '--field', str(package)))[
                'X-WM0104-Release-Date'], '2026-11-12')

    def test_release_date_alone_generates_details_and_connects_only_opted_in_version(self):
        name = 'org.example.date-only'
        package = self.package(name, '1.10',
            'X-WM0104-Release-Date: 2026-11-12\n'
            'SileoDepiction: https://example.org/old.json\n'
            'Depiction: https://example.org/details.html')
        original = package.read_bytes()
        self.package(name, '2.0', 'SileoDepiction: https://example.org/other.json')
        output = self.build()
        depiction = json.loads((output / f'depictions/{name}/depiction.json').read_text())
        self.assertEqual(depiction['tabs'][0]['views'][0]['markdown'], f'{name} 1.10')
        views = depiction['tabs'][1]['views']
        self.assertEqual(views, [{'class': 'DepictionLabelView',
            'text': '无法查看此软件包的更新日志。'}])
        blocks = [parse(b) for b in (output / 'Packages').read_text().strip().split('\n\n')]
        records = {b['Version']: {k.lower(): v for k, v in b.items()} for b in blocks}
        self.assertEqual(records['1.10']['sileodepiction'],
            f'https://wm0104.github.io/repo/depictions/{name}/depiction.json')
        self.assertEqual(records['1.10']['depiction'], 'https://example.org/details.html')
        self.assertEqual(records['2.0']['sileodepiction'], 'https://example.org/other.json')
        self.assertNotIn('x-wm0104-release-date:', (output / 'Packages').read_text().lower())
        self.assertEqual(package.read_bytes(), original)
        self.assertEqual((output / 'debs' / package.name).read_bytes(), original)
        self.assertEqual(records['1.10']['sha256'], hashlib.sha256(original).hexdigest())
        self.assertFalse(list((output / 'depictions').rglob('*.html')))

    def test_clear_current_changes_keeps_history_and_uses_highest_participating_description(self):
        name = 'org.example.clear'
        self.package(name, '1.9-1', 'X-WM0104-Changelog: 1.9-1 | 2026-11-10 | 历史更新')
        current = self.package(name, '1.10-1',
            ' 当前多行详细介绍\nX-WM0104-Release-Date: 2026-11-12\n'
            'X-WM0104-Changelog: 1.10-1 | 2026-11-11 | 待清空内容')
        self.build()
        # Fila rewrites its current artifact without the changelog field, retaining the date.
        self.package(name, '1.10-1',
            ' 当前多行详细介绍\nX-WM0104-Release-Date: 2026-11-12')
        output = self.build()
        depiction = json.loads((output / f'depictions/{name}/depiction.json').read_text())
        self.assertEqual(depiction['tabs'][0]['views'][0]['markdown'],
            f'{name} 1.10-1\n当前多行详细介绍')
        views = depiction['tabs'][1]['views']
        self.assertEqual(len(views), 2)
        self.assertEqual([v['views'][0]['text'] for v in views if v['class'] == 'DepictionStackView'],
            ['1.9-1'])
        self.assertEqual(views[1]['markdown'], '- 历史更新')
        self.assertNotIn('待清空内容', json.dumps(depiction, ensure_ascii=False))
        self.assertEqual((output / 'debs' / current.name).read_bytes(), current.read_bytes())

    def test_same_day_versions_stay_separate_in_debian_order_with_latest_details(self):
        name = 'org.example.same-day'
        for version, changes in [('1.9-1', '旧版同日更新'), ('1.10-1', '新版同日更新'),
                                 ('1:1.0-1', None)]:
            extra = 'X-WM0104-Release-Date: 2026-11-12'
            if changes:
                extra += f'\nX-WM0104-Changelog: {version} | 2026-11-12 | {changes}'
            self.package(name, version, extra)
        output = self.build()
        depiction = json.loads((output / f'depictions/{name}/depiction.json').read_text())
        self.assertEqual(depiction['tabs'][0]['views'][0]['markdown'], f'{name} 1:1.0-1')
        views = depiction['tabs'][1]['views']
        headers = [v for v in views if v['class'] == 'DepictionStackView']
        self.assertEqual([v['views'][0]['text'] for v in headers],
            ['1.10-1', '1.9-1'])
        self.assertEqual([v['views'][1]['text'] for v in headers], ['2026.11.12'] * 2)
        self.assertEqual([v['markdown'] for v in views if v['class'] == 'DepictionMarkdownView'],
            ['- 新版同日更新', '- 旧版同日更新'])
        self.assertEqual(len(views), 4)

    def test_release_date_overrides_old_date_without_losing_changes(self):
        for field in ('X-WM0104-Changelog', 'X-RepoPocket-Changelog'):
            with self.subTest(field=field):
                self.package('com.wm0104.repopocket', '1.0',
                    f'{field}: 1.0 | 2026-11-01 | 真实更新\n'
                    'X-WM0104-Release-Date: 2024-02-29')
                output = self.build()
                depiction = json.loads((output / 'depictions/com.wm0104.repopocket/depiction.json').read_text())
                self.assertEqual(depiction['tabs'][1]['views'][0]['views'][1]['text'], '2024.02.29')
                self.assertEqual(depiction['tabs'][1]['views'][1]['markdown'], '- 真实更新')

    def test_release_date_is_strict_valid_iso_calendar_date(self):
        for date in ('2026-02-29', '2026-04-31', '0000-01-01', '2026-13-01',
                     '2026-1-02', '20261112', '2026-W46-4', '2026-11-12T00:00:00'):
            with self.subTest(date=date):
                self.package('org.example.invalid-date', '1.0',
                    f'X-WM0104-Release-Date: {date}')
                with self.assertRaisesRegex(ValueError, 'invalid X-WM0104-Release-Date'):
                    self.build()

    def test_empty_release_date_is_rejected(self):
        package = self.package('org.example.empty-date', '1.0',
            'X-WM0104-Release-Date: 2026-11-12')
        fields = run('dpkg-deb', '--field', str(package)).replace(
            'X-WM0104-Release-Date: 2026-11-12', 'X-WM0104-Release-Date:')
        # dpkg-deb refuses to build an empty field, so inject only that control edge case.
        with patch('build_repo.run', return_value=fields):
            with self.assertRaisesRegex(ValueError, 'invalid X-WM0104-Release-Date'):
                self.build()

    def test_release_date_does_not_bypass_old_changelog_validation(self):
        for field in ('X-WM0104-Changelog', 'X-RepoPocket-Changelog'):
            for entry in ('2.0 | 2026-11-11 | 错误版本', '1.0 |  | 更新',
                          '1.0 | 2026-11-11 | ', 'broken'):
                with self.subTest(field=field, entry=entry):
                    self.package('com.wm0104.repopocket', '1.0',
                        f'X-WM0104-Release-Date: 2026-11-12\n{field}: {entry}')
                    with self.assertRaisesRegex(ValueError, f'invalid {field}'):
                        self.build()

    def test_date_only_package_name_cannot_escape_depiction_directory(self):
        package = self.package('org.example.safe', '1.0',
            'X-WM0104-Release-Date: 2026-11-12')
        fields = run('dpkg-deb', '--field', str(package)).replace(
            'Package: org.example.safe', 'Package: ../../outside')
        with patch('build_repo.run', return_value=fields):
            with self.assertRaisesRegex(ValueError, 'invalid Package'):
                self.build()

    def test_unicode_description_separators_cannot_forge_opt_in_fields(self):
        for separator in ('\u2028', '\u2029', '\u0085'):
            with self.subTest(separator=repr(separator)):
                for opted_in in (False, True):
                    name = f'org.example.unicode-{int(opted_in)}'
                    body = f'第一段{separator}X-WM0104-Release-Date: 2026-11-12{separator}末段: 正文'
                    extra = f' {body}\nSileoDepiction: https://example.org/native.json'
                    if opted_in:
                        extra += '\nX-WM0104-Release-Date: 2026-11-11'
                    package = self.package(name, '1.0', extra)
                    original = package.read_bytes()
                    output = self.build()
                    fields = next(parse(b) for b in (output / 'Packages').read_text().strip().split('\n\n')
                                  if parse(b)['Package'] == name)
                    self.assertEqual(fields['Description'], f'{name} 1.0\n{body}')
                    depiction_path = output / f'depictions/{name}/depiction.json'
                    if opted_in:
                        depiction = json.loads(depiction_path.read_text())
                        self.assertEqual(depiction['tabs'][0]['views'][0]['markdown'],
                            f'{name} 1.0\n{body}')
                        self.assertEqual(depiction['tabs'][1]['views'], [{
                            'class': 'DepictionLabelView', 'text': '无法查看此软件包的更新日志。'}])
                    else:
                        self.assertFalse(depiction_path.exists())
                        self.assertEqual({k.lower(): v for k, v in fields.items()}['sileodepiction'],
                            'https://example.org/native.json')
                    self.assertEqual(package.read_bytes(), original)
                    self.assertEqual((output / 'debs' / package.name).read_bytes(), original)

    def test_blank_multiline_description_is_missing_except_existing_legacy_allowlist(self):
        for name, version, allowed in [('org.example.blank', '1.0', False),
                                       ('cn.wkk.swipeextenderx', '0.0.2', True)]:
            with self.subTest(name=name):
                package = self.package(name, version, '')
                control = self.root / f'staging/{name}_{version}/DEBIAN/control'
                control.write_text(control.read_text().replace(
                    f'Description: {name} {version}', 'Description:\n .\n .'))
                subprocess.run(['dpkg-deb', '--build', '--root-owner-group',
                    str(control.parents[1]), str(package)], capture_output=True, check=True)
                if allowed:
                    self.build()
                else:
                    with self.assertRaisesRegex(ValueError, 'missing.*Description'):
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
