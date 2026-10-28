"""Offline-only tests for the pinned three-bundle downloader."""
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

import download as dl

HERE = Path(__file__).parent

class Response:
    def __init__(self, data, status=200, headers=None, interrupt_after=None, interrupt_error=None):
        self.data = data
        self.status = status
        self.headers = headers or {}
        self.interrupt_after = interrupt_after
        self.interrupt_error = interrupt_error or OSError('synthetic interruption')
        self.reads = 0
        self.closed = False
    def __enter__(self): return self
    def __exit__(self, *_): self.closed = True
    def getcode(self): return self.status
    def read(self, n=-1):
        self.reads += 1
        if self.interrupt_after is not None and self.reads > self.interrupt_after:
            raise self.interrupt_error
        if not self.data: return b''
        part, self.data = self.data[:min(n, 3)], self.data[min(n, 3):]
        return part

class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='download-fixture-', dir='/tmp')
        self.root = Path(self.tmp.name)
        self.destination = self.root / 'bundle'
        self.payload = b'pinned fixture payload'
        self.asset = {'url':'https://invalid.example/file','local_name':'file.gguf','bytes':len(self.payload),'sha256':hashlib.sha256(self.payload).hexdigest()}
    def tearDown(self): self.tmp.cleanup()

    def test_manifest_has_exact_three_bundles_and_totals(self):
        data = dl.manifest()
        self.assertEqual(set(data['bundles']), {'runtime','qwen','gemma'})
        totals = {k:sum(a['bytes'] for a in b['assets']) for k,b in data['bundles'].items()}
        self.assertEqual(totals, {'runtime':776895574,'qwen':6598688544,'gemma':5967544256})
        self.assertEqual(data['bundles']['runtime']['runtime_license'],'llama.cpp: MIT')
        self.assertIn('NVIDIA',data['bundles']['runtime']['cuda_terms'])
        for name in ('qwen','gemma'):
            self.assertTrue(data['bundles'][name]['publisher_base_source'].startswith('https://'))
            self.assertTrue(data['bundles'][name]['conversion_source'].startswith('https://'))
            self.assertEqual(data['bundles'][name]['publisher_license'],'Apache-2.0')
            self.assertEqual(data['bundles'][name]['conversion_license'],'Apache-2.0')

    def test_wrappers_dry_run_select_one_and_do_not_download(self):
        env = os.environ.copy(); env['HOME'] = str(self.root)
        for name in ('runtime','qwen','gemma'):
            result = subprocess.run([sys.executable,str(HERE/f'{name}.py'),'--dry-run'],capture_output=True,text=True,env=env,check=True)
            obj=json.loads(result.stdout)
            self.assertEqual(obj['bundle'],name); self.assertGreater(obj['size_bytes'],0)
            self.assertEqual(Path(obj['destination']),self.root/'local-ai-guru-data'/dl.manifest()['bundles'][name]['destination'])
        self.assertFalse((self.root/'local-ai-guru-data').exists())

    def test_verified_existing_skips_network(self):
        self.destination.mkdir(); (self.destination/self.asset['local_name']).write_bytes(self.payload)
        with mock.patch.object(dl.urllib.request,'urlopen',side_effect=AssertionError('network used')):
            self.assertEqual(dl.download_asset(self.asset,self.destination),'verified-existing')

    def test_partial_resume_and_interruption_retains_bytes(self):
        self.destination.mkdir(); path=self.destination/self.asset['local_name']; path.write_bytes(self.payload[:5])
        def opener(request, timeout):
            self.assertEqual(request.headers.get('Range'),'bytes=5-')
            return Response(self.payload[5:],206,{'Content-Range':f'bytes 5-{len(self.payload)-1}/{len(self.payload)}'})
        self.assertEqual(dl.download_asset(self.asset,self.destination,opener),'downloaded-and-verified')
        self.assertEqual(path.read_bytes(),self.payload)
        path.unlink(); path.write_bytes(self.payload[:4])
        def interrupted(request, timeout):
            return Response(self.payload[4:],206,{'Content-Range':f'bytes 4-{len(self.payload)-1}/{len(self.payload)}'},interrupt_after=1)
        with self.assertRaises(dl.DownloadError): dl.download_asset(self.asset,self.destination,interrupted)
        self.assertEqual(path.read_bytes(),self.payload[:7])

    def test_progress_events_are_coarse_and_include_verification(self):
        events=[]
        response=Response(self.payload,200)
        dl.download_asset(self.asset,self.destination,lambda *_a,**_k:response,lambda _asset,stage,done,total:events.append((stage,done,total)))
        self.assertEqual(events[0][0],'starting'); self.assertTrue(any(stage.startswith('progress-') for stage,_,_ in events)); self.assertEqual(events[-1][0],'verified')

    def test_empty_manual_file_resumes_at_zero(self):
        self.destination.mkdir(); path=self.destination/self.asset['local_name']; path.touch()
        def opener(request, timeout):
            self.assertEqual(request.headers.get('Range'),'bytes=0-')
            return Response(self.payload,206,{'Content-Range':f'bytes 0-{len(self.payload)-1}/{len(self.payload)}'})
        self.assertEqual(dl.download_asset(self.asset,self.destination,opener),'downloaded-and-verified')
        self.assertEqual(path.read_bytes(),self.payload)
        path.unlink(); path.touch()
        def ignored_range(request, timeout):
            self.assertEqual(request.headers.get('Range'),'bytes=0-')
            return Response(self.payload,200)
        self.assertEqual(dl.download_asset(self.asset,self.destination,ignored_range),'downloaded-and-verified')
        self.assertEqual(path.read_bytes(),self.payload)

    def test_malformed_content_range_rejected_before_append(self):
        self.destination.mkdir(); path=self.destination/self.asset['local_name']; prefix=self.payload[:4]; path.write_bytes(prefix)
        for content_range in ('bytes 3-22/23','bytes 4-22/24','bytes 4-3/23','nonsense'):
            path.write_bytes(prefix)
            with self.assertRaises(dl.DownloadError):
                dl.download_asset(self.asset,self.destination,lambda *_a,**_k: Response(self.payload[4:],206,{'Content-Range':content_range}))
            self.assertEqual(path.read_bytes(),prefix)

    def test_http_incomplete_read_preserves_partial(self):
        self.destination.mkdir(); path=self.destination/self.asset['local_name']; path.write_bytes(self.payload[:2])
        response=Response(self.payload[2:],206,{'Content-Range':f'bytes 2-{len(self.payload)-1}/{len(self.payload)}'},interrupt_after=1,interrupt_error=http.client.IncompleteRead(b'',4))
        with self.assertRaisesRegex(dl.DownloadError,'interrupted; partial preserved'):
            dl.download_asset(self.asset,self.destination,lambda *_a,**_k:response)
        self.assertEqual(path.read_bytes(),self.payload[:5])

    def test_https_redirect_policy(self):
        handler=dl.HTTPSOnlyRedirectHandler(); request=dl.urllib.request.Request('https://huggingface.co/model')
        with self.assertRaisesRegex(dl.DownloadError,'non-HTTPS redirect'):
            handler.redirect_request(request,None,302,'Found',{},'http://storage.example/file')
        redirected=handler.redirect_request(request,None,302,'Found',{},'https://storage.example/file')
        self.assertEqual(redirected.full_url,'https://storage.example/file')

    def test_complete_hash_failure_preserved(self):
        self.destination.mkdir(); path=self.destination/self.asset['local_name']; corrupt=b'z'*len(self.payload); path.write_bytes(corrupt)
        with self.assertRaisesRegex(dl.DownloadError,'SHA-256 mismatch'):
            dl.download_asset(self.asset,self.destination,lambda *_a,**_k: self.fail('network used'))
        self.assertEqual(path.read_bytes(),corrupt)

    def test_bundle_lock_excludes_second_writer(self):
        with dl.bundle_lock(self.destination):
            with self.assertRaisesRegex(dl.DownloadError,'locked'):
                with dl.bundle_lock(self.destination): pass
        self.assertFalse((self.destination/'.download.lock').exists())

    def make_tar(self, members):
        archive_path=self.root/'fixture.tar.gz'
        with tarfile.open(archive_path,'w:gz') as archive:
            for kind,name,value,mode in members:
                info=tarfile.TarInfo(name); info.mode=mode
                if kind=='file':
                    data=value; info.size=len(data); archive.addfile(info,io.BytesIO(data))
                elif kind=='dir':
                    info.type=tarfile.DIRTYPE; archive.addfile(info)
                elif kind=='symlink':
                    info.type=tarfile.SYMTYPE; info.linkname=value; archive.addfile(info)
                elif kind=='hardlink':
                    info.type=tarfile.LNKTYPE; info.linkname=value; archive.addfile(info)
            return archive_path

    def tar_asset(self, path):
        body=path.read_bytes()
        return {'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()}

    def test_safe_relative_symlink_and_hardlink_extract(self):
        archive=self.make_tar([
            ('dir','lib',None,0o751),('dir','lib/sub',None,0o755),
            ('file','lib/libx.so.1',b'library',0o644),
            ('symlink','lib/sub/libx.so','../libx.so.1',0o777),
            ('hardlink','lib/libx-hard.so','lib/libx.so.1',0o600),
        ])
        install=self.root/'install'; dl.reconcile_runtime_archive(archive,install,self.tar_asset(archive))
        self.assertEqual((install/'lib/libx.so.1').read_bytes(),b'library')
        self.assertEqual((install/'lib').stat().st_mode & 0o777,0o751)
        self.assertEqual(os.readlink(install/'lib/sub/libx.so'),'../libx.so.1')
        self.assertTrue(os.path.samefile(install/'lib/libx.so.1',install/'lib/libx-hard.so'))
        dl.reconcile_runtime_archive(archive,install,self.tar_asset(archive)) # verified extracted members are reusable

    def test_traversal_and_escaping_link_rejected(self):
        for member in [
            [('file','../escape',b'bad',0o644)],
            [('symlink','lib/out','../../escape',0o777),('file','lib/data',b'x',0o644)],
            [('symlink','lib/first','second',0o777),('symlink','lib/second','../../escape',0o777)],
            [('symlink','lib/out','../../escape',0o777),('file','lib/out/write',b'x',0o644)],
        ]:
            archive=self.make_tar(member)
            with self.assertRaises(dl.DownloadError): dl.reconcile_runtime_archive(archive,self.root/'install',self.tar_asset(archive))
        self.assertFalse((self.root/'escape').exists())

    def test_modified_existing_extraction_path_is_not_overwritten(self):
        archive=self.make_tar([('dir','bin',None,0o755),('file','bin/llama-cli',b'official',0o755)])
        install=self.destination; (install/'bin').mkdir(parents=True); (install/'bin').chmod(0o700)
        path=install/'bin/llama-cli'; path.write_bytes(b'official'); path.chmod(0o755)
        dl.reconcile_runtime_archive(archive,install,self.tar_asset(archive))
        self.assertFalse((install/'runtime').exists()); path.write_bytes(b'user modification')
        with self.assertRaisesRegex(dl.DownloadError,'modified extraction path'):
            dl.reconcile_runtime_archive(archive,install,self.tar_asset(archive))
        self.assertEqual(path.read_bytes(),b'user modification')
        self.assertEqual((install/'bin').stat().st_mode & 0o777,0o700)

if __name__=='__main__': unittest.main()
