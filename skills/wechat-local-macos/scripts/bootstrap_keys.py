#!/usr/bin/env python3
"""Explicit, bounded debug-copy bootstrap. Never re-sign the installed application."""
import argparse
import json
import os
from pathlib import Path
import platform
import signal
import subprocess as sp
import sys
import time

from voice_export import private_directory
from wechat_db import root_for


def emit(kind, **items):
    print(json.dumps(dict(type=kind, **items)), flush=True)


def run(args, timeout=45):
    result = sp.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('command_failed: ' + Path(args[0]).name)
    return result.stdout or result.stderr


def wechat_processes():
    result = sp.run(['pgrep', '-x', 'WeChat'], capture_output=True, text=True, timeout=5)
    if result.returncode not in (0, 1):
        raise RuntimeError('process_inventory_failed')
    found = []
    for value in result.stdout.split():
        pid = int(value)
        result = sp.run(['ps', '-p', str(pid), '-o', 'comm='], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            found.append((pid, result.stdout.strip()))
    return found


def close_exact_copy(executable):
    """Only signal processes whose current executable is this newly created copy."""
    for sig, seconds in ((signal.SIGTERM, 5), (signal.SIGKILL, 3)):
        owned = [(pid, name) for pid, name in wechat_processes() if name == str(executable)]
        if not owned:
            return True
        for pid, _ in owned:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not any(name == str(executable) for _, name in wechat_processes()):
                return True
            time.sleep(.2)
    return not any(name == str(executable) for _, name in wechat_processes())


def stop_capture(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except sp.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except sp.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--app', type=Path, default=Path('/Applications/WeChat.app'))
    ap.add_argument('--db-root', type=Path)
    ap.add_argument('--state-dir', type=Path, required=True)
    ap.add_argument('--timeout', type=int, default=180)
    ap.add_argument('--acknowledge-debug-copy', action='store_true')
    args = ap.parse_args()
    if not args.acknowledge_debug_copy:
        ap.error('Read references/bootstrap.md; quit WeChat normally, then use --acknowledge-debug-copy only with authorization.')
    if not 10 <= args.timeout <= 300:
        ap.error('--timeout must be 10..300 seconds')
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('native Apple Silicon macOS is required; do not use Rosetta')
    if os.geteuid() == 0:
        raise RuntimeError('do_not_run_as_root')
    if wechat_processes():
        raise RuntimeError('quit WeChat normally first; no processes were stopped')
    os.umask(0o077)
    app = args.app.resolve(strict=True)
    original_exe = app / 'Contents/MacOS/WeChat'
    if not original_exe.is_file():
        raise RuntimeError('wechat_executable_missing')
    root = root_for(args.db_root)
    if not list(root.rglob('*.db')):
        raise RuntimeError('no_local_databases')
    if args.state_dir.resolve().is_relative_to(root) or args.state_dir.resolve().is_relative_to(app):
        raise RuntimeError('state_must_be_outside_source_data_and_installed_app')
    state = private_directory(args.state_dir)
    keys, shadow, disabled = state/'keys.json', state/'WeChat-trial.app', state/'WeChat-trial.disabled'
    if any(p.exists() for p in (keys, keys.with_name('keys.json.partial'), shadow, disabled)):
        raise RuntimeError('previous_state_exists; choose a new state directory')
    signature = run(['codesign', '-dvv', str(app)])
    run(['codesign', '--verify', '--deep', '--strict', str(app)])
    (state/'original-signature.txt').write_text(signature)
    lldb_python = run(['/usr/bin/lldb', '-P']).strip()
    env = dict(os.environ, PYTHONPATH=lldb_python)
    proc = None
    rc = 2
    restore_ok = False
    try:
        run(['/usr/bin/ditto', str(app), str(shadow)], timeout=180)
        run(['codesign', '--force', '--deep', '--sign', '-', str(shadow)], timeout=90)
        run(['codesign', '--verify', '--deep', '--strict', str(shadow)])
        emit('copy_prepared', instruction='Complete entry/login in the temporary window; host and copy may each need Full Disk Access.')
        command = ['/usr/bin/python3', str(Path(__file__).with_name('capture_keys.py')),
                   '--exe', str(shadow/'Contents/MacOS/WeChat'), '--db-root', str(root),
                   '--out', str(keys), '--timeout', str(args.timeout), '--status-file', str(state/'capture-status.json')]
        proc = sp.Popen(command, env=env)
        try:
            rc = proc.wait(timeout=args.timeout + 30)
        except sp.TimeoutExpired:
            emit('capture_watchdog_timeout')
            rc = 124
        except KeyboardInterrupt:
            rc = 130
    finally:
        try:
            stop_capture(proc)
        except Exception as error:
            emit('capture_cleanup_error', reason=type(error).__name__)
        try:
            closed = close_exact_copy(shadow/'Contents/MacOS/WeChat')
        except Exception as error:
            closed = False
            emit('temporary_process_cleanup_error', reason=type(error).__name__)
        emit('temporary_process_cleanup', confirmed_closed=closed)
        try:
            run(['codesign', '--verify', '--deep', '--strict', str(app)])
            if run(['codesign', '-dvv', str(app)]) != signature:
                raise RuntimeError('signature_identity_changed')
            emit('original_signature_verified')
            if closed:
                if shadow.exists():
                    ls = '/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister'
                    sp.run([ls, '-u', str(shadow)], capture_output=True, timeout=15)
                    shadow.rename(disabled)
                emit('temporary_bundle_disabled', confirmed=not shadow.exists())
                running = wechat_processes()
                if not running:
                    restored = sp.Popen([str(original_exe)], stdout=sp.DEVNULL, stderr=sp.DEVNULL, start_new_session=True)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if restored.poll() is not None:
                            break
                        if any(pid == restored.pid and name == str(original_exe) for pid, name in wechat_processes()):
                            restore_ok = True
                            break
                        time.sleep(.2)
                else:
                    restore_ok = any(name == str(original_exe) for _, name in running)
                emit('original_process_restored', confirmed=restore_ok, window_and_login_require_user_check=True)
        except Exception as error:
            emit('manual_restore_required', reason=type(error).__name__)
        emit('temporary_full_disk_access', state='manual_revocation_required; confirm disabled in System Settings')
    if keys.exists():
        data = json.loads(keys.read_text())
        emit('keys_saved_privately', verified_databases=len(data['keys']), complete=data.get('complete', False), permissions=oct(keys.stat().st_mode & 0o777))
    if not restore_ok:
        emit('manual_restore_required', reason='original process not confirmed; do not launch another temporary copy')
        return 2
    return rc


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        emit('bootstrap_error', reason=type(error).__name__ + ': ' + str(error))
        sys.exit(2)
