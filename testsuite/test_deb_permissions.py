import contextlib, io, json, sys, tarfile, tempfile, unittest, gzip
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import repair_deb_permissions as r

def tar(entries):
    out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w',format=tarfile.USTAR_FORMAT) as t:
        for name,mode,content,kind in entries:
            x=tarfile.TarInfo(name); x.mode=mode; x.uid=501; x.gid=20; x.mtime=123; x.type=kind; x.size=len(content); t.addfile(x,io.BytesIO(content))
    return out.getvalue()
def member(n,p):
    return f'{n+"/":16}{123:12}{501:6}{20:6}{"100644":8}{len(p):10}`\n'.encode()+p+(b'\n' if len(p)%2 else b'')
def deb(package='org.test.one',mode=0,compression='gz',fields=None):
    text=fields or f'Package: {package}\nVersion: 1.0\nArchitecture: iphoneos-arm64e\nMaintainer: Test\nDescription: synthetic\n'
    raw=tar([('.',0o755,b'',tarfile.DIRTYPE),('./control',mode,text.encode(),tarfile.REGTYPE),('./postinst',mode,b'#!/bin/sh\nnever-execute\n',tarfile.REGTYPE)])
    n='control.tar'+('.'+compression if compression else '')
    data=r.pack('data.tar.xz',tar([('./usr/test',0o644,b'unchanged',tarfile.REGTYPE)]))
    return b'!<arch>\n'+member('debian-binary',b'2.0\n')+member(n,r.pack(n,raw))+member('data.tar.xz',data)+member('extra',b'other')
class RepairTests(unittest.TestCase):
    def test_compressions_packages_raw_preservation(self):
        for c in ('','gz','xz','zst'):
            for p in ('org.test.one','net.test.two'):
                blob=deb(p,compression=c); fixed,changes=r.fix_blob(blob)
                self.assertEqual([(x['old'],x['new']) for x in changes],[('0000','0644'),('0000','0755')]); self.assertEqual(r.fix_blob(fixed),(fixed,[]))
                bm,bc,_=r.ar_members(blob); am,ac,_=r.ar_members(fixed)
                self.assertEqual([blob[x[1]:x[2]] for x in bm if x!=bc],[fixed[x[1]:x[2]] for x in am if x!=ac])
                old=r.unpack(bc[0],bc[4],r.MAX_CONTROL); new=r.unpack(ac[0],ac[4],r.MAX_CONTROL); allowed=set()
                for pos,h,s in r.tar_headers(old):
                    allowed.update(range(pos+100,pos+108)); allowed.update(range(pos+148,pos+156))
                self.assertTrue({i for i,(a,b) in enumerate(zip(old,new)) if a!=b}<=allowed)
    def test_custom_modes(self):
        for m in (0o644,0o600,0o640,0o755,0o700):
            b=deb(mode=m); self.assertEqual(r.fix_blob(b),(b,[]))
    def test_corrupt_long_names_and_limits(self):
        b=deb()
        for bad in (b[:-1],b'broken',b.replace(b'control.tar.gz/',b'#1/1234567890 /',1)):
            with self.assertRaises(ValueError): r.fix_blob(bad)
        with self.assertRaises(ValueError): r.unpack('control.tar.gz',gzip.compress(b'a')[:-2],100)
        with self.assertRaises(ValueError): list(r.tar_headers(bytes(512)))
        with self.assertRaises(ValueError): list(r.tar_headers(b'x'*1024))
        with self.assertRaises(ValueError): r.unpack('control.tar.gz',gzip.compress(bytes(1000)),10)
    def test_paths_links_root(self):
        for n,k in (('../control',tarfile.REGTYPE),('/control',tarfile.REGTYPE),('control',tarfile.SYMTYPE),('control',tarfile.LNKTYPE)):
            with self.assertRaises(ValueError): r.repair_control(tar([(n,0,b'',k)]))
        raw=tar([('.',0,b'',tarfile.DIRTYPE),('control',0o644,b'Package: x\n',tarfile.REGTYPE)])
        self.assertEqual(r.repair_control(raw)[1][0]['new'],'0555')
    def test_fields_paragraphs(self):
        for f in ('Package: a\nPackage: b\n','Package: a\n\nPackage: b\n'):
            with self.assertRaises(ValueError): r.fix_blob(deb(fields=f))
    def test_blocking_no_partial(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'debs').mkdir(); blobs={'a.deb':deb(),'b.deb':deb(mode=0o644),'c.deb':deb(fields='Package: missing\n'),'d.deb':b'broken'}
            for n,b in blobs.items(): (root/'debs'/n).write_bytes(b)
            with contextlib.redirect_stdout(io.StringIO()): self.assertFalse(r.audit(root,root/'report.json'))
            self.assertEqual(len(json.loads((root/'report.json').read_text())['blocking']),3)
            for n,b in blobs.items(): self.assertEqual((root/'debs'/n).read_bytes(),b)
    def test_mixed_and_idempotence(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'debs').mkdir(); valid=deb('net.test.valid',mode=0o644)
            (root/'debs'/'valid.deb').write_bytes(valid); (root/'debs'/'bad.deb').write_bytes(deb())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(r.audit(root,root/'report.json')); self.assertTrue(r.audit(root,root/'report.json'))
            self.assertEqual((root/'debs'/'valid.deb').read_bytes(),valid); self.assertEqual(json.loads((root/'report.json').read_text())['repaired_paths'],[])
