#!/usr/bin/env python3
"""Bounded, non-extracting deb audit; only repair known control permission anomalies."""
import argparse
import gzip
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import resource
import subprocess
import tarfile
import tempfile
import zlib

from build_repo import parse, LEGACY_MISSING

MAX_DEB = 512 * 1024 * 1024
MAX_CONTROL = 16 * 1024 * 1024
MAX_DATA = 512 * 1024 * 1024
SCRIPTS = {'preinst', 'postinst', 'prerm', 'postrm'}


def fail(message):
    raise ValueError(message)


def ar_members(blob):
    if not blob.startswith(b'!<arch>\n'):
        fail('ar: invalid magic')
    out, pos = [], 8
    while pos < len(blob):
        start = pos
        h = blob[pos:pos + 60]
        if len(h) != 60 or h[58:] != b'`\n':
            fail('ar: truncated/invalid header')
        try:
            name = h[:16].decode('ascii').rstrip()
            if name.endswith('/'):
                name = name[:-1]
            if not name or '/' in name or name.startswith('#1') or name in ('.', '..'):
                fail('ar: unsupported long/unsafe member name')
            if not h[48:58].strip().isdigit():
                fail('ar: invalid member length')
            size = int(h[48:58])
        except (UnicodeError, ValueError) as exc:
            fail(f'ar: {exc}')
        pos += 60
        end = pos + size
        if size > MAX_DEB or end + (size % 2) > len(blob):
            fail('ar: truncated/oversized member')
        if size % 2 and blob[end:end + 1] != b'\n':
            fail('ar: invalid padding')
        out.append((name, start, end + size % 2, h, blob[pos:end]))
        pos = end + size % 2
    names = [x[0] for x in out]
    if len(names) != len(set(names)):
        fail('ar: duplicate member')
    if not out or out[0][0] != 'debian-binary' or out[0][4] != b'2.0\n':
        fail('ar: missing/invalid debian-binary')
    controls = [x for x in out if x[0] in ('control.tar', 'control.tar.gz', 'control.tar.xz', 'control.tar.zst')]
    datas = [x for x in out if x[0] in ('data.tar', 'data.tar.gz', 'data.tar.xz', 'data.tar.zst', 'data.tar.bz2')]
    if len(controls) != 1 or len(datas) != 1 or names.index(controls[0][0]) >= names.index(datas[0][0]):
        fail('ar: require one supported control and data member in order')
    if any(n.startswith(('control.tar', 'data.tar')) and n not in (controls[0][0], datas[0][0]) for n in names):
        fail('ar: ambiguous or unsupported archive compression')
    return out, controls[0], datas[0]


def unpack(name, payload, limit):
    if name.endswith('.tar'):
        raw = payload
    elif name.endswith('.gz'):
        decoder = zlib.decompressobj(31)
        raw = decoder.decompress(payload, limit + 1)
        if len(raw) > limit or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
            fail(f'{name}: truncated, trailing or oversized gzip')
    elif name.endswith('.xz'):
        decoder = lzma.LZMADecompressor(memlimit=256 * 1024 * 1024)
        raw = decoder.decompress(payload, max_length=limit + 1)
        if len(raw) > limit or not decoder.eof or decoder.unused_data:
            fail(f'{name}: truncated, trailing or oversized xz')
    elif name.endswith('.bz2'):
        import bz2
        decoder = bz2.BZ2Decompressor()
        raw = decoder.decompress(payload, max_length=limit + 1)
        if len(raw) > limit or not decoder.eof or decoder.unused_data:
            fail(f'{name}: truncated, trailing or oversized bzip2')
    elif name.endswith('.zst'):
        with tempfile.TemporaryFile() as output:
            def bounded():
                resource.setrlimit(resource.RLIMIT_FSIZE, (limit + 1, limit + 1))
            p = subprocess.run(['zstd', '-q', '-d', '-c'], input=payload, stdout=output,
                               stderr=subprocess.PIPE, timeout=60, preexec_fn=bounded)
            if p.returncode or output.tell() > limit:
                fail(f'{name}: invalid/truncated/oversized zstd')
            output.seek(0)
            raw = output.read(limit + 1)
    else:
        fail(f'{name}: unsupported compression')
    if len(raw) > limit:
        fail(f'{name}: decompressed limit exceeded')
    return raw


def number(field):
    value = field.rstrip(b'\0 ').lstrip(b' ')
    if not value:
        return 0
    if any(c not in b'01234567' for c in value):
        fail('tar: unsupported/invalid octal numeric field')
    return int(value, 8)


def safe_path(name, root=False):
    while name.startswith('./'):
        name = name[2:]
    if name in ('', '.') and root:
        return '.'
    if not name or name.startswith('/') or '\\' in name or any(p in ('', '..') for p in name.rstrip('/').split('/')):
        fail(f'tar: unsafe path {name!r}')
    return name.rstrip('/')


def tar_headers(raw):
    if len(raw) % 512:
        fail('tar: unaligned/truncated archive')
    pos = 0
    while pos < len(raw):
        h = raw[pos:pos + 512]
        if h == bytes(512):
            if len(raw) - pos < 1024 or any(raw[pos:]):
                fail('tar: invalid end markers/trailing bytes')
            return
        if number(h[148:156]) != sum(h[:148]) + 8 * 32 + sum(h[156:]):
            fail('tar: invalid header checksum')
        size = number(h[124:136])
        end = pos + 512 + ((size + 511) // 512) * 512
        if end > len(raw):
            fail('tar: truncated payload')
        yield pos, h, size
        pos = end
    fail('tar: missing end markers')


def repair_control(raw):
    changed, seen, result = [], set(), bytearray(raw)
    for pos, h, size in tar_headers(raw):
        name = h[:100].split(b'\0')[0]
        prefix = h[345:500].split(b'\0')[0] if h[257:263] == b'ustar\0' else b''
        name = (prefix + b'/' + name if prefix else name).decode('utf-8', 'strict')
        path = safe_path(name, root=True)
        if path in seen:
            fail(f'control tar: duplicate path {path}')
        seen.add(path)
        kind = h[156:157]
        if kind not in (b'0', b'\0', b'5') or h[157:257].strip(b'\0'):
            fail(f'control tar: unsupported link/extension/type at {name}')
        if path == '.' and kind != b'5':
            fail('control tar: root is not a directory')
        if path != '.' and ('/' in path or kind == b'5'):
            fail(f'control tar: unsupported nested path {name}')
        mode = number(h[100:108])
        target = None
        if path == '.' and mode & 0o555 != 0o555:
            if mode & 0o7000:
                fail('control tar: unsafe root special bits')
            target = mode | 0o555
        elif path == 'control' and mode == 0:
            target = 0o644
        elif path in SCRIPTS and mode == 0:
            target = 0o755
        elif path == 'control' and mode & 0o444 == 0:
            fail(f'control tar: nonzero unreadable mode {mode:04o}, requires manual review')
        if target is not None:
            new = bytearray(h)
            new[100:108] = f'{target:07o}\0'.encode()
            new[148:156] = b'        '
            new[148:156] = f'{sum(new):06o}\0 '.encode()
            result[pos:pos + 512] = new
            changed.append({'path': name, 'old': f'{mode:04o}', 'new': f'{target:04o}'})
    if 'control' not in seen:
        fail('control tar: missing regular control file')
    return bytes(result), changed


def pack(name, raw):
    if name.endswith('.tar'):
        return raw
    if name.endswith('.gz'):
        return gzip.compress(raw, mtime=0)
    if name.endswith('.xz'):
        return lzma.compress(raw)
    if name.endswith('.zst'):
        return subprocess.check_output(['zstd', '-q', '-c'], input=raw, timeout=60)
    fail('unsupported control compression')


def fix_blob(blob):
    members, control, data = ar_members(blob)
    raw = unpack(control[0], control[4], MAX_CONTROL)
    newraw, changes = repair_control(raw)
    # Inspect the original control text, not dpkg's normalized field output.
    with tarfile.open(fileobj=io.BytesIO(newraw), mode='r:') as archive:
        entry = next(m for m in archive if safe_path(m.name, root=True) == 'control')
        text = archive.extractfile(entry).read().decode('utf-8', 'strict')
        paragraphs = [p for p in text.strip().split('\n\n') if p.strip()]
        if len(paragraphs) != 1:
            fail('control fields: multiple or empty paragraphs')
        keys = set()
        for line in text.splitlines():
            if not line or line.startswith((' ', '\t')):
                continue
            key, sep, value = line.partition(':')
            if not sep or key in keys:
                fail(f'control fields: duplicate/invalid field {key!r}')
            keys.add(key)
    # Validate data, without extraction, modification or executing scripts.
    dataraw = unpack(data[0], data[4], MAX_DATA)
    list(tar_headers(dataraw))
    with tarfile.open(fileobj=io.BytesIO(dataraw), mode='r:') as archive:
        for member in archive:
            safe_path(member.name, root=True)
            if member.islnk():
                safe_path(member.linkname)
    if not changes:
        return blob, []
    packed = pack(control[0], newraw)
    header = bytearray(control[3])
    header[48:58] = str(len(packed)).ljust(10).encode()
    rebuilt = blob[:control[1]] + bytes(header) + packed + (b'\n' if len(packed) % 2 else b'') + blob[control[2]:]
    after, ac, ad = ar_members(rebuilt)
    if unpack(ac[0], ac[4], MAX_CONTROL) != newraw:
        fail('repair verification: control round trip differs')
    if [blob[x[1]:x[2]] for x in members if x[0] != control[0]] != [rebuilt[x[1]:x[2]] for x in after if x[0] != control[0]]:
        fail('repair verification: non-control ar bytes changed')
    if repair_control(newraw)[1]:
        fail('repair verification: not idempotent')
    return rebuilt, changes


def audit(root, report_path):
    report = {'auto-fixed': [], 'warnings': [], 'blocking': [], 'packages': [], 'repaired_paths': []}
    seen, staged = {}, []
    paths = sorted((root / 'debs').glob('*.deb'))
    if not paths:
        report['blocking'].append({'package': 'debs', 'reason': 'No deb packages found'})
    with tempfile.TemporaryDirectory(prefix='.deb-audit-', dir=root) as temp:
        for index, path in enumerate(paths):
            relative = str(path.relative_to(root))
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_DEB:
                    fail('not a regular deb or size limit exceeded')
                original = path.read_bytes()
                fixed, changes = fix_blob(original)
                candidate = Path(temp) / f'{index}.deb'
                candidate.write_bytes(fixed)
                candidate.chmod(0o644)
                proc = subprocess.run(['dpkg-deb', '--field', str(candidate)], capture_output=True, text=True, timeout=60)
                if proc.returncode:
                    fail('control fields: ' + proc.stderr.strip())
                fields = parse(proc.stdout)
                missing = {f for f in ('Package', 'Version', 'Architecture', 'Maintainer', 'Description') if not fields.get(f)}
                allowed = LEGACY_MISSING.get((fields.get('Package'), fields.get('Version')), set())
                if missing - allowed:
                    fail(f'control fields: missing {sorted(missing - allowed)}')
                if missing:
                    report['warnings'].append({'package': relative, 'reason': f'unchanged legacy exemption: missing {sorted(missing)}'})
                if fields['Architecture'] != 'iphoneos-arm64e':
                    fail(f'architecture: unexpected {fields["Architecture"]}')
                identity = tuple(fields[f] for f in ('Package', 'Version', 'Architecture'))
                if identity in seen:
                    fail(f'duplicate identity {identity}, also {seen[identity]}')
                seen[identity] = relative
                with tempfile.TemporaryFile() as errors:
                    proc = subprocess.run(['dpkg-deb', '--contents', str(candidate)], stdout=subprocess.DEVNULL, stderr=errors, timeout=60)
                    if proc.returncode:
                        errors.seek(0)
                        fail('data archive: ' + errors.read(4096).decode(errors='replace'))
                report['packages'].append({'package': relative, 'sha256': hashlib.sha256(fixed).hexdigest(), 'status': 'auto-fixed' if changes else 'unchanged'})
                for change in changes:
                    report['auto-fixed'].append({'package': relative, **change})
                if changes:
                    staged.append((path, candidate, original))
                    report['repaired_paths'].append(relative)
            except (ValueError, OSError, EOFError, UnicodeError, lzma.LZMAError, zlib.error, tarfile.TarError, subprocess.SubprocessError) as exc:
                report['blocking'].append({'package': relative, 'reason': str(exc)})
        # Nothing in the repository is replaced until every package has passed.
        if not report['blocking']:
            for path, candidate, original in staged:
                if path.read_bytes() != original:
                    fail(f'{path}: changed during audit; refusing overwrite')
            for path, candidate, _ in staged:
                os.replace(candidate, path)
        report['applied'] = not report['blocking']
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return not report['blocking']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--report', type=Path, default=Path('deb-repair-report.json'))
    args = parser.parse_args()
    raise SystemExit(0 if audit(args.root.resolve(), args.report.resolve()) else 1)
