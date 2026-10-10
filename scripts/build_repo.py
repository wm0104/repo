#!/usr/bin/env python3
"""Validate debs and build an atomic GitHub Pages/APT artifact."""
import argparse
import bz2
import email.utils
import hashlib
import lzma
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Existing upstream packages only. Never silently accept new incomplete packages.
LEGACY_MISSING = {
    ('cn.wkk.swipeextenderx', '0.0.2'): {'Description'},
    ('llld.keyboard', '2.0'): {'Maintainer'},
}


def run(*args):
    return subprocess.check_output(args, text=True)


def parse(text):
    fields = {}
    for line in text.splitlines():
        if line.startswith((' ', '\t')) or not line:
            continue
        key, sep, value = line.partition(':')
        if not sep:
            raise ValueError(f'Invalid control line: {line!r}')
        fields[key] = value.strip()
    return fields


def build(output):
    if output == ROOT or ROOT not in output.parents:
        raise ValueError('Output must be a subdirectory of the repository')
    packages = sorted((ROOT / 'debs').glob('*.deb'))
    if not packages:
        raise ValueError('No deb packages found')
    seen = set()
    for package in packages:
        fields = parse(run('dpkg-deb', '--field', str(package)))
        missing = {f for f in ('Package', 'Version', 'Architecture', 'Maintainer', 'Description') if not fields.get(f)}
        allowed = LEGACY_MISSING.get((fields.get('Package'), fields.get('Version')), set())
        if missing - allowed:
            raise ValueError(f'{package.name}: missing {sorted(missing - allowed)}')
        if missing:
            print(f'WARNING: legacy package {package.name}: missing {sorted(missing)}', file=sys.stderr)
        if fields['Architecture'] != 'iphoneos-arm64e':
            raise ValueError(f'{package.name}: unexpected architecture {fields["Architecture"]}')
        identity = tuple(fields[f] for f in ('Package', 'Version', 'Architecture'))
        if identity in seen:
            raise ValueError(f'Duplicate package/version/architecture: {identity}')
        seen.add(identity)
        # Read the complete data archive to catch broken/truncated debs, not just control metadata.
        subprocess.run(['dpkg-deb', '--contents', str(package)], stdout=subprocess.DEVNULL, check=True)

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    shutil.copytree(ROOT / 'debs', output / 'debs')
    for name in ('index.html', 'CydiaIcon.png', 'README.md'):
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, output / name)
    for name in ('depictions', 'assets'):
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, output / name)
    (output / '.nojekyll').touch()
    result = subprocess.run(['dpkg-scanpackages', '--multiversion', 'debs', '/dev/null'], cwd=output, capture_output=True, text=True, check=True)
    print(result.stderr, file=sys.stderr, end='')
    # Compute hashes with Python for identical results across host toolchains.
    blocks = []
    for block in result.stdout.strip().split('\n\n'):
        record = parse(block)
        file = output / record['Filename']
        if file.parent != output / 'debs':
            raise ValueError('Unexpected index path')
        content = file.read_bytes()
        if len(content) != int(record['Size']):
            raise ValueError(f'Index size mismatch: {file.name}')
        lines = [line for line in block.splitlines() if not line.startswith(('MD5sum:', 'SHA1:', 'SHA256:'))]
        for field, algorithm in [('MD5sum', 'md5'), ('SHA1', 'sha1'), ('SHA256', 'sha256')]:
            lines.append(f'{field}: {hashlib.new(algorithm, content).hexdigest()}')
        blocks.append('\n'.join(lines))
    data = ('\n\n'.join(blocks) + '\n\n').encode()
    if len(blocks) != len(packages):
        raise ValueError('Index/package count mismatch')
    (output / 'Packages').write_bytes(data)
    (output / 'Packages.bz2').write_bytes(bz2.compress(data, compresslevel=9))
    (output / 'Packages.xz').write_bytes(lzma.compress(data, preset=9))
    subprocess.run(['zstd', '-q', '-19', '-k', str(output / 'Packages'), '-o', str(output / 'Packages.zst')], check=True)
    if bz2.decompress((output / 'Packages.bz2').read_bytes()) != data or lzma.decompress((output / 'Packages.xz').read_bytes()) != data:
        raise ValueError('Compressed index mismatch')
    if subprocess.check_output(['zstd', '-q', '-d', '-c', str(output / 'Packages.zst')]) != data:
        raise ValueError('Zstandard index mismatch')
    release = [
        'Origin: wm0104(roothide)', 'Label: wm0104', 'Suite: stable',
        'Version: 1.0', 'Codename: ios', 'Architectures: iphoneos-arm64e',
        'Components: main', 'Description: wm0104 RootHide plugin backup repository',
        'Date: ' + email.utils.formatdate(usegmt=True), 'SHA256:',
    ]
    for name in ('Packages', 'Packages.bz2', 'Packages.xz', 'Packages.zst'):
        content = (output / name).read_bytes()
        release.append(f' {hashlib.sha256(content).hexdigest()} {len(content)} {name}')
    (output / 'Release').write_text('\n'.join(release) + '\n')
    print(f'Validated {len(packages)} packages; site ready at {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='_site')
    args = parser.parse_args()
    try:
        build((ROOT / args.output).resolve())
    except (ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(f'ERROR: {exc}')
