"""Attach the Royal Match MOD menu to a running game process.

Keep this process running while you use the menu; Ctrl+C removes it and detaches.

    pip install frida
    python run_menu.py --port 27042

The device must already be running frida-server (or Florida) and be reachable
over `adb forward`. Run with --help for device/port selection.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import frida

ROOT = Path(__file__).resolve().parent
PKG = 'com.dreamgames.royalmatch'

# The class/field/method names baked into royal_mod.js come from one specific
# build's IL2CPP metadata, so the launcher checks the version before attaching.
VALIDATED_VERSION = '38174'

DEFAULT_PORTS = [27042, 27055, 1337, 27043]


def find_adb():
    """Locate adb on PATH, else under the usual Android SDK locations."""
    exe = shutil.which('adb')
    if exe:
        return exe
    sdk_roots = [
        os.environ.get('ANDROID_HOME'),
        os.environ.get('ANDROID_SDK_ROOT'),
        os.path.expanduser('~/Android/Sdk'),
        os.path.expanduser('~/AppData/Local/Android/Sdk'),
    ]
    name = 'adb.exe' if os.name == 'nt' else 'adb'
    for root in sdk_roots:
        if not root:
            continue
        candidate = Path(root) / 'platform-tools' / name
        if candidate.exists():
            return str(candidate)
    sys.exit('adb not found: install Android platform-tools, or put adb on PATH.')


def attach_with_retry(device, pid, attempts=6):
    """Attach to pid, retrying: a first-try failure is common on remote devices."""
    last = None
    for _ in range(attempts):
        try:
            return device.attach(pid)
        except Exception as exc:
            last = exc
            time.sleep(1)
    raise last


def probe_port(adb_sh, pid, candidates):
    """Return the first candidate port the device speaks frida on.

    Probes with attach(pid) rather than enumerate_processes(): some
    frida-server builds answer attach but not enumeration over a remote device.
    """
    for port in candidates:
        try:
            adb_sh('forward', f'tcp:{port}', f'tcp:{port}')
            device = frida.get_device_manager().add_remote_device(f'127.0.0.1:{port}')
            session = attach_with_retry(device, pid, attempts=2)
            session.detach()
            return port
        except Exception:
            continue
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--serial', help='device serial; omit when exactly one device is connected')
    parser.add_argument('--port', type=int, help='frida-server port on the device (default: auto-detect)')
    parser.add_argument('--duration', type=int, default=0, help='seconds to stay attached; 0 runs until Ctrl+C')
    parser.add_argument('--allow-any-version', action='store_true',
                        help=f'attach even when the game build is not {VALIDATED_VERSION}')
    args = parser.parse_args()

    adb = [find_adb()]
    if args.serial:
        adb += ['-s', args.serial]
    else:
        listing = subprocess.check_output(adb + ['devices'], text=True).splitlines()[1:]
        serials = [line.split()[0] for line in listing if line.strip().endswith('device')]
        if len(serials) != 1:
            sys.exit(f'Expected exactly one authorized device, found {len(serials)}. '
                     f'Pass --serial (adb devices -l lists them).')
        adb += ['-s', serials[0]]

    def sh(*cmd):
        return subprocess.check_output(adb + list(cmd), text=True, timeout=30).strip()

    match = re.search(r'versionName=(\S+)', sh('shell', 'dumpsys', 'package', PKG))
    if not match:
        sys.exit(f'{PKG} is not installed on this device.')
    version = match.group(1)
    if version != VALIDATED_VERSION and not args.allow_any_version:
        sys.exit(f'Game build is {version}; royal_mod.js is validated against '
                 f'{VALIDATED_VERSION}. Pass --allow-any-version to try anyway.')

    # A dozing device keeps the Unity bootstrap from advancing, which looks like
    # a failed attach. Wake it and keep the screen on for the session.
    sh('shell', 'input', 'keyevent', '224')
    sh('shell', 'wm', 'dismiss-keyguard')

    pid = int(sh('shell', 'pidof', PKG).split()[0])

    port = args.port
    if port is None:
        port = probe_port(sh, pid, DEFAULT_PORTS)
        if port is None:
            sys.exit(f'No frida-server reachable on ports {DEFAULT_PORTS}. '
                     f'Start frida-server on the device, then pass --port.')
    else:
        sh('forward', f'tcp:{port}', f'tcp:{port}')

    device = frida.get_device_manager().add_remote_device(f'127.0.0.1:{port}')
    session = attach_with_retry(device, pid)
    script = session.create_script((ROOT / 'royal_mod.js').read_text(encoding='utf-8-sig'))
    script.on('message', lambda message, data: print(json.dumps(message, ensure_ascii=False), flush=True))

    detached = []
    session.on('detached', lambda *a: detached.append(a))
    script.load()
    print(f'Menu attached to {PKG} {version} pid={pid} via port {port}.', flush=True)
    print('Tap the green MOD button to open it; drag to move. Ctrl+C to remove.', flush=True)

    # Optional scripted control: drop a JSON file next to this script and the
    # loop below executes it, appending the result to a session log.
    control = ROOT / 'control.json'
    control.unlink(missing_ok=True)
    log = ROOT / ('session_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S') + '.jsonl')
    started = time.monotonic()
    try:
        while not detached and (not args.duration or time.monotonic() - started < args.duration):
            if control.exists():
                command = json.loads(control.read_text(encoding='utf-8-sig'))
                control.unlink()
                if command.get('action') == 'stop':
                    break
                try:
                    if command.get('action') == 'layout':
                        result = script.exports_sync.layout()
                    else:
                        result = script.exports_sync.run(command.get('action', 'read'),
                                                         int(command.get('amount', 0)))
                except Exception as exc:
                    result = {'error': str(exc)}
                item = {'time': datetime.datetime.now().isoformat(),
                        'command': command, 'result': result}
                with log.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(item, ensure_ascii=False) + '\n')
                print(json.dumps(item, ensure_ascii=False), flush=True)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if not detached:
            try:
                script.exports_sync.dispose()
            except Exception:
                pass
            session.detach()
        print('Menu detached.', flush=True)


if __name__ == '__main__':
    main()
