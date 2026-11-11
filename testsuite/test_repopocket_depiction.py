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
from build_repo import build


class RepoPocketDepictionTest(unittest.TestCase):
    def test_package_changelog_generates_native_depiction_and_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            control = root / 'package/DEBIAN/control'
            control.parent.mkdir(parents=True)
            control.write_text(
                'Package: com.wm0104.repopocket\nVersion: 1.3.0\n'
                'Architecture: iphoneos-arm64e\nMaintainer: wm0104\nDescription: 测试\n'
                'X-RepoPocket-Changelog: 1.3.0 | 2026-11-11 | 测试\n', encoding='utf-8')
            (root / 'debs').mkdir()
            package = root / 'debs/RepoPocket_1.3.0_iphoneos-arm64e.deb'
            subprocess.run(['dpkg-deb', '--build', '--root-owner-group',
                            str(control.parents[1]), str(package)], capture_output=True, check=True)
            output = root / '_site'
            with patch('build_repo.ROOT', root):
                build(output)
            self.assertEqual((output / 'debs' / package.name).read_bytes(), package.read_bytes())
            data = (output / 'Packages').read_text()
            block = next(b for b in data.split('\n\n') if b.startswith('Package: com.wm0104.repopocket\n'))
            self.assertIn('SileoDepiction: https://wm0104.github.io/repo/depictions/com.wm0104.repopocket/depiction.json', block)
            self.assertNotIn('X-Repopocket-Changelog:', block)
            self.assertIn('SHA256: ' + hashlib.sha256(package.read_bytes()).hexdigest(), block)
            depiction = json.loads((output / 'depictions/com.wm0104.repopocket/depiction.json').read_text())
            self.assertEqual(depiction['class'], 'DepictionTabView')
            self.assertEqual([tab['tabname'] for tab in depiction['tabs']], ['插件详细', '更新'])
            self.assertEqual(depiction['tabs'][0]['views'], [
                {'class': 'DepictionMarkdownView', 'markdown': '测试', 'useSpacing': True}])
            self.assertEqual(depiction['tabs'][1]['views'], [
                {'class': 'DepictionStackView', 'orientation': 'landscape', 'views': [
                    {'class': 'DepictionLabelView', 'text': '1.3.0', 'fontSize': 17,
                     'fontWeight': 'bold', 'alignment': 0},
                    {'class': 'DepictionLabelView', 'text': '2026.11.11', 'fontSize': 17,
                     'fontWeight': 'bold', 'textColor': '#707070', 'alignment': 2},
                ]},
                {'class': 'DepictionMarkdownView', 'markdown': '- 测试'},
            ])

    def test_semicolon_separated_changes_generate_individual_bullets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            control = root / 'package/DEBIAN/control'
            control.parent.mkdir(parents=True)
            control.write_text(
                'Package: com.wm0104.repopocket\n'
                'Version: 1.3.1\n'
                'Architecture: iphoneos-arm64e\n'
                'Maintainer: wm0104\n'
                'Description: 测试\n'
                'X-RepoPocket-Changelog: 1.3.1 | 2026-11-11 | '
                '修复仓库列表刷新问题； 优化下载体验 ；调整设置页面；\n',
                encoding='utf-8')
            (root / 'debs').mkdir()
            package = root / 'debs/RepoPocket_1.3.1_iphoneos-arm64e.deb'
            subprocess.run(['dpkg-deb', '--build', '--root-owner-group',
                            str(control.parents[1]), str(package)], capture_output=True, check=True)
            original = package.read_bytes()
            output = root / '_site'
            with patch('build_repo.ROOT', root):
                build(output)
            depiction = json.loads((output / 'depictions/com.wm0104.repopocket/depiction.json').read_text())
            views = depiction['tabs'][1]['views']
            self.assertEqual(views[1], {'class': 'DepictionMarkdownView', 'markdown':
                '- 修复仓库列表刷新问题\n- 优化下载体验\n- 调整设置页面'})
            self.assertEqual(views[0]['views'][0]['text'], '1.3.1')
            self.assertEqual(views[0]['views'][1]['text'], '2026.11.11')
            self.assertEqual((output / 'debs' / package.name).read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
