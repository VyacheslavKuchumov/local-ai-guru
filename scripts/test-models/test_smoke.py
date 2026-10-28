"""Offline-only tests for public model smoke wrappers and bounded process capture."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import smoke

FLAGS = ' '.join(smoke.REQUIRED_FLAGS)
STUB = f'''#!/usr/bin/env python3
import os, sys, time
mode = os.environ.get('SMOKE_STUB_MODE', '')
if '--version' in sys.argv:
    print('llama.cpp stub b11968')
    raise SystemExit(0)
if '--help' in sys.argv:
    print(os.environ.get('SMOKE_STUB_HELP', {FLAGS!r}))
    raise SystemExit(0)
pid_file = os.environ.get('SMOKE_STUB_PID_FILE')
if pid_file:
    open(pid_file, 'w').write(str(os.getpid()))
call_counter = os.environ.get('SMOKE_CALL_COUNTER')
if call_counter:
    with open(call_counter, 'a') as f: f.write('call\\n')
if mode == 'sleep':
    print('partial output before sleep', flush=True)
    time.sleep(30)
elif mode == 'close':
    print('partial output before closed streams', flush=True)
    os.close(1); os.close(2)
    time.sleep(30)
elif mode == 'huge':
    print('x' * 200000, flush=True)
else:
    print('\\x1b[31mFAKE MODEL OUTPUT\\x1b[0m', flush=True)
    print('def is_prime(n): return n > 1', flush=True)
    print('assert is_prime(2)\\nassert is_prime(3)\\nassert not is_prime(4)', flush=True)
    print('[ Prompt: 155.6 t/s | Generation: 53.6 t/s ]', flush=True)
    print('stub stderr', file=sys.stderr, flush=True)
    if mode == 'reject':
        print('unsupported template kwargs', file=sys.stderr, flush=True)
        raise SystemExit(2)
'''

class CaptureBytes:
    def __init__(self): self.data=bytearray(); self.buffer=self
    def write(self, data): self.data.extend(data)
    def flush(self): pass

class ModelSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='model-smoke-', dir='/tmp')
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        (self.project / 'scripts' / 'test-models').mkdir(parents=True)
        (self.project / 'scripts' / 'download').mkdir(parents=True)
        for name in ('smoke.py', 'qwen.py', 'gemma.py'):
            shutil.copy2(HERE / name, self.project / 'scripts' / 'test-models' / name)
        manifest = json.loads((HERE.parent / 'download' / 'manifest.json').read_text())
        for model_name in ('qwen', 'gemma'):
            asset = next(a for a in manifest['bundles'][model_name]['assets'] if 'mmproj' not in a['filename'].lower())
            asset['bytes'] = 23
        (self.project / 'scripts' / 'download' / 'manifest.json').write_text(json.dumps(manifest))
        self.manifest = manifest
        self.home = self.root / 'home'
        self.data_root = self.home / 'local-ai-guru-data'
        runtime_rel = manifest['bundles']['runtime']['destination']
        self.runtime = self.data_root / runtime_rel
        (self.runtime / 'bin').mkdir(parents=True)
        self.lib = self.runtime / 'lib'
        self.lib.mkdir()
        (self.lib / 'libstub.so.1').write_bytes(b'synthetic shared-library marker')
        self.binary = self.runtime / 'bin' / 'llama-cli'
        self.binary.write_text(STUB)
        self.binary.chmod(0o755)
        self.model_paths = {}
        for model_name in ('qwen', 'gemma'):
            bundle = manifest['bundles'][model_name]
            asset = next(a for a in bundle['assets'] if 'mmproj' not in a['filename'].lower())
            path = self.data_root / bundle['destination'] / asset['local_name']
            path.parent.mkdir(parents=True)
            path.write_bytes(b'x' * 23)
            self.model_paths[model_name] = path

    def tearDown(self):
        self.temp.cleanup()

    def env(self, **extra):
        env = os.environ.copy()
        env.update({'HOME': str(self.home), 'PYTHONDONTWRITEBYTECODE': '1'})
        env.update(extra)
        return env

    def wrapper(self, model):
        return self.project / 'scripts' / 'test-models' / f'{model}.py'

    def run_wrapper(self, model, *args, **env):
        return subprocess.run([sys.executable, str(self.wrapper(model)), *args],
                              capture_output=True, env=self.env(**env), timeout=20)

    def runs(self):
        return sorted((self.data_root / 'bench-results').glob('*-smoke-*'))

    def test_help_and_dry_run_have_no_execution_writes_or_bytecode(self):
        fresh = self.root / 'fresh'; (fresh / 'scripts' / 'test-models').mkdir(parents=True)
        (fresh / 'scripts' / 'download').mkdir(parents=True)
        for name in ('smoke.py','qwen.py','gemma.py'):
            shutil.copy2(HERE/name, fresh/'scripts'/'test-models'/name)
        shutil.copy2(HERE.parent/'download'/'manifest.json', fresh/'scripts'/'download'/'manifest.json')
        home = self.root/'fresh-home'; env=self.env(HOME=str(home))
        for model in ('qwen','gemma'):
            script=fresh/'scripts'/'test-models'/f'{model}.py'
            help_run=subprocess.run([sys.executable,str(script),'--help'],capture_output=True,env=env,timeout=10)
            dry_run=subprocess.run([sys.executable,str(script),'--dry-run'],capture_output=True,env=env,timeout=10)
            self.assertEqual(help_run.returncode,0,help_run.stderr)
            self.assertEqual(dry_run.returncode,0,dry_run.stderr)
            self.assertEqual(json.loads(dry_run.stdout)['model'],model)
        self.assertFalse(home.exists())
        self.assertFalse((fresh/'scripts'/'test-models'/'__pycache__').exists())

    def test_fixed_prompt_and_both_command_vectors(self):
        qwen = smoke.build_command(self.binary,self.model_paths['qwen'])
        gemma = smoke.build_command(self.binary,self.model_paths['gemma'])
        self.assertEqual(qwen[ qwen.index('-p')+1 ], smoke.PROMPT)
        self.assertEqual(gemma[ gemma.index('-p')+1 ], smoke.PROMPT)
        self.assertEqual(smoke.PROMPT, 'Write a Python function is_prime(n) and exactly three assert statements that test it. Return only code, with no explanation.')
        for command in (qwen,gemma):
            self.assertEqual(command[command.index('-n')+1], '512')
            self.assertEqual(command[command.index('-ngl')+1], '99')
            self.assertEqual(command[command.index('--fit')+1], 'off')
            self.assertEqual(command[command.index('--chat-template-kwargs')+1], '{"enable_thinking":false}')
            self.assertIn('--offline',command); self.assertIn('--jinja',command)
            self.assertNotIn('--mmproj',command); self.assertNotIn('--no-mmproj',command)
            self.assertNotIn('mmproj-F16.gguf',' '.join(command))

    def test_environment_strips_llama_arg_overrides(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {'LLAMA_ARG_MODEL_URL':'https://bad.invalid','LLAMA_ARG_N_PREDICT':'1','LD_LIBRARY_PATH':'/existing'}, clear=False))
            env=smoke.clean_environment([str(self.lib)])
        self.assertNotIn('LLAMA_ARG_MODEL_URL',env); self.assertNotIn('LLAMA_ARG_N_PREDICT',env)
        self.assertEqual(env['HF_HUB_OFFLINE'],'1'); self.assertIn(str(self.lib),env['LD_LIBRARY_PATH']); self.assertIn('/existing',env['LD_LIBRARY_PATH'])

    def test_missing_model_refusal_is_persisted_without_binary_probe(self):
        self.model_paths['qwen'].unlink()
        marker=self.root/'binary-ran'
        self.binary.write_text(STUB.replace("if '--version' in sys.argv:", "open(os.environ['MARKER'],'w').write('ran')\nif '--version' in sys.argv:"))
        self.binary.chmod(0o755)
        result=self.run_wrapper('qwen',MARKER=str(marker))
        self.assertEqual(result.returncode,2)
        run=self.runs()[0]; data=json.loads((run/'result.json').read_text())
        self.assertEqual(data['status'],'refused'); self.assertIn('model file is missing',data['reason'])
        self.assertFalse(marker.exists())

    def test_public_wrappers_stream_and_persist_text_only_diagnostics(self):
        for model in ('qwen','gemma'):
            result=self.run_wrapper(model,LLAMA_ARG_N_PREDICT='1')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn(b'FAKE MODEL OUTPUT',result.stdout)
            run=next(p for p in self.runs() if p.name.startswith(model+'-'))
            data=json.loads((run/'result.json').read_text())
            self.assertEqual(data['status'],'completed')
            self.assertEqual(data['runtime']['version']['stdout_excerpt'].strip(),'llama.cpp stub b11968')
            command=data['command']
            self.assertEqual(command[command.index('-n')+1],'512')
            self.assertEqual(command[command.index('-p')+1],smoke.PROMPT)
            self.assertNotIn('--mmproj',command); self.assertNotIn('--no-mmproj',command)
            self.assertGreaterEqual(data['removed_llama_arg_environment_count'],1)
            execution=data['execution']
            raw=(run/'stdout.raw').read_bytes()
            self.assertIn(b'\x1b[31m',raw)
            self.assertIn(b'stub stderr',(run/'stderr.raw').read_bytes())
            self.assertIsNone(execution['stdout_first_byte_ttft'])
            self.assertEqual(execution['reported_performance'][0]['prompt_tokens_per_second'],155.6)
            self.assertEqual(execution['reported_performance'][0]['generation_tokens_per_second'],53.6)
            self.assertFalse(any('quality' in key.lower() and value for key,value in data.items()))

    def test_model_template_rejection_is_saved_without_retry(self):
        counter=self.root/'generation.calls'
        result=self.run_wrapper('qwen',SMOKE_STUB_MODE='reject',SMOKE_CALL_COUNTER=str(counter))
        self.assertEqual(result.returncode,2)
        data=json.loads((self.runs()[0]/'result.json').read_text())
        self.assertEqual(data['status'],'failed'); self.assertEqual(data['returncode'],2)
        self.assertIn('unsupported template kwargs',(self.runs()[0]/'stderr.raw').read_text())
        self.assertEqual(counter.read_text().splitlines(),['call'])

    def test_runtime_flag_refusal_persists_help_evidence_without_fallback(self):
        env=self.env(SMOKE_STUB_HELP=FLAGS.replace('--jinja',''))
        result=subprocess.run([sys.executable,str(self.wrapper('qwen'))],capture_output=True,env=env,timeout=20)
        self.assertEqual(result.returncode,2)
        run=self.runs()[0]; data=json.loads((run/'result.json').read_text())
        self.assertEqual(data['status'],'refused'); self.assertIn('--jinja',data['runtime']['missing_flags'])
        self.assertEqual(data['runtime']['version_status'],0); self.assertEqual(data['runtime']['help_status'],0)
        self.assertNotIn('command',data)

    def test_long_help_with_required_flags_near_start_is_fully_validated(self):
        long_help=FLAGS+'\n'+('description filler '*1500)
        result=self.run_wrapper('qwen',SMOKE_STUB_HELP=long_help)
        self.assertEqual(result.returncode,0,result.stderr)
        data=json.loads((self.runs()[0]/'result.json').read_text())
        self.assertGreater(data['runtime']['help']['stdout_bytes'],8192)
        self.assertFalse(data['runtime']['missing_flags'])

    def test_runtime_discovery_fails_on_ambiguity_and_is_bounded(self):
        duplicate=self.runtime/'other'/'bin'/'llama-cli'; duplicate.parent.mkdir(parents=True); shutil.copy2(self.binary,duplicate); duplicate.chmod(0o755)
        with self.assertRaisesRegex(smoke.SmokeRefusal,'ambiguous runtime'):
            smoke.find_runtime(self.runtime)

    def _sleep_command(self, run_dir, mode):
        pid_file=self.root/f'{mode}.pid'
        env=smoke.clean_environment([str(self.lib)]); env.update({'SMOKE_STUB_MODE':'close' if mode=='eof-timeout' else 'sleep','SMOKE_STUB_PID_FILE':str(pid_file)})
        return [str(self.binary),'--generate'], env, pid_file

    def _assert_pid_gone(self, path):
        pid=int(path.read_text())
        with self.assertRaises(ProcessLookupError): os.kill(pid,0)

    def test_timeout_guard_and_log_limit_kill_process_group_and_keep_partial_logs(self):
        for mode in ('timeout','guard','eof-timeout'):
            run_dir=self.root/f'run-{mode}'; run_dir.mkdir()
            command,env,pid_file=self._sleep_command(run_dir,mode)
            if mode in ('timeout','eof-timeout'):
                with mock.patch.object(smoke.sys,'stdout',CaptureBytes()), mock.patch.object(smoke.sys,'stderr',CaptureBytes()):
                    record=smoke.execute_command(command,env,run_dir,cwd=self.runtime,timeout=0.3,guard=lambda _: (4_000_000_000,10_000_000_000))
                self.assertEqual(record['status'],'timed_out')
                if mode == 'eof-timeout': self.assertIn(b'partial output before closed streams',(run_dir/'stdout.raw').read_bytes())
            else:
                calls=[0]
                def guard(_):
                    calls[0]+=1
                    if calls[0]>=3: raise smoke.SmokeRefusal('synthetic RAM guard trip')
                    return 4_000_000_000,10_000_000_000
                with mock.patch.object(smoke.sys,'stdout',CaptureBytes()), mock.patch.object(smoke.sys,'stderr',CaptureBytes()):
                    record=smoke.execute_command(command,env,run_dir,cwd=self.runtime,timeout=10,guard=guard)
                self.assertEqual(record['status'],'refused'); self.assertIn('guest_guard',record['reason'])
            self.assertTrue((run_dir/'stdout.raw').read_bytes())
            self._assert_pid_gone(pid_file)
        run_dir=self.root/'run-limit'; run_dir.mkdir()
        env=smoke.clean_environment([str(self.lib)]); env['SMOKE_STUB_MODE']='huge'
        with mock.patch.object(smoke.sys,'stdout',CaptureBytes()), mock.patch.object(smoke.sys,'stderr',CaptureBytes()):
            record=smoke.execute_command([str(self.binary),'--generate'],env,run_dir,cwd=self.runtime,
                                         timeout=5,log_limit=1024,guard=lambda _: (4_000_000_000,10_000_000_000))
        self.assertEqual(record['status'],'output_limited')
        self.assertLessEqual((run_dir/'stdout.raw').stat().st_size+(run_dir/'stderr.raw').stat().st_size,1024)

    def test_cancel_wrapper_saves_partial_evidence_and_kills_child(self):
        pid_file=self.root/'cancel-child.pid'
        env=self.env(SMOKE_STUB_MODE='sleep',SMOKE_STUB_PID_FILE=str(pid_file))
        proc=subprocess.Popen([sys.executable,str(self.wrapper('qwen'))],stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,start_new_session=True)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline and not pid_file.exists() and proc.poll() is None:
            time.sleep(0.05)
        self.assertTrue(pid_file.exists(),'stub never reached model invocation')
        proc.send_signal(signal.SIGINT)
        stdout,stderr=proc.communicate(timeout=10)
        self.assertEqual(proc.returncode,130,stderr.decode(errors='replace'))
        run=self.runs()[0]; data=json.loads((run/'result.json').read_text())
        self.assertEqual(data['status'],'cancelled')
        self.assertTrue((run/'stdout.raw').read_bytes())
        self._assert_pid_gone(pid_file)

    def test_run_directories_are_unique(self):
        now=__import__('datetime').datetime(2026,10,28,tzinfo=__import__('datetime').timezone.utc)
        first=smoke.make_run_dir(self.root/'out','qwen',now)
        second=smoke.make_run_dir(self.root/'out','qwen',now)
        self.assertNotEqual(first,second); self.assertIn('qwen-smoke-',first.name)

    def test_result_json_stays_bounded_with_large_performance_evidence(self):
        run_dir=self.root/'bounded-result'; run_dir.mkdir()
        record={'model':'qwen','status':'completed','run_dir':str(run_dir),
                'execution':{'samples':[{'elapsed_seconds':i,'padding':'x'*1000} for i in range(1000)],
                             'reported_performance':[{'raw_line':'y'*10000} for _ in range(1000)]}}
        smoke.persist(run_dir,record)
        result=run_dir/'result.json'
        self.assertLessEqual(result.stat().st_size,1024*1024)
        self.assertTrue(json.loads(result.read_text())['metadata_truncated'])

if __name__ == '__main__': unittest.main()
