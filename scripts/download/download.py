#!/usr/bin/env python3
"""Pinned, resumable downloads for the three reviewed local bundles."""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

MANIFEST_PATH = Path(__file__).with_name('manifest.json')
HOME_DEST = Path.home() / 'local-ai-guru-data'
CHUNK = 1024 * 1024

class DownloadError(Exception):
    pass

class HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() != 'https':
            raise DownloadError(f'refusing non-HTTPS redirect: {newurl}')
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def _https_open(request, timeout=60):
    return urllib.request.build_opener(HTTPSOnlyRedirectHandler()).open(request, timeout=timeout)

def manifest():
    data = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))
    if set(data.get('bundles', {})) != {'runtime', 'qwen', 'gemma'}:
        raise DownloadError('manifest must contain exactly runtime, qwen, and gemma bundles')
    return data

def selected_info(bundle_id, data=None):
    data = data or manifest()
    bundle = data['bundles'][bundle_id]
    dest = HOME_DEST / bundle['destination']
    return {
        'bundle': bundle_id,
        'size_bytes': sum(a['bytes'] for a in bundle['assets']),
        'destination': str(dest),
        'source': [a['url'] for a in bundle['assets']],
    }

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(CHUNK), b''):
            digest.update(block)
    return digest.hexdigest()

def verify_complete(path: Path, asset: dict) -> bool:
    if path.is_symlink():
        raise DownloadError(f'refusing symlink input: {path}')
    if not path.exists():
        return False
    if not path.is_file():
        raise DownloadError(f'expected a regular file: {path}')
    size = path.stat().st_size
    if size < asset['bytes']:
        return False
    if size > asset['bytes']:
        raise DownloadError(f'file is larger than pinned size; preserved for inspection: {path}')
    actual = sha256(path)
    if actual != asset['sha256']:
        raise DownloadError(f'complete file SHA-256 mismatch; preserved without overwrite/delete: {path}')
    return True

@contextlib.contextmanager
def bundle_lock(destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    lock = destination / '.download.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise DownloadError(f'bundle is locked or a stale lock exists: {lock}') from exc
    inode = os.fstat(fd).st_ino
    try:
        os.write(fd, f'pid={os.getpid()}\n'.encode())
        os.fsync(fd)
        yield
    finally:
        os.close(fd)
        try:
            if lock.stat(follow_symlinks=False).st_ino == inode:
                lock.unlink()
        except FileNotFoundError:
            pass

def _response_status(response):
    status = getattr(response, 'status', None)
    return status if status is not None else response.getcode()

def download_asset(asset: dict, destination: Path, opener=None, progress=None) -> str:
    """Verify, adopt, or resume one pinned asset. Partial bytes remain on failure."""
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / asset['local_name']
    total = asset['bytes']
    def report(stage, done):
        if progress:
            progress(asset, stage, done, total)
    if verify_complete(path, asset):
        report('verified-existing', total)
        return 'verified-existing'
    if path.is_symlink():
        raise DownloadError(f'refusing symlink partial: {path}')
    existed = path.exists()
    offset = path.stat().st_size if existed else 0
    if offset > total:
        raise DownloadError(f'partial exceeds pinned size; preserved: {path}')
    parsed_url = urllib.parse.urlsplit(asset['url'])
    if parsed_url.scheme.lower() != 'https':
        raise DownloadError(f'only HTTPS asset URLs are allowed: {asset["url"]}')
    report('resuming' if existed else 'starting', offset)
    headers = {'User-Agent': 'local-ai-guru-pinned-fetch/1'}
    if existed:
        headers['Range'] = f'bytes={offset}-'
    request = urllib.request.Request(asset['url'], headers=headers)
    open_url = opener or _https_open
    try:
        response = open_url(request, timeout=60)
    except (OSError, http.client.HTTPException, urllib.error.URLError) as exc:
        raise DownloadError(f'download request failed; partial preserved: {exc}') from exc
    with response:
        status = _response_status(response)
        if status == 206:
            content_range = response.headers.get('Content-Range', '')
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', content_range)
            if not match:
                raise DownloadError(f'malformed Content-Range; partial preserved: {path}')
            range_start, range_end, range_total = map(int, match.groups())
            if range_start != offset or range_total != total or range_end < range_start or range_end >= range_total:
                raise DownloadError(f'Content-Range does not match pinned size/resume offset; partial preserved: {path}')
        elif status == 200 and offset == 0:
            pass
        else:
            raise DownloadError(f'server did not honor resume offset {offset} (HTTP {status}); partial preserved: {path}')
        # Empty pre-existing manual files are valid resume points too.
        mode = 'ab' if existed else 'xb'
        last_reported = (offset * 10 // total) if total else 10
        try:
            with path.open(mode) as output:
                since_sync = 0
                while True:
                    block = response.read(CHUNK)
                    if not block:
                        break
                    if output.tell() + len(block) > total:
                        raise DownloadError(f'response exceeds pinned size; partial preserved: {path}')
                    output.write(block)
                    output.flush()
                    since_sync += len(block)
                    if since_sync >= 64 * CHUNK:
                        os.fsync(output.fileno())
                        since_sync = 0
                    now_reported = output.tell() * 10 // total if total else 10
                    if now_reported > last_reported:
                        report(f'progress-{min(100, now_reported * 10)}%', output.tell())
                        last_reported = now_reported
                os.fsync(output.fileno())
        except (OSError, http.client.HTTPException) as exc:
            raise DownloadError(f'download interrupted; partial preserved for resume: {path}: {exc}') from exc
    if not verify_complete(path, asset):
        raise DownloadError(f'incomplete download remains resumable: {path} ({path.stat().st_size}/{total} bytes)')
    report('verified', total)
    return 'downloaded-and-verified'

def _member_name(name: str) -> str:
    if '\\' in name:
        raise DownloadError(f'backslash in archive member path: {name!r}')
    p = PurePosixPath(name)
    if p.is_absolute() or any(part == '..' for part in p.parts):
        raise DownloadError(f'absolute/traversal archive path rejected: {name!r}')
    parts = tuple(part for part in p.parts if part not in ('', '.'))
    return '/'.join(parts)

def _link_target(member_name: str, linkname: str, hard: bool) -> str:
    if not linkname or '\\' in linkname:
        raise DownloadError(f'invalid archive link target: {linkname!r}')
    target = PurePosixPath(linkname)
    if target.is_absolute() or (hard and any(part == '..' for part in target.parts)):
        raise DownloadError(f'absolute/traversal archive link rejected: {linkname!r}')
    if hard:
        return _member_name(linkname)
    parent = posixpath.dirname(member_name)
    resolved = posixpath.normpath(posixpath.join(parent, linkname))
    if resolved == '..' or resolved.startswith('../') or resolved.startswith('/'):
        raise DownloadError(f'archive symlink escapes extraction root: {member_name} -> {linkname}')
    return '' if resolved == '.' else resolved

def validate_members(members):
    entries = {}
    symlinks = {}
    hardlinks = {}
    for member in members:
        name = _member_name(member.name)
        if not name:
            if member.isdir():
                continue
            raise DownloadError('non-directory archive root member rejected')
        if member.isdir(): kind = 'dir'
        elif member.isfile(): kind = 'file'
        elif member.issym(): kind = 'symlink'
        elif member.islnk(): kind = 'hardlink'
        else: raise DownloadError(f'unsupported tar member type rejected: {member.name!r}')
        if name in entries:
            if kind == entries[name]['kind'] == 'dir':
                continue
            raise DownloadError(f'duplicate archive member rejected: {name}')
        entry = {'name': name, 'kind': kind, 'member': member}
        entries[name] = entry
        if kind == 'symlink':
            symlinks[name] = _link_target(name, member.linkname, False)
        elif kind == 'hardlink':
            hardlinks[name] = _link_target(name, member.linkname, True)
    # No member may be materialized below a link; this also prevents writes through links.
    for name in entries:
        parts = name.split('/')
        for i in range(1, len(parts)):
            ancestor = '/'.join(parts[:i])
            if ancestor in entries and entries[ancestor]['kind'] in ('symlink', 'hardlink'):
                raise DownloadError(f'archive member would write through link {ancestor!r}: {name!r}')
    def resolve(name, trail=()):
        if name in trail:
            raise DownloadError(f'archive link cycle: {" -> ".join((*trail, name))}')
        if name in symlinks:
            target = symlinks[name]
            if target not in entries:
                raise DownloadError(f'archive symlink target is absent: {name!r} -> {target!r}')
            return resolve(target, (*trail, name))
        if name in hardlinks:
            target = hardlinks[name]
            if target not in entries:
                raise DownloadError(f'archive hardlink target is absent: {name!r} -> {target!r}')
            return resolve(target, (*trail, name))
        return name
    for name in (*symlinks.keys(), *hardlinks.keys()):
        final = resolve(name)
        if final not in entries or entries[final]['kind'] not in ('file', 'dir'):
            raise DownloadError(f'archive link does not resolve to an in-root file/directory: {name!r}')
        if name in hardlinks and entries[final]['kind'] != 'file':
            raise DownloadError(f'hardlink target is not a regular file: {name!r}')
    return entries, symlinks, hardlinks, resolve

def _mode(member):
    return member.mode & 0o777

def _stage_archive(archive_path: Path, stage: Path):
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = archive.getmembers()
        entries, symlinks, hardlinks, resolve = validate_members(members)
        dirs = [e for e in entries.values() if e['kind'] == 'dir']
        files = [e for e in entries.values() if e['kind'] == 'file']
        # Make all directories and regular files before creating any links.
        for entry in sorted(dirs, key=lambda e: (e['name'].count('/'), e['name'])):
            path = stage / entry['name']
            path.mkdir(parents=True, exist_ok=True)
        for entry in files:
            path = stage / entry['name']
            path.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(entry['member'])
            if source is None:
                raise DownloadError(f'cannot read archive member: {entry["name"]}')
            with source, path.open('xb') as output:
                shutil.copyfileobj(source, output, CHUNK)
            path.chmod(_mode(entry['member']))
        for name, target in hardlinks.items():
            canonical = resolve(name)
            src = stage / canonical
            dst = stage / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.link(src, dst)
        for name, entry in entries.items():
            if entry['kind'] == 'symlink':
                path = stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(entry['member'].linkname, path)
        for entry in sorted(dirs, key=lambda e: (-e['name'].count('/'), e['name'])):
            (stage / entry['name']).chmod(_mode(entry['member']))
        return entries, symlinks, hardlinks, resolve

def _hash_equal(a: Path, b: Path) -> bool:
    return a.is_file() and not a.is_symlink() and a.stat().st_size == b.stat().st_size and sha256(a) == sha256(b)

def _safe_install_path(install: Path, relative: str) -> Path:
    target = install.joinpath(*relative.split('/'))
    if not target.resolve(strict=False).is_relative_to(install.resolve()):
        raise DownloadError(f'install path escapes runtime root: {relative}')
    for parent in target.parents:
        if parent == install.parent:
            break
        if parent.is_symlink():
            raise DownloadError(f'refusing extraction through symlinked path: {parent}')
    return target

def reconcile_runtime_archive(archive_path: Path, install: Path, asset: dict):
    """Verify, stage, preflight conflicts, then add missing archive members only."""
    if not verify_complete(archive_path, asset):
        raise DownloadError(f'archive is not complete and hash-verified: {archive_path}')
    if install.is_symlink():
        raise DownloadError(f'refusing symlink extraction root: {install}')
    install.mkdir(parents=True, exist_ok=True)
    if not install.is_dir():
        raise DownloadError(f'extraction root is not a directory: {install}')
    with tempfile.TemporaryDirectory(prefix='.extract-stage-', dir=install.parent) as temp_name:
        stage = Path(temp_name)
        entries, symlinks, hardlinks, resolve = _stage_archive(archive_path, stage)
        # Conflict scan before changing any installed member.
        for name, entry in entries.items():
            target = _safe_install_path(install, name)
            kind = entry['kind']
            if not target.exists() and not target.is_symlink():
                continue
            if kind == 'dir':
                if target.is_symlink() or not target.is_dir():
                    raise DownloadError(f'existing modified extraction directory; preserved: {target}')
            elif kind in ('file', 'hardlink'):
                canonical = resolve(name) if kind == 'hardlink' else name
                staged_file = stage / canonical
                expected_mode = _mode(entries[canonical]['member'])
                if target.is_symlink() or not _hash_equal(target, staged_file) or (target.stat().st_mode & 0o777) != expected_mode:
                    raise DownloadError(f'existing modified extraction path; preserved without overwrite: {target}')
            elif kind == 'symlink':
                if not target.is_symlink() or os.readlink(target) != entry['member'].linkname:
                    raise DownloadError(f'existing extraction symlink conflicts; preserved: {target}')
        # Add directories and regular files exclusively; never replace existing paths.
        created_dirs = set()
        for name, entry in sorted(entries.items(), key=lambda item: (item[0].count('/'), item[0])):
            if entry['kind'] != 'dir':
                continue
            target = _safe_install_path(install, name)
            if not target.exists():
                target.mkdir(parents=True, exist_ok=False, mode=_mode(entry['member']))
                created_dirs.add(name)
            if target.is_symlink() or not target.is_dir():
                raise DownloadError(f'unsafe extraction directory path: {target}')
        for name, entry in entries.items():
            if entry['kind'] != 'file':
                continue
            target = _safe_install_path(install, name)
            source = stage / name
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if any(p.is_symlink() for p in target.parents if p != install.parent and p != install):
                raise DownloadError(f'refusing extraction through a symlinked parent: {target}')
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _mode(entry['member']))
            with os.fdopen(fd, 'wb') as output, source.open('rb') as input_stream:
                shutil.copyfileobj(input_stream, output, CHUNK)
            os.chmod(target, _mode(entry['member']))
        for name, entry in entries.items():
            if entry['kind'] != 'hardlink':
                continue
            target = _safe_install_path(install, name)
            if target.exists():
                continue
            canonical = resolve(name)
            source = _safe_install_path(install, canonical)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(source, target)
        for name, entry in entries.items():
            if entry['kind'] != 'symlink':
                continue
            target = _safe_install_path(install, name)
            if target.is_symlink():
                continue
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(entry['member'].linkname, target)
        for name in sorted(created_dirs, key=lambda item: (-item.count('/'), item)):
            _safe_install_path(install, name).chmod(_mode(entries[name]['member']))

def run_bundle(bundle_id: str, progress=None, data=None) -> dict:
    data = data or manifest()
    if bundle_id not in data['bundles']:
        raise DownloadError(f'unknown bundle: {bundle_id}')
    bundle = data['bundles'][bundle_id]
    destination = HOME_DEST / bundle['destination']
    with bundle_lock(destination):
        states = []
        for asset in bundle['assets']:
            states.append({'file': asset['local_name'], 'status': download_asset(asset, destination, progress=progress)})
        if bundle_id == 'runtime':
            install = destination
            # Both official archives have been hash-verified before extraction begins.
            for asset in bundle['assets']:
                reconcile_runtime_archive(destination / asset['local_name'], install, asset)
        return {'bundle': bundle_id, 'destination': str(destination), 'assets': states,
                'extracted_to': str(destination) if bundle_id == 'runtime' else None}

def main(bundle_id: str, argv=None):
    parser = argparse.ArgumentParser(description=f'Fetch the pinned {bundle_id} bundle only.')
    parser.add_argument('--dry-run', action='store_true', help='print selected bundle metadata; no network or extraction')
    args = parser.parse_args(argv)
    try:
        data = manifest()
        info = selected_info(bundle_id, data)
        if args.dry_run:
            print(json.dumps(info, ensure_ascii=False, indent=2), flush=True)
            return 0
        print(json.dumps(info, ensure_ascii=False, indent=2), flush=True)
        def progress(asset, stage, done, total):
            print(f'{asset["local_name"]}: {stage} ({done}/{total} bytes)', flush=True)
        result = run_bundle(bundle_id, progress=progress, data=data)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0
    except KeyboardInterrupt:
        print('INTERRUPTED: partial download retained for resume; bundle lock released', file=sys.stderr)
        return 130
    except (DownloadError, OSError, ValueError, tarfile.TarError, urllib.error.URLError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2

if __name__ == '__main__':
    print('Use runtime.py, qwen.py, or gemma.py to select exactly one bundle.', file=sys.stderr)
    raise SystemExit(2)
