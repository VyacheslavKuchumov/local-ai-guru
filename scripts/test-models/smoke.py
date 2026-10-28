#!/usr/bin/env python3
"""Bounded text-only smoke runs for the two pinned local models."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = PROJECT_ROOT / 'scripts' / 'download' / 'manifest.json'
DATA_ROOT = Path.home() / 'local-ai-guru-data'
PROMPT = "Write a Python function is_prime(n) and exactly three assert statements that test it. Return only code, with no explanation."
TEMPLATE_KWARGS = '{"enable_thinking":false}'
CONTEXT = 4096
BATCH = 512
UBATCH = 128
GPU_LAYERS = 99
PREDICT = 512
SEED = 42
TEMPERATURE = 0
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 3600
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_PROBE_BYTES = 2 * 1024 * 1024
MAX_SEARCH_DEPTH = 5
MAX_SEARCH_DIRS = 512
MIN_RAM_BYTES = 1024 * 1024 * 1024
MIN_FREE_DISK_BYTES = 2 * 1024 * 1024 * 1024
REQUIRED_FLAGS = (
    '-m', '-p', '-n', '-c', '-b', '-ub', '-ngl', '--fit', '--offline',
    '--single-turn', '--simple-io', '--no-display-prompt', '--perf', '-s',
    '--temp', '--jinja', '--chat-template-kwargs',
)
ANSI_RE = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')
SUMMARY_RE = re.compile(r'\[\s*Prompt:\s*([\d.]+)\s*t/s\s*\|\s*Generation:\s*([\d.]+)\s*t/s\s*\]', re.I)
EVAL_RE = re.compile(r'(prompt eval time|eval time)\s*=.*?\(([\d.]+)\s*tokens per second\)', re.I)

class SmokeRefusal(Exception):
    pass

def load_manifest(path=MANIFEST_PATH):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    bundles = data.get('bundles', {})
    if not {'runtime', 'qwen', 'gemma'}.issubset(bundles):
        raise SmokeRefusal('pinned download manifest is missing runtime/qwen/gemma entries')
    return data

def selected_assets(model_name, manifest):
    model_bundle = manifest['bundles'][model_name]
    runtime_bundle = manifest['bundles']['runtime']
    model_asset = next((a for a in model_bundle.get('assets', []) if a.get('filename','').endswith('.gguf') and 'mmproj' not in a.get('filename','').lower()), None)
    if model_asset is None:
        raise SmokeRefusal(f'manifest has no text-model GGUF for {model_name}')
    model_path = DATA_ROOT / model_bundle['destination'] / model_asset['local_name']
    runtime_root = DATA_ROOT / runtime_bundle['destination']
    return runtime_root, model_path, model_asset

def dry_run_info(model_name, manifest):
    runtime_root, model_path, model_asset = selected_assets(model_name, manifest)
    return {
        'model': model_name,
        'model_path': str(model_path),
        'model_expected_bytes': model_asset['bytes'],
        'model_source': model_asset['url'],
        'runtime_root': str(runtime_root),
        'output_root': str(DATA_ROOT / 'bench-results'),
        'prompt': PROMPT,
        'settings': {'context': CONTEXT, 'batch': BATCH, 'ubatch': UBATCH,
                     'gpu_layers': GPU_LAYERS, 'predict': PREDICT,
                     'temperature': TEMPERATURE, 'seed': SEED,
                     'template_kwargs': TEMPLATE_KWARGS, 'text_only': True},
    }

def find_runtime(runtime_root):
    root = runtime_root.resolve(strict=True)
    if not root.is_dir():
        raise SmokeRefusal(f'llama.cpp runtime directory is not a directory: {root}')
    binary_candidates = []
    library_dirs = set()
    visited = 0
    exceeded = False
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)
        dirs[:] = sorted(d for d in dirs if not (current_path / d).is_symlink()) if depth < MAX_SEARCH_DEPTH else []
        visited += 1
        if visited > MAX_SEARCH_DIRS:
            exceeded = True
            break
        for name in files:
            item = current_path / name
            if name == 'llama-cli' and item.is_file() and os.access(item, os.X_OK):
                resolved = item.resolve(strict=True)
                if resolved.is_relative_to(root):
                    binary_candidates.append(item)
            if name.startswith('lib') and '.so' in name and item.is_file():
                library_dirs.add(str(current_path.resolve()))
    if exceeded:
        raise SmokeRefusal(f'runtime search exceeded {MAX_SEARCH_DIRS} directories; refusing ambiguous partial search')
    if not binary_candidates:
        raise SmokeRefusal(f'could not find executable llama-cli within {MAX_SEARCH_DEPTH} directory levels under {root}')
    if len(binary_candidates) != 1:
        names = ', '.join(str(p) for p in binary_candidates[:8])
        raise SmokeRefusal(f'ambiguous runtime: found {len(binary_candidates)} executable llama-cli files: {names}')
    binary = binary_candidates[0].resolve(strict=True)
    return root, binary, sorted(library_dirs)

def clean_environment(library_dirs):
    env = {key: value for key, value in os.environ.items() if not key.startswith('LLAMA_ARG_')}
    env['HF_HUB_OFFLINE'] = '1'
    env['HF_HUB_DISABLE_TELEMETRY'] = '1'
    existing = env.get('LD_LIBRARY_PATH', '')
    paths = list(library_dirs)
    if existing:
        paths.extend(existing.split(os.pathsep))
    env['LD_LIBRARY_PATH'] = os.pathsep.join(dict.fromkeys(p for p in paths if p))
    return env

def build_command(binary, model):
    return [
        str(binary), '-m', str(model), '-p', PROMPT, '-n', str(PREDICT),
        '-c', str(CONTEXT), '-b', str(BATCH), '-ub', str(UBATCH),
        '-ngl', str(GPU_LAYERS), '--fit', 'off', '--offline',
        '--single-turn', '--simple-io', '--no-display-prompt', '--perf',
        '-s', str(SEED), '--temp', str(TEMPERATURE), '--jinja',
        '--chat-template-kwargs', TEMPLATE_KWARGS,
    ]

def _run_probe(command, env, cwd=None):
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, cwd=str(cwd) if cwd else None,
                            start_new_session=True, bufsize=0)
    selector = selectors.DefaultSelector()
    chunks = {'stdout': bytearray(), 'stderr': bytearray()}
    streams = {'stdout': (proc.stdout, 'stdout'), 'stderr': (proc.stderr, 'stderr')}
    for name, (pipe, _) in streams.items():
        selector.register(pipe, selectors.EVENT_READ, name)
    started = time.monotonic()
    reason = None
    total = 0
    try:
        while selector.get_map() or proc.poll() is None:
            if time.monotonic() - started >= 15:
                reason = 'probe timeout'
                break
            events = selector.select(timeout=0.2) if selector.get_map() else []
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                remaining = MAX_PROBE_BYTES - total
                if remaining <= 0:
                    reason = f'probe output exceeded {MAX_PROBE_BYTES} bytes'
                    break
                kept = chunk[:remaining]
                chunks[key.data].extend(kept)
                total += len(kept)
                if len(kept) != len(chunk):
                    reason = f'probe output exceeded {MAX_PROBE_BYTES} bytes'
                    break
            if reason:
                break
            if not selector.get_map() and proc.poll() is None:
                time.sleep(0.05)
        if reason:
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        proc.wait()
    except BaseException:
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        proc.wait()
        raise
    finally:
        selector.close()
        for pipe, _ in streams.values():
            if pipe is not None:
                try: pipe.close()
                except OSError: pass
        # The probe owns a new process group; clear descendants even if the probe parent exited.
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    return {'command': command, 'returncode': proc.returncode,
            'stdout_bytes': len(chunks['stdout']), 'stderr_bytes': len(chunks['stderr']),
            'stdout': chunks['stdout'].decode('utf-8', errors='replace'),
            'stderr': chunks['stderr'].decode('utf-8', errors='replace'),
            'reason': reason, 'elapsed_seconds': round(time.monotonic()-started, 3)}

def _probe_evidence(probe):
    return {key: probe[key] for key in ('command','returncode','stdout_bytes','stderr_bytes','reason','elapsed_seconds')} | {
        'stdout_excerpt': probe['stdout'][:4096], 'stderr_excerpt': probe['stderr'][:4096]}

def probe_runtime(binary, env, cwd=None):
    version = _run_probe([str(binary), '--version'], env, cwd)
    help_result = _run_probe([str(binary), '--help'], env, cwd)
    help_text = help_result['stdout'] + '\n' + help_result['stderr']
    missing = [flag for flag in REQUIRED_FLAGS if not re.search(r'(?<![\w-])' + re.escape(flag) + r'(?![\w-])', help_text)]
    return {'version': _probe_evidence(version), 'help': _probe_evidence(help_result),
            'version_status': version['returncode'], 'help_status': help_result['returncode'],
            'missing_flags': missing,
            'verified_flags': [flag for flag in REQUIRED_FLAGS if flag not in missing]}

def _mem_available():
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        return None
    return None

def _process_rss(pid):
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        return None
    return None

def guest_guard(output_dir, min_ram=MIN_RAM_BYTES, min_disk=MIN_FREE_DISK_BYTES):
    available = _mem_available()
    free = shutil.disk_usage(output_dir).free
    if available is None or available < min_ram:
        raise SmokeRefusal(f'guest MemAvailable below minimum or unavailable ({available})')
    if free < min_disk:
        raise SmokeRefusal(f'guest free disk below minimum ({free})')
    return available, free

def _terminal_write(stream, data):
    if not data:
        return
    target = getattr(stream, 'buffer', None)
    if target is not None:
        target.write(data)
        target.flush()
    else:
        stream.write(data.decode('utf-8', errors='replace'))
        stream.flush()

def execute_command(command, env, run_dir, cwd=None, timeout=DEFAULT_TIMEOUT,
                    log_limit=MAX_LOG_BYTES, guard=guest_guard, sample_interval=1.0):
    record = {'command': command, 'status': 'starting', 'returncode': None,
              'elapsed_seconds': None, 'reason': None, 'samples': [],
              'stdout_file': 'stdout.raw', 'stderr_file': 'stderr.raw',
              'stdout_first_byte_ttft': None, 'reported_performance': []}
    stdout_path = run_dir / 'stdout.raw'
    stderr_path = run_dir / 'stderr.raw'
    proc = None
    selector = selectors.DefaultSelector()
    started = time.monotonic()
    bytes_saved = 0
    reason = None
    try:
        ram, disk = guard(run_dir)
        record['prelaunch_guest'] = {'mem_available_bytes': ram, 'free_disk_bytes': disk}
        with stdout_path.open('xb') as stdout_file, stderr_path.open('xb') as stderr_file:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=env, cwd=str(cwd or run_dir), start_new_session=True, bufsize=0)
            selector.register(proc.stdout, selectors.EVENT_READ, (stdout_file, sys.stdout))
            selector.register(proc.stderr, selectors.EVENT_READ, (stderr_file, sys.stderr))
            next_sample = started
            while selector.get_map() or proc.poll() is None:
                now = time.monotonic()
                if now - started >= timeout:
                    reason = 'timeout'
                    break
                try:
                    ram, disk = guard(run_dir)
                except SmokeRefusal as exc:
                    reason = 'guest_guard: ' + str(exc)
                    break
                if now >= next_sample:
                    record['samples'].append({'elapsed_seconds': round(now-started, 2),
                                              'mem_available_bytes': ram,
                                              'free_disk_bytes': disk,
                                              'process_rss_bytes': _process_rss(proc.pid) if proc.poll() is None else None})
                    next_sample = now + sample_interval
                events = selector.select(timeout=0.2) if selector.get_map() else []
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = max(0, log_limit - bytes_saved)
                    kept = chunk[:remaining]
                    if kept:
                        key.data[0].write(kept)
                        key.data[0].flush()
                        _terminal_write(key.data[1], kept)
                        bytes_saved += len(kept)
                    if len(kept) != len(chunk):
                        reason = f'log_limit_exceeded: {log_limit} bytes'
                        break
                if reason:
                    break
                if not selector.get_map() and proc.poll() is None:
                    time.sleep(0.05)
            if reason:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            proc.wait()
            record['returncode'] = proc.returncode
            record['elapsed_seconds'] = round(time.monotonic() - started, 3)
            record['reason'] = reason or (None if proc.returncode == 0 else f'llama-cli exited with status {proc.returncode}')
            record['status'] = 'completed' if reason is None and proc.returncode == 0 else ('timed_out' if reason == 'timeout' else 'refused' if reason and reason.startswith('guest_guard:') else 'output_limited' if reason and reason.startswith('log_limit') else 'failed')
    except KeyboardInterrupt:
        reason = 'cancelled by user (KeyboardInterrupt)'
        record.update({'status': 'cancelled', 'reason': reason})
    except SmokeRefusal as exc:
        record.update({'status': 'refused', 'reason': str(exc)})
    except OSError as exc:
        record.update({'status': 'refused', 'reason': f'could not start or capture llama-cli: {exc}'})
    except Exception as exc:
        record.update({'status': 'refused', 'reason': f'capture failed: {type(exc).__name__}: {exc}'})
    finally:
        if proc is not None:
            if proc.poll() is None:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                proc.wait()
            # Popen used start_new_session, so its process group is ours even if its parent exited.
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        selector.close()
        if proc is not None:
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    try: pipe.close()
                    except OSError: pass
        record['elapsed_seconds'] = round(time.monotonic() - started, 3)
        # Preserve partial return code/log evidence after timeout, guard, or cancellation.
        if proc is not None:
            record['returncode'] = proc.returncode
        record['log_bytes'] = bytes_saved
        record['reason'] = reason or record.get('reason')
        record['reported_performance'] = parse_performance(stdout_path, stderr_path)
    return record

def parse_performance(*paths):
    lines = []
    for path in paths:
        try:
            text = path.read_bytes().decode('utf-8', errors='replace')
        except OSError:
            continue
        text = ANSI_RE.sub('', text)
        for line in text.splitlines():
            match = SUMMARY_RE.search(line)
            if match and len(lines) < 100:
                lines.append({'format': 'prompt-generation-summary',
                              'prompt_tokens_per_second': float(match.group(1)),
                              'generation_tokens_per_second': float(match.group(2)),
                              'raw_line': line.strip()[:512]})
                continue
            match = EVAL_RE.search(line)
            if match and len(lines) < 100:
                lines.append({'format': match.group(1).lower(),
                              'tokens_per_second': float(match.group(2)),
                              'raw_line': line.strip()[:512]})
    return lines

def make_run_dir(data_root, model_name, now=None):
    base = data_root / 'bench-results'
    base.mkdir(parents=True, exist_ok=True)
    stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime('%Y%m%dT%H%M%S.%fZ')
    first = base / f'{model_name}-smoke-{stamp}'
    candidate = first
    suffix = 1
    while True:
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            candidate = base / f'{model_name}-smoke-{stamp}-{suffix:02d}'
            suffix += 1

def persist(run_dir, record):
    target = run_dir / 'result.json'
    temp = run_dir / 'result.json.tmp'
    encoded = json.dumps(record, ensure_ascii=False, indent=2)
    if len(encoded.encode('utf-8')) > 1024 * 1024:
        record['metadata_truncated'] = True
        for key in ('samples',):
            if key in record:
                record[key] = record[key][-100:]
        execution = record.get('execution', {})
        execution['samples'] = execution.get('samples', [])[-200:]
        perf = execution.get('reported_performance', [])[-100:]
        for item in perf:
            if 'raw_line' in item:
                item['raw_line'] = item['raw_line'][:256]
        execution['reported_performance'] = perf
        runtime = record.get('runtime', {})
        for key in ('help', 'version'):
            probe = runtime.get(key)
            if probe:
                probe['stdout_excerpt'] = probe.get('stdout_excerpt','')[:2048]
                probe['stderr_excerpt'] = probe.get('stderr_excerpt','')[:2048]
        encoded = json.dumps(record, ensure_ascii=False, indent=2)
    if len(encoded.encode('utf-8')) > 1024 * 1024:
        # The raw bounded logs remain authoritative; retain compact status/link metadata.
        compact = {key: record.get(key) for key in ('model','status','run_dir','reason','returncode','elapsed_seconds','command','started_utc') if key in record}
        compact.update({'metadata_truncated': True, 'logs': ['stdout.raw','stderr.raw'],
                        'note': 'JSON details exceeded 1 MiB; inspect bounded raw logs for captured evidence.'})
        encoded = json.dumps(compact, ensure_ascii=False, indent=2)
    if len(encoded.encode('utf-8')) > 1024 * 1024:
        raise SmokeRefusal('compact result metadata exceeds 1 MiB bound')
    temp.write_text(encoded, encoding='utf-8')
    temp.replace(target)

def main(model_name, argv=None):
    parser = argparse.ArgumentParser(description=f'Run one offline, text-only {model_name} smoke test.')
    parser.add_argument('--dry-run', action='store_true', help='show selected local inputs and fixed run settings only')
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest()
        info = dry_run_info(model_name, manifest)
    except (OSError, ValueError, KeyError, SmokeRefusal) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    if args.dry_run:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    run_dir = None
    record = {'model': model_name, 'status': 'preflight', 'selected': info,
              'started_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
              'limits': {'timeout_seconds': DEFAULT_TIMEOUT, 'max_log_bytes': MAX_LOG_BYTES,
                         'guest_min_ram_bytes': MIN_RAM_BYTES, 'guest_min_free_disk_bytes': MIN_FREE_DISK_BYTES},
              'notes': ['text-only; projector not loaded or required',
                        'model output is untrusted text and is never evaluated or executed',
                        'stdout-first-byte/TTFT unavailable',
                        'CLI-reported performance only; no quality grade',
                        'no physical Windows capacity, complete product-budget, or OS/network-isolation certification']}
    try:
        run_dir = make_run_dir(DATA_ROOT, model_name)
        record['run_dir'] = str(run_dir)
        persist(run_dir, record)
        runtime_root, model_path, model_asset = selected_assets(model_name, manifest)
        if not runtime_root.is_dir():
            raise SmokeRefusal(f'local llama.cpp runtime directory is missing: {runtime_root}')
        if not model_path.is_file():
            raise SmokeRefusal(f'pinned model file is missing: {model_path}; prepare it with scripts/download/{model_name}.py')
        if model_path.stat().st_size != model_asset['bytes']:
            raise SmokeRefusal(f'model file size mismatch: expected {model_asset["bytes"]}, found {model_path.stat().st_size}: {model_path}')
        runtime_root, binary, library_dirs = find_runtime(runtime_root)
        env = clean_environment(library_dirs)
        record['binary'] = str(binary)
        record['model_file_bytes'] = model_path.stat().st_size
        record['runtime_library_dirs'] = library_dirs
        record['removed_llama_arg_environment_count'] = sum(1 for k in os.environ if k.startswith('LLAMA_ARG_'))
        record['environment_controls'] = {'HF_HUB_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1'}
        persist(run_dir, record)
        guest_guard(run_dir)
        runtime_check = probe_runtime(binary, env, runtime_root)
        record['runtime'] = runtime_check
        persist(run_dir, record)
        if runtime_check['version_status'] != 0 or runtime_check['help_status'] != 0 or runtime_check['version'].get('reason') or runtime_check['help'].get('reason'):
            raise SmokeRefusal(f'local llama-cli --version/--help failed (version={runtime_check["version_status"]}, help={runtime_check["help_status"]})')
        if runtime_check['missing_flags']:
            raise SmokeRefusal('local llama-cli --help lacks required flags: ' + ', '.join(runtime_check['missing_flags']) + '; no fallback is attempted')
        command = build_command(binary, model_path)
        record['command'] = command
        record['status'] = 'running'
        persist(run_dir, record)
        execution = execute_command(command, env, run_dir, cwd=runtime_root)
        record['execution'] = execution
        record['status'] = execution['status']
        record['reason'] = execution['reason']
        record['elapsed_seconds'] = execution['elapsed_seconds']
        record['returncode'] = execution['returncode']
        persist(run_dir, record)
        print(f"\nSmoke status: {record['status']} | results: {run_dir}")
        return 0 if record['status'] == 'completed' else 130 if record['status'] == 'cancelled' else 2
    except KeyboardInterrupt:
        record.update({'status': 'cancelled', 'reason': 'cancelled before/during runtime probe'})
        if run_dir:
            try: persist(run_dir, record)
            except OSError: pass
        print('INTERRUPTED; partial evidence saved when available', file=sys.stderr)
        return 130
    except (SmokeRefusal, OSError, ValueError, subprocess.TimeoutExpired, KeyError) as exc:
        record.update({'status': 'refused', 'reason': str(exc)})
        if run_dir:
            try: persist(run_dir, record)
            except OSError: pass
        print(f'REFUSED: {exc}' + (f' | evidence: {run_dir}' if run_dir else ''), file=sys.stderr)
        return 2
