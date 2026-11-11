import bz2
import gzip
import hashlib
import io
import lzma
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import upstream_apt
import sync_upstream
import build_repo
import test_unified_management as management


class Response(io.BytesIO):
    def geturl(self):
        return 'https://example.org/repo/Packages'


class AptIndexTest(unittest.TestCase):
    def test_all_supported_compressions_and_404_fallback(self):
        text = b'Package: org.example.test\nVersion: 1.0\nArchitecture: iphoneos-arm64e\n'
        for suffix, pack in [('.xz', lzma.compress), ('.bz2', bz2.compress), ('.gz', gzip.compress), ('', lambda b: b)]:
            calls = []

            def response(request, timeout):
                calls.append(request.full_url)
                if request.full_url != 'https://example.org/repo/Packages' + suffix:
                    raise urllib.error.HTTPError(request.full_url, 404, 'missing', {}, None)
                return Response(pack(text))

            with self.subTest(suffix=suffix), patch('upstream_apt.urllib.request.urlopen', side_effect=response):
                self.assertEqual(upstream_apt.read_index('https://example.org/repo/'), text.decode())
                self.assertEqual(calls[-1], 'https://example.org/repo/Packages' + suffix)

    def test_index_size_corruption_permission_and_https_redirect_are_rejected(self):
        for data in [b'not xz', lzma.compress(b'x' * 200)]:
            with self.subTest(data=data[:5]), patch('upstream_apt.MAX_INDEX', 100), \
                    patch('upstream_apt.urllib.request.urlopen', return_value=Response(data)):
                with self.assertRaises(ValueError):
                    upstream_apt.read_index('https://example.org/repo/')
        with patch('upstream_apt.urllib.request.urlopen', side_effect=urllib.error.HTTPError('url', 403, 'forbidden', {}, None)):
            with self.assertRaises(urllib.error.HTTPError):
                upstream_apt.read_index('https://example.org/repo/')
        response = Response(b'')
        response.geturl = lambda: 'http://example.org/Packages'
        with patch('upstream_apt.urllib.request.urlopen', return_value=response):
            with self.assertRaisesRegex(ValueError, 'HTTPS'):
                upstream_apt.read_index('https://example.org/repo/')

    def test_selects_highest_debian_version_for_only_requested_architecture(self):
        blocks = []
        for package, version, arch in [('org.example.Test', '1.9', 'iphoneos-arm64e'),
                                       ('org.example.Test', '1.10', 'iphoneos-arm64e'),
                                       ('org.example.Test', '9.0', 'iphoneos-arm64'),
                                       ('org.example.other', '2.0', 'iphoneos-arm64e')]:
            blocks.append(f'Package: {package}\nVersion: {version}\nArchitecture: {arch}\n')
        with patch('upstream_apt.read_index', return_value='\n'.join(blocks)):
            result = upstream_apt.latest_assets('https://example.org/repo/', ['org.example.Test'], 'iphoneos-arm64e')
        self.assertEqual(len(result['assets']), 1)
        self.assertEqual(result['assets'][0]['record']['Version'], '1.10')

    def test_filename_digest_and_source_url_validation(self):
        fields = {'Filename': './debs/plugin%20test.deb', 'SHA256': 'a' * 64, 'Size': '123'}
        self.assertEqual(upstream_apt.asset_metadata('https://example.org/repo/', fields),
                         ('https://example.org/repo/debs/plugin%20test.deb', 'plugin test.deb', 123, 'a' * 64))
        url, name, _, _ = upstream_apt.asset_metadata('https://example.org/repo/', {**fields, 'Filename': './debs/插件 1.deb'})
        self.assertEqual(name, '插件 1.deb')
        self.assertEqual(url, 'https://example.org/repo/debs/%E6%8F%92%E4%BB%B6%201.deb')
        for filename in ['../outside.deb', '%2e%2e/bad.deb', '//evil.org/x.deb', 'https://evil.org/x.deb',
                         '/abs.deb', 'debs/x.deb?token=1', 'file:///tmp/x.deb']:
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                upstream_apt.asset_metadata('https://example.org/repo/', {**fields, 'Filename': filename})
        for override in [{'SHA256': ''}, {'SHA256': 'bad'}, {'Size': '-1'}]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                upstream_apt.asset_metadata('https://example.org/repo/', {**fields, **override})
        for url in ['http://example.org/', 'file:///tmp/', 'https://user:pass@example.org/', 'https://example.org/?token=x']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                build_repo.validate_sources([{'type': 'apt', 'repo': url, 'packages': {'org.example.Test': '工具'}}])

    def test_ambiguous_highest_version_is_isolated_from_another_package(self):
        records = [
            'Package: org.example.bad\nVersion: 1.0\nArchitecture: iphoneos-arm64e\nFilename: one.deb',
            'Package: org.example.bad\nVersion: 1.0\nArchitecture: iphoneos-arm64e\nFilename: two.deb',
            'Package: org.example.good\nVersion: 1.0\nArchitecture: iphoneos-arm64e\nFilename: good.deb',
        ]
        with patch('upstream_apt.read_index', return_value='\n\n'.join(records)):
            result = upstream_apt.latest_assets('https://example.org/', ['org.example.bad', 'org.example.good'], 'iphoneos-arm64e')
        self.assertIn('error', result['assets'][0])
        self.assertEqual(result['assets'][1]['record']['Package'], 'org.example.good')

    def test_verified_download_rejects_wrong_digest_and_size(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / 'test.deb'
            data = b'package'
            for size, digest in [(len(data), '0' * 64), (len(data) + 1, hashlib.sha256(data).hexdigest()),
                                 (0, hashlib.sha256(data).hexdigest())]:
                with self.subTest(size=size), patch('sync_upstream.urllib.request.urlopen', return_value=Response(data)):
                    with self.assertRaises(ValueError):
                        sync_upstream.download_verified('https://example.org/test.deb', target, size, digest, 'test.deb')


class AptIntegrationTest(unittest.TestCase):
    setUp = management.ManagementTest.setUp
    write_config = management.ManagementTest.write_config
    config_now = management.ManagementTest.config_now
    package = management.ManagementTest.package

    def apt_sync(self, package, record, **kwargs):
        def download(url, target, size, digest, name):
            self.assertEqual(size, package.stat().st_size)
            self.assertEqual(digest, hashlib.sha256(package.read_bytes()).hexdigest())
            target.write_bytes(package.read_bytes())
        with patch('upstream_apt.read_index', return_value='\n'.join(f'{k}: {v}' for k, v in record.items()) + '\n'), \
                patch('sync_upstream.download_verified', side_effect=download):
            return sync_upstream.sync(self.root, **kwargs)

    def record(self, package, name='org.example.Test', version='1.0', arch='iphoneos-arm64e'):
        return {'Package': name, 'Version': version, 'Architecture': arch,
                'Filename': './debs/' + package.name, 'Size': str(package.stat().st_size),
                'SHA256': hashlib.sha256(package.read_bytes()).hexdigest()}

    def test_add_apt_source_then_update_uses_saved_type_and_section(self):
        item = self.package('org.example.Test')
        kwargs = {'action': 'add', 'source_type': 'apt', 'repo': 'https://example.org/repo',
                  'packages': 'org.example.Test', 'section': '工具'}
        result = self.apt_sync(item, self.record(item), **kwargs)
        self.assertFalse(result['errors'])
        source = self.config_now()['sources'][1]
        self.assertEqual(source, {'type': 'apt', 'repo': 'https://example.org/repo/', 'packages': {'org.example.Test': '工具'}})
        self.assertTrue((self.root / 'debs' / item.name).exists())
        result = self.apt_sync(item, self.record(item), scope='repo', repo='https://example.org/repo/')
        self.assertEqual(result['paths'], [])
        self.assertFalse(result['errors'])

    def test_wrong_index_identity_architecture_or_hash_never_imports_or_registers(self):
        item = self.package('org.example.Test')
        for override in [{'Version': '2.0'}, {'Architecture': 'iphoneos-arm64'}, {'SHA256': ''}]:
            with self.subTest(override=override):
                result = self.apt_sync(item, {**self.record(item), **override}, action='add', source_type='apt',
                                       repo='https://example.org/repo/', packages='org.example.Test', section='工具')
                self.assertTrue(result['errors'])
                self.assertEqual(result['paths'], [])
                self.assertEqual(self.config_now(), self.config)


if __name__ == '__main__':
    unittest.main()
