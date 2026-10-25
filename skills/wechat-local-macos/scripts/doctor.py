#!/usr/bin/env python3
"""Report local capabilities without reading message contents or key material."""
import argparse
import json
from pathlib import Path
import platform
import plistlib
import shutil
import sys


def inspect(app, root=None):
    result = {'platform': platform.system(), 'machine': platform.machine(), 'python': platform.python_version(),
              'lldb_available': shutil.which('lldb') is not None, 'sqlcipher_cli_available': shutil.which('sqlcipher') is not None,
              'sqlcipher_library_candidates': [str(p) for p in [Path('/opt/homebrew/opt/sqlcipher/lib/libsqlcipher.dylib'), Path('/usr/local/opt/sqlcipher/lib/libsqlcipher.dylib')] if p.is_file()]}
    plist = app / 'Contents/Info.plist'
    if plist.is_file():
        data = plistlib.loads(plist.read_bytes())
        result['wechat_version'] = data.get('CFBundleShortVersionString')
        result['wechat_build'] = data.get('CFBundleVersion')
    base = Path.home() / 'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files'
    try:
        candidates = [root.resolve(strict=True)] if root else sorted(base.glob('*/db_storage'))
        result['database_roots'] = [{'path': str(p), 'database_count': sum(1 for _ in p.rglob('*.db'))} for p in candidates]
    except PermissionError:
        result['database_access'] = 'permission_denied: grant Full Disk Access to the executing host, then retry'
    except OSError as e:
        result['database_access'] = type(e).__name__
    result['discovery_note'] = 'Zero roots may mean an unavailable container, no local account data, or denied macOS access; this does not diagnose a key error.'
    return result


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--app', type=Path, default=Path('/Applications/WeChat.app'))
    ap.add_argument('--db-root', type=Path)
    args = ap.parse_args()
    print(json.dumps(inspect(args.app, args.db_root), ensure_ascii=False, indent=2))
