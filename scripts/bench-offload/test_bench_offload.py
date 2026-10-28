"""Small stdlib fixture tests; fake binaries only, never a model."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import shutil
import unittest

SCRIPT = pathlib.Path(__file__).with_name('bench_offload.py')

class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='bench-offload-', dir='/tmp')
        self.d = pathlib.Path(self.tmp.name)
        self.root = self.d / 'root'; self.root.mkdir()
        self.model = self.root / 'm.gguf'; self.model.write_bytes(b'x')
        self.prompt = self.d / 'p.txt'; self.prompt.write_text('hello')
        self.bin = self.d / 'stub'
        self.bin.write_text('''#!/usr/bin/env python3
import os, sys, time
if '--version' in sys.argv: print('stub 1'); raise SystemExit(0)
if '--help' in sys.argv: print('-m -ngl --kv-offload --no-kv-offload -ctk -ctv --fit --single-turn -p -n -c -b -ub -s --temp --simple-io --no-display-prompt --perf --mmproj'); raise SystemExit(0)
if os.getenv('STUB_MODE') == 'sleep': time.sleep(5)
if os.getenv('STUB_MODE') == 'noise': print('x' * 1000000)
if os.getenv('STUB_MODE') == 'fill':
 open(os.environ['STUB_PID'], 'w').write(str(os.getpid()))
 d=os.environ['STUB_FILL']
 os.makedirs(d, exist_ok=True)
 for i in range(1024): open(os.path.join(d, str(i)), 'wb').write(b'x' * 4096)
 time.sleep(30)
print('ok')
''')
        self.bin.chmod(0o755)
        self.ledger = self.d / 'ledger.json'
        self.ledger.write_text(json.dumps({'complete': True, 'product_root': str(self.root), 'accounting_basis':'external_only', 'overlap_exclusions':['files_within_vhdx','overlapping_host_guest_views','product_root_overlap'], 'external_allocations_bytes': {'vhdx': 1000000000}, 'root_container_key':'vhdx', 'model_projector_location':'inside_product_root', 'model_bytes': 1, 'projector_bytes': 0, 'update_peak_bytes': 1}))

    def tearDown(self): self.tmp.cleanup()
    def args(self, *extra): return [sys.executable, str(SCRIPT), '--binary', str(self.bin), '--model', str(self.model), '--prompt', str(self.prompt), '--product-root', str(self.root), *extra]

    def test_plan_does_not_launch_and_matrix(self):
        out = self.d / 'plan'; r = subprocess.run(self.args('--output', str(out)), capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads((out / 'plan.json').read_text())
        self.assertEqual(data['status'], 'plan-only'); self.assertEqual(len(data['runs']), 6)

    def test_default_output_unique_children(self):
        paths=[]
        for _ in range(2):
            r=subprocess.run(self.args(),capture_output=True,cwd=self.d)
            self.assertEqual(r.returncode,0,r.stderr); paths.append(pathlib.Path(json.loads(r.stdout)['output']))
        self.assertNotEqual(paths[0],paths[1]); self.assertTrue(all(p.parent.name=='bench-results' for p in paths)); self.assertTrue(all((p/'plan.json').is_file() for p in paths))

    def test_budget_container_overlap_counted_once(self):
        out=self.root/'budget-overlap'; r=subprocess.run(self.args('--run','--ledger',str(self.ledger),'--output',str(out),'--layers','0','--min-free-disk','1','--min-available-ram','1'),capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr); data=json.loads((out/'plan.json').read_text()); b=data['budget']
        self.assertEqual(b['root_container_allocation_bytes'],1000000000); self.assertEqual(b['root_and_container_counted_once_bytes'],1000000000)
        self.assertEqual(b['nonoverlap_external_bytes'],0); self.assertEqual(b['projected_peak_bytes'],b['root_and_container_counted_once_bytes']+b['nonoverlap_external_bytes']+b['update_peak_bytes']+b['planned_log_headroom_bytes']+b['planned_result_headroom_bytes']); self.assertFalse(b['certified_pass'])

    def test_unsupported_flag_refuses(self):
        self.bin.write_text('#!/usr/bin/env python3\nprint("-ngl -ctk -ctv --fit")\n'); self.bin.chmod(0o755)
        out = self.root / 'results'; r = subprocess.run(self.args('--run', '--ledger', str(self.ledger), '--output', str(out)), capture_output=True)
        self.assertEqual(r.returncode, 2); self.assertIn(b'unsupported', r.stderr)

    def test_input_location_claim_must_match(self):
        external=self.d/'outside.gguf'; external.write_bytes(b'x')
        out=self.root/'location-check'; r=subprocess.run(self.args('--run','--ledger',str(self.ledger),'--output',str(out),'--model',str(external)),capture_output=True)
        self.assertEqual(r.returncode,2); data=json.loads((out/'plan.json').read_text()); self.assertIn('does not match resolved',data['error'])

    def test_budget_rejection(self):
        ledger = json.loads(self.ledger.read_text()); ledger['external_allocations_bytes']['huge'] = 100_000_000_000; self.ledger.write_text(json.dumps(ledger))
        out = self.root / 'budget'; r = subprocess.run(self.args('--run', '--ledger', str(self.ledger), '--output', str(out)), capture_output=True)
        self.assertEqual(r.returncode, 2); self.assertIn(b'budget exceeded', r.stderr)

    def test_external_input_allocation_must_cover_file_bytes(self):
        external=self.d/'external-model.gguf'; external.write_bytes(b'x')
        ledger=json.loads(self.ledger.read_text()); ledger['model_projector_location']='inside_external_allocation'; ledger['model_projector_allocation_key']='model-files'; ledger['external_allocations_bytes']['model-files']=0; self.ledger.write_text(json.dumps(ledger))
        out=self.root/'stale-external'; r=subprocess.run(self.args('--run','--ledger',str(self.ledger),'--output',str(out),'--model',str(external)),capture_output=True)
        self.assertEqual(r.returncode,2); data=json.loads((out/'plan.json').read_text()); self.assertIn('smaller than their actual combined',data['error'])

    def test_low_ram_refusal_persisted(self):
        out=self.root/'lowram'; r=subprocess.run(self.args('--run','--ledger',str(self.ledger),'--output',str(out),'--min-available-ram',str(100_000_000_000_000)),capture_output=True)
        self.assertEqual(r.returncode,2); data=json.loads((out/'plan.json').read_text()); self.assertEqual(data['status'],'refused'); self.assertIn('MemAvailable',data['error'])

    def test_during_case_guard_persists_partial_and_kills_child(self):
        out=self.root/'disk-guard'; pidfile=self.d/'child.pid'; fillfile=self.d/'filldir'; env=os.environ.copy(); env.update(STUB_MODE='fill',STUB_PID=str(pidfile),STUB_FILL=str(fillfile))
        free=shutil.disk_usage(self.d).free; threshold=max(1,free-256*1024)
        r=subprocess.run(self.args('--run','--ledger',str(self.ledger),'--output',str(out),'--layers','0','--min-free-disk',str(threshold),'--min-available-ram','1','--log-limit','4096'),capture_output=True,env=env)
        self.assertEqual(r.returncode,2,r.stderr); data=json.loads((out/'plan.json').read_text()); attempt=data['runs'][0]['repeats'][0]
        self.assertEqual(attempt['status'],'failed'); self.assertIn('disk guard',attempt['error']); self.assertTrue(attempt['samples']); self.assertIsNotNone(attempt['returncode'])
        pid=int(pidfile.read_text())
        with self.assertRaises(ProcessLookupError): os.kill(pid,0)
        shutil.rmtree(fillfile,ignore_errors=True)

    def test_timeout_and_log_limit_cleanup(self):
        for mode, expected in [('sleep', b'timed out'), ('noise', b'log limit')]:
            out = self.root / mode; env = os.environ.copy(); env['STUB_MODE'] = mode
            r = subprocess.run(self.args('--run', '--ledger', str(self.ledger), '--output', str(out), '--layers', '0', '--timeout', '1', '--log-limit', '4096'), capture_output=True, env=env)
            self.assertEqual(r.returncode, 2, r.stderr)
            data=json.loads((out/'plan.json').read_text()); attempt=data['runs'][0]['repeats'][0]; self.assertEqual(data['status'],'refused'); self.assertIn('error',data); self.assertTrue(any(out.glob('*.log'))); self.assertTrue(attempt['timed_out'] if expected == b'timed out' else attempt['log_limit_exceeded'])

if __name__ == '__main__': unittest.main()
