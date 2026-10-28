#!/usr/bin/env python3
"""Bounded, explicit llama.cpp offload benchmark runner for WSL/Linux guests."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import subprocess
import sys
import time

BUDGET = 100_000_000_000
MAX_CTX, MAX_TOKENS, MAX_REPEATS = 131072, 8192, 20
MAX_TIMEOUT, MAX_LOG, MAX_TOTAL_LOG, MAX_RESULT = 7200, 8 * 1024 * 1024, 64 * 1024 * 1024, 8 * 1024 * 1024
REQUIRED = ("-m", "-ngl", "--kv-offload", "--no-kv-offload", "-ctk", "-ctv", "--fit", "--single-turn", "-p", "-n", "-c", "-b", "-ub", "-s", "--temp", "--simple-io", "--no-display-prompt", "--perf")
CACHE = ("f16", "q8_0", "q4_0")

class Refusal(Exception): pass

def positive(value, cap, name):
    n = int(value)
    if n < 1 or n > cap: raise argparse.ArgumentTypeError(f"{name} must be in 1..{cap}")
    return n

def safe_file(path, label):
    original = Path(path).expanduser()
    if original.is_symlink(): raise Refusal(f"{label} may not be a symlink: {original}")
    p = original.resolve(strict=True)
    if not p.is_file() or p.is_symlink(): raise Refusal(f"{label} must be an existing regular, non-symlink file: {p}")
    return p

def mem_available():
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'): return int(line.split()[1]) * 1024
    except (OSError, ValueError): pass
    return None

def proc_rss(pid):
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('VmRSS:'): return int(line.split()[1]) * 1024
    except (OSError, ValueError): pass
    return None

def tree_bytes(root):
    total = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base)/d).is_symlink()]
        for name in files:
            p = Path(base)/name
            try:
                if not p.is_symlink(): total += p.stat().st_size
            except OSError: pass
    return total

def load_ledger(path, root):
    if not path: raise Refusal("actual runs require --ledger: complete external byte accounting is mandatory")
    data = json.loads(Path(path).read_text())
    if data.get('complete') is not True: raise Refusal("ledger complete must be true; unknown external allocations block even a provisional budget pass")
    if Path(data.get('product_root','')).expanduser().resolve() != root: raise Refusal("ledger product_root must exactly match --product-root")
    if data.get('accounting_basis') != 'external_only': raise Refusal("ledger accounting_basis must explicitly be external_only")
    exclusions=data.get('overlap_exclusions')
    if not isinstance(exclusions,list) or not {'files_within_vhdx','overlapping_host_guest_views','product_root_overlap'}.issubset(set(exclusions)):
        raise Refusal("ledger overlap_exclusions must declare files_within_vhdx, overlapping_host_guest_views, and product_root_overlap")
    root_container=data.get('root_container_key')
    if root_container is not None and not isinstance(root_container,str): raise Refusal('root_container_key must be an allocation key or null')
    allocations=data.get('external_allocations_bytes')
    if not isinstance(allocations,dict) or not allocations: raise Refusal("ledger external_allocations_bytes must enumerate external allocations including full VHDX files")
    if any(not isinstance(v,int) or isinstance(v,bool) or v<0 for v in allocations.values()): raise Refusal("all external allocation values must be nonnegative integer bytes")
    if root_container is not None and root_container not in allocations: raise Refusal('root_container_key must name an external_allocations_bytes entry')
    data['_root_container_key']=root_container
    if data.get('model_projector_location') not in ('inside_product_root','inside_external_allocation'):
        raise Refusal("ledger requires model_projector_location: inside_product_root or inside_external_allocation")
    for field in ('model_bytes','projector_bytes','update_peak_bytes'):
        if not isinstance(data.get(field),int) or isinstance(data[field],bool) or data[field]<0: raise Refusal(f"ledger requires nonnegative integer {field}")
    return data

def gpu_snapshot():
    exe = shutil.which('nvidia-smi')
    if not exe: return None
    try:
        cp = subprocess.run([exe,'--query-gpu=memory.used,memory.free','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=0.5,check=True)
        return cp.stdout.strip()
    except Exception: return None

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--binary', required=True); ap.add_argument('--model', required=True); ap.add_argument('--projector'); ap.add_argument('--prompt', required=True)
    ap.add_argument('--output', default='bench-results')
    ap.add_argument('--product-root', required=True); ap.add_argument('--ledger')
    ap.add_argument('--context', type=lambda x: positive(x,MAX_CTX,'context'), default=4096)
    ap.add_argument('--tokens', type=lambda x: positive(x,MAX_TOKENS,'tokens'), default=128)
    ap.add_argument('--repeats', type=lambda x: positive(x,MAX_REPEATS,'repeats'), default=1)
    ap.add_argument('--timeout', type=lambda x: positive(x,MAX_TIMEOUT,'timeout'), default=120)
    ap.add_argument('--log-limit', type=lambda x: positive(x,MAX_LOG,'log-limit'), default=1*1024*1024)
    ap.add_argument('--layers', type=int, nargs='+', default=[0], help='explicit numeric layer expansion, at most four values / 24 total cases')
    ap.add_argument('--min-free-disk', type=lambda x: positive(x,100_000_000_000_000,'min-free-disk'), default=2_000_000_000)
    ap.add_argument('--min-available-ram', type=lambda x: positive(x,100_000_000_000_000,'min-available-ram'), default=1_000_000_000)
    ap.add_argument('--run', action='store_true', help='explicitly execute; default is plan only')
    a = ap.parse_args(argv)
    data=None; out=None
    def persist():
        if data is not None and out is not None:
            target=out/'plan.json'; tmp=out/'plan.json.tmp'
            encoded=json.dumps(data,indent=2)
            if len(encoded.encode()) > MAX_RESULT: raise Refusal('aggregate results exceed configured result-size bound')
            tmp.write_text(encoded); tmp.replace(target)
    proc=None
    try:
        binary=safe_file(a.binary,'binary'); model=safe_file(a.model,'model'); prompt=safe_file(a.prompt,'prompt')
        projector=safe_file(a.projector,'projector') if a.projector else None
        if not binary.stat().st_mode & 0o111: raise Refusal('binary is not executable')
        if not a.layers or len(a.layers)>4 or any(n < 0 or n > 999 for n in a.layers) or len(set(a.layers)) != len(a.layers): raise Refusal('layers must be 1..4 unique numeric values in 0..999')
        if len(a.layers)*len(CACHE)*2 > 24: raise Refusal('planned case cap exceeded (24)')
        root=Path(a.product_root).expanduser().resolve(strict=True)
        if not root.is_dir(): raise Refusal('product root must be an existing directory')
        outbase=Path(a.output).expanduser()
        if outbase.is_symlink(): raise Refusal('output path may not be a symlink')
        if a.output == 'bench-results':
            outbase.mkdir(parents=True,exist_ok=True)
            stamp=time.strftime('run-%Y%m%dT%H%M%SZ',time.gmtime()); out=outbase/stamp; suffix=1
            while out.exists(): out=outbase/f'{stamp}-{suffix:02d}'; suffix+=1
        else: out=outbase
        out=out.resolve()
        if out.exists(): raise Refusal(f'output already exists; refusing overwrite: {out}')
        out.mkdir(parents=True)
        plan=[]
        # Weights placement is -ngl. KV placement is separately CPU/default or GPU/offloaded.
        for layer in a.layers:
            for kv in ('gpu','cpu'):
                for cache in CACHE:
                    prompt_text=prompt.read_text()
                    prompt_size=len(prompt_text.encode())
                    if prompt_size > 64*1024: raise Refusal('prompt exceeds 64 KiB argv cap')
                    if '\x00' in prompt_text: raise Refusal('prompt contains NUL, invalid for argv')
                    cmd=[str(binary),'-m',str(model),'-p',prompt_text,'-n',str(a.tokens),'-c',str(a.context),'-b','512','-ub','128','-ngl',str(layer),'--fit','off','-ctk',cache,'-ctv',cache,'--kv-offload' if kv=='gpu' else '--no-kv-offload','--single-turn','--simple-io','--no-display-prompt','--perf','-s','42','--temp','0']
                    if projector: cmd += ['--mmproj',str(projector)]
                    plan.append({'weights_gpu_layers':layer,'kv_placement':kv,'kv_cache_kv_type':cache,'batch_size':512,'ubatch_size':128,'mode':'single_turn','sampler':{'seed':42,'temperature':0},'command':cmd})
        data={'status':'plan-only' if not a.run else 'not-run','runs':plan,'limits':{'max_context':MAX_CTX,'max_tokens':MAX_TOKENS,'max_repeats':MAX_REPEATS,'max_timeout_s':MAX_TIMEOUT,'max_log_bytes_per_case':MAX_LOG,'max_aggregate_log_bytes':MAX_TOTAL_LOG,'max_result_bytes':MAX_RESULT,'max_sample_records':20000,'max_planned_cases':24,'max_layers':4,'max_prompt_bytes':65536},'notes':['No model launch unless --run is specified.','No true TTFT measurement is available; stdout-first-byte proxy is intentionally null.','Host RAM/disk counters are unavailable to this guest runner; manual host checks are required and host/guest RAM must not be added.','Guest guards are best effort, not an OS sandbox or protection from instantaneous OOM. GPU/VRAM sampling is observational only, not a guard; Windows host guards are manual.','External network isolation is a prerequisite.']}
        if a.run:
            ledger=load_ledger(a.ledger,root)
            if ledger['model_bytes'] != model.stat().st_size: raise Refusal('ledger model_bytes does not match the supplied model file')
            expected_projector=projector.stat().st_size if projector else 0
            if ledger['projector_bytes'] != expected_projector: raise Refusal('ledger projector_bytes does not match the supplied projector file (or zero when omitted)')
            inputs=[model] + ([projector] if projector else [])
            actual_inside=all(p.is_relative_to(root) for p in inputs)
            claimed_inside=ledger['model_projector_location']=='inside_product_root'
            if actual_inside != claimed_inside: raise Refusal('ledger model_projector_location does not match resolved model/projector input paths')
            if not claimed_inside:
                key=ledger.get('model_projector_allocation_key')
                if not isinstance(key,str) or key not in ledger['external_allocations_bytes']:
                    raise Refusal('external model/projector paths require model_projector_allocation_key naming a ledger external allocation')
                if ledger['external_allocations_bytes'][key] < ledger['model_bytes'] + ledger['projector_bytes']:
                    raise Refusal('model/projector external allocation is smaller than their actual combined file bytes')
            root_bytes=tree_bytes(root); external_bytes=sum(ledger['external_allocations_bytes'].values())
            root_key=ledger['_root_container_key']
            container_bytes=ledger['external_allocations_bytes'].get(root_key,0) if root_key else 0
            root_counted=max(root_bytes,container_bytes)
            nonoverlap_external=external_bytes-container_bytes
            log_headroom=min(len(plan)*a.repeats*a.log_limit,MAX_TOTAL_LOG+a.log_limit)
            result_headroom=MAX_RESULT
            projected=root_counted+nonoverlap_external+ledger['update_peak_bytes']+log_headroom+result_headroom
            data['budget']={'ceiling_bytes':BUDGET,'accounting_basis':'external_only','product_root_logical_bytes':root_bytes,'root_container_key':root_key,'root_container_allocation_bytes':container_bytes,'root_and_container_counted_once_bytes':root_counted,'external_allocations_bytes':ledger['external_allocations_bytes'],'external_allocations_total_bytes':external_bytes,'nonoverlap_external_bytes':nonoverlap_external,'model_bytes_informational':ledger['model_bytes'],'projector_bytes_informational':ledger['projector_bytes'],'model_projector_location':ledger['model_projector_location'],'update_peak_bytes':ledger['update_peak_bytes'],'planned_log_headroom_bytes':log_headroom,'planned_result_headroom_bytes':result_headroom,'projected_peak_bytes':projected,'certified_pass':False,'unknown_host_counters':['physical VHDX allocation','host-side overlap/deduplication'],'overlap_exclusions':ledger['overlap_exclusions'],'accounting_note':'Count max(product-root logical bytes, mapped enclosing container allocation) once, plus other non-overlapping external allocations and update peak. Model/projector sizes are informational only. Host physical VHDX allocation and host/guest deduplication remain unknown, so no certified pass.'}
            persist()
            if data['budget']['projected_peak_bytes']>BUDGET: raise Refusal('100 GB product budget exceeded by ledger projected peak')
            usage=shutil.disk_usage(out.parent)
            if usage.free<a.min_free_disk: raise Refusal('guest free disk below configured minimum')
            available=mem_available()
            if available is None or available<a.min_available_ram: raise Refusal('guest MemAvailable unavailable or below configured minimum')
            # Mandatory local capability check; no fallback on any unsupported flag.
            clean_env={k:v for k,v in os.environ.items() if not k.startswith('LLAMA_ARG_')}
            ver=subprocess.run([str(binary),'--version'],capture_output=True,text=True,timeout=10,env=clean_env)
            helpcp=subprocess.run([str(binary),'--help'],capture_output=True,text=True,timeout=10,env=clean_env)
            helptext=helpcp.stdout+'\n'+helpcp.stderr
            version=ver.stdout+'\n'+ver.stderr
            data['binary_check']={'version_status':ver.returncode,'version':version[-4000:],'help_status':helpcp.returncode,'help_required_flags':{f:f in helptext for f in (REQUIRED + ((('--mmproj',) if projector else ())))}}
            persist()
            if ver.returncode or helpcp.returncode or any(f not in helptext for f in (REQUIRED + ((('--mmproj',) if projector else ())))): raise Refusal('local --help/--version failed or required flags unsupported; no run attempted')
            if not out.is_relative_to(root): raise Refusal('--run output must be inside product root so its logs count toward budget')
            data['status']='running'; persist()
            data['inherited_llama_arg_environment_removed_for_probes_and_runs']=True; persist()
            sample_count=0
            for ix,item in enumerate(plan):
                result={'case':item,'repeats':[]}
                data['runs'][ix]=result; persist()
                for rep in range(a.repeats):
                    log=out/f'case-{ix:03d}-rep-{rep+1:02d}.log'
                    start=time.monotonic(); proc=None; samples=[]; timed=False; oversized=False
                    attempt={'status':'running','log_file':log.name,'samples':samples,'returncode':None}
                    result['repeats'].append(attempt); persist()
                    try:
                        available_before=mem_available(); free_before=shutil.disk_usage(out.parent).free
                        if available_before is None or available_before<a.min_available_ram: raise Refusal('guest MemAvailable below configured minimum immediately before case')
                        if free_before<a.min_free_disk: raise Refusal('guest free disk below configured minimum immediately before case')
                        with log.open('xb') as f:
                            proc=subprocess.Popen(item['command'],stdout=f,stderr=subprocess.STDOUT,start_new_session=True,env=clean_env,preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE,(a.log_limit,a.log_limit)))
                            last_sample=0.0
                            while proc.poll() is None:
                                now=time.monotonic(); available=mem_available(); free=shutil.disk_usage(out.parent).free
                                if now-last_sample >= 1.0:
                                    sample_count += 1
                                    if sample_count > 20000: raise Refusal('aggregate sample cap exceeded')
                                    samples.append({'elapsed_s':round(now-start,2),'guest_mem_available_bytes':available,'guest_free_disk_bytes':free,'process_rss_bytes':proc_rss(proc.pid),'nvidia_smi':gpu_snapshot()}); last_sample=now
                                aggregate_logs=sum(x.stat().st_size for x in out.glob('*.log'))
                                if aggregate_logs>=MAX_TOTAL_LOG or log.stat().st_size>=a.log_limit: oversized=True
                                if available is None or available<a.min_available_ram: raise Refusal('guest RAM guard tripped during case')
                                if free<a.min_free_disk: raise Refusal('guest disk guard tripped during case')
                                if oversized or now-start>a.timeout:
                                    timed=not oversized
                                    try: os.killpg(proc.pid,signal.SIGKILL)
                                    except ProcessLookupError: pass
                                    proc.wait(); break
                                time.sleep(.25)
                    except BaseException as exc:
                        attempt['error']=str(exc) or type(exc).__name__
                        raise
                    finally:
                        if proc is not None and proc.poll() is None:
                            try: os.killpg(proc.pid,signal.SIGKILL)
                            except ProcessLookupError: pass
                            proc.wait()
                        attempt['returncode']=proc.returncode if proc is not None else None
                        attempt['wall_seconds']=time.monotonic()-start
                        attempt['timed_out']=timed; attempt['log_limit_exceeded']=oversized
                        attempt['status']='finished' if proc is not None and proc.returncode==0 and not timed and not oversized else 'failed'
                        persist()
                    if log.exists() and log.stat().st_size >= a.log_limit: oversized=True
                    attempt['log_limit_exceeded']=oversized
                    if proc.returncode and not attempt.get('error'):
                        attempt['error']='timed out' if timed else ('log limit exceeded' if oversized else f'child exited with status {proc.returncode}')
                    raw=log.read_text(errors='replace')[-a.log_limit:]
                    perf=[x.strip() for x in raw.splitlines() if re.search(r'(eval time|prompt eval time|tokens per second|tok/s)',x,re.I)]
                    attempt.update({'stderr_perf_lines':perf,'stdout_first_byte_proxy_seconds':None})
                    persist()
                    if proc.returncode or timed or oversized: raise Refusal('case failed, timed out, or exceeded log limit; remaining cases stopped')
            data['status']='completed'
        data['status']='completed' if a.run else 'plan-only'; persist()
        print(json.dumps({'status':data['status'],'output':str(out),'cases':len(plan)},indent=2))
        return 0
    except (Refusal,OSError,ValueError,subprocess.TimeoutExpired,json.JSONDecodeError,KeyError,TypeError,KeyboardInterrupt) as e:
        if proc is not None and proc.poll() is None:
            try: os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            proc.wait()
        if data is not None:
            data['status']='interrupted' if isinstance(e,KeyboardInterrupt) else 'refused'
            data['error']=str(e) if not isinstance(e,KeyboardInterrupt) else 'KeyboardInterrupt; active child process group terminated'
            try: persist()
            except Exception: pass
        print(f'REFUSED/FAILED: {e}',file=sys.stderr); return 130 if isinstance(e,KeyboardInterrupt) else 2

if __name__=='__main__': sys.exit(main())
