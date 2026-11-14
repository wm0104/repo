import importlib.util
import json
import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import build_repo

PACKAGE = 'com.wm0104.repopocket'
BASE = 'https://wm0104.github.io/repo/depictions/' + PACKAGE + '/'

class RepoPocketResourcesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name).resolve()
        (self.root / 'debs').mkdir()

    def package(self, package, version='1.2.1'):
        staging = self.root / 'staging' / package
        control = staging / 'DEBIAN/control'
        control.parent.mkdir(parents=True)
        control.write_text(f'Package: {package}\nVersion: {version}\n'
            'Architecture: iphoneos-arm64e\nMaintainer: wm0104\nDescription: 原始简介\n'
            'Icon: https://example.org/original.png\n'
            f'X-WM0104-Changelog: {version} | 2026-11-14 | 保留更新；第二条\n')
        destination = self.root / 'debs' / (package + '.deb')
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(staging), str(destination)],
                       check=True, capture_output=True)
        return destination

    def build(self):
        output = self.root / '_site'
        with patch.object(build_repo, 'ROOT', self.root):
            build_repo.build(output)
        return output

    def copy_resources(self):
        destination = self.root / 'depictions' / PACKAGE
        shutil.copytree(ROOT / 'depictions' / PACKAGE, destination)
        return destination

    def test_custom_intro_keeps_automatic_updates_and_copies_original_images(self):
        original = self.package(PACKAGE).read_bytes()
        resources = self.copy_resources()
        output = self.build()
        result = json.loads((output / 'depictions' / PACKAGE / 'depiction.json').read_text())
        expected = json.loads((resources / 'description.json').read_text())
        self.assertEqual(result['tabs'][0]['views'], expected)
        self.assertEqual(result['tabs'][1]['views'][1]['markdown'], '- 保留更新\n- 第二条')
        self.assertEqual(result['tabs'][1]['views'][0]['views'][0]['text'], '1.2.1')
        self.assertIn('Icon: ' + BASE + 'icon.png', (output / 'Packages').read_text())
        for source in resources.rglob('*'):
            if source.is_file():
                self.assertEqual(source.read_bytes(), (output / source.relative_to(self.root)).read_bytes())
        self.assertEqual((output / 'debs' / (PACKAGE + '.deb')).read_bytes(), original)

    def test_invalid_intro_is_rejected_instead_of_publishing_broken_json(self):
        self.package(PACKAGE)
        resources = self.copy_resources()
        (resources / 'description.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'description.json'):
            self.build()

    def test_other_package_is_not_given_repopocket_intro_or_icon(self):
        other = 'org.example.unrelated'
        original = self.package(other).read_bytes()
        resources = self.copy_resources()
        # A different package's directory must not supply this package's introduction.
        (resources / 'description.json').write_text('{}')
        output = self.build()
        result = json.loads((output / 'depictions' / other / 'depiction.json').read_text())
        self.assertEqual(result['tabs'][0]['views'][0]['markdown'], '原始简介')
        self.assertIn('Icon: https://example.org/original.png', (output / 'Packages').read_text())
        self.assertEqual((output / 'debs' / (other + '.deb')).read_bytes(), original)

    def test_each_package_uses_its_own_intro_icon_and_screenshot_count(self):
        self.package(PACKAGE)
        first = self.copy_resources()
        other = 'org.example.second'
        original = self.package(other, '2.0').read_bytes()
        second = self.root / 'depictions' / other
        (second / 'screenshots').mkdir(parents=True)
        shutil.copyfile(first / 'icon.png', second / 'icon.png')
        screenshots = []
        for n in range(1, 3):
            shutil.copyfile(first / f'screenshots/{n:02}.png', second / f'screenshots/{n:02}.png')
            screenshots.append({'url': f'https://wm0104.github.io/repo/depictions/{other}/screenshots/{n:02}.png',
                                'accessibilityText': f'第二个插件截图{n}'})
        views = [{'class': 'DepictionMarkdownView', 'markdown': '第二个插件介绍'},
                 {'class': 'DepictionScreenshotsView', 'itemSize': '{160, 320}',
                  'itemCornerRadius': 8, 'screenshots': screenshots}]
        (second / 'description.json').write_text(json.dumps(views))
        output = self.build()
        for package, expected in [(PACKAGE, json.loads((first / 'description.json').read_text())), (other, views)]:
            result = json.loads((output / 'depictions' / package / 'depiction.json').read_text())
            self.assertEqual(result['tabs'][0]['views'], expected)
            self.assertEqual(result['tabs'][1]['views'][1]['markdown'], '- 保留更新\n- 第二条')
        index = (output / 'Packages').read_text()
        self.assertIn(f'Icon: https://wm0104.github.io/repo/depictions/{other}/icon.png', index)
        self.assertEqual((output / 'debs' / (other + '.deb')).read_bytes(), original)

    def test_directory_config_works_without_package_changelog(self):
        other = 'org.example.no-log'
        package = self.package(other)
        control = self.root / 'staging' / other / 'DEBIAN/control'
        control.write_text('\n'.join(line for line in control.read_text().splitlines()
                                    if not line.startswith('X-WM0104-Changelog:')) + '\n')
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(control.parents[1]), str(package)],
                       check=True, capture_output=True)
        resources = self.root / 'depictions' / other
        resources.mkdir(parents=True)
        views = [{'class': 'DepictionMarkdownView', 'markdown': '无日志插件介绍'}]
        (resources / 'description.json').write_text(json.dumps(views))
        output = self.build()
        depiction = json.loads((output / 'depictions' / other / 'depiction.json').read_text())
        self.assertEqual(depiction['tabs'][0]['views'], views)
        self.assertEqual(depiction['tabs'][1]['views'][0]['text'], '无法查看此软件包的更新日志。')
        self.assertIn(f'SileoDepiction: https://wm0104.github.io/repo/depictions/{other}/depiction.json',
                      (output / 'Packages').read_text())

    def test_screenshot_urls_dimensions_and_inline_star(self):
        directory = ROOT / 'depictions' / PACKAGE
        views = json.loads((directory / 'description.json').read_text())
        screenshots = next(v for v in views if v['class'] == 'DepictionScreenshotsView')
        self.assertEqual(len(screenshots['screenshots']), 5)
        for n, shot in enumerate(screenshots['screenshots'], 1):
            self.assertEqual(shot['url'], BASE + f'screenshots/{n:02}.png')
            image = (directory / f'screenshots/{n:02}.png').read_bytes()
            self.assertEqual(image[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack('>II', image[16:24]), (1290, 2796))
            self.assertTrue(shot['accessibilityText'])
        self.assertIn('[RepoPocket 点个 Star](https://github.com/wm0104/RepoPocket)', views[-1]['markdown'])
        self.assertFalse(any(v['class'] == 'DepictionButtonView' for v in views))

if __name__ == '__main__':
    unittest.main()
