import io
import lzma
import pathlib
import sys
import tarfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import repair_deb_permissions as repair


def member(name, data):
    header = (f'{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n').encode()
    return header + data + (b'\n' if len(data) % 2 else b'')


def archive(name, data, mode=0o644):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as tar:
        info = tarfile.TarInfo(name)
        info.size, info.mode = len(data), mode
        tar.addfile(info, io.BytesIO(data))
    return output.getvalue()


class LzmaDebTest(unittest.TestCase):
    def test_lzma_data_audit_preserves_original_bytes(self):
        control = archive('control', b'Package: org.example.lzma\nVersion: 1.0\n'
                          b'Architecture: iphoneos-arm64e\nMaintainer: test\nDescription: test\n')
        data = lzma.compress(archive('var/jb/test', b'payload'), format=lzma.FORMAT_ALONE)
        blob = (b'!<arch>\n' + member('debian-binary', b'2.0\n')
                + member('control.tar.gz', repair.gzip.compress(control)) + member('data.tar.lzma', data))
        self.assertEqual(repair.fix_blob(blob), (blob, []))

    def test_lzma_data_rejects_invalid_streams_and_limits(self):
        raw = b'a' * 2048
        packed = lzma.compress(raw, format=lzma.FORMAT_ALONE)
        self.assertEqual(repair.unpack('data.tar.lzma', packed, len(raw)), raw)
        for payload, limit in [(packed, len(raw) - 1), (packed[:-1], len(raw)),
                               (packed + b'trailing', len(raw)), (packed + packed, len(raw)),
                               (b'invalid', len(raw))]:
            with self.subTest(payload=payload[:16], limit=limit):
                with self.assertRaises((ValueError, lzma.LZMAError)):
                    repair.unpack('data.tar.lzma', payload, limit)
        oversized_dictionary = packed[:1] + (512 * 1024 * 1024).to_bytes(4, 'little') + packed[5:]
        with self.assertRaises(lzma.LZMAError):
            repair.unpack('data.tar.lzma', oversized_dictionary, len(raw))

    def test_lzma_does_not_accept_xz_under_wrong_extension(self):
        with self.assertRaises((ValueError, lzma.LZMAError)):
            repair.unpack('data.tar.lzma', lzma.compress(b'payload'), 100)


if __name__ == '__main__':
    unittest.main()
