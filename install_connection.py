#!/usr/bin/python3
"""Install only the on-demand CUPS backend; no background job or timer."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

SOURCE = Path(__file__).resolve().parent
ROOT = Path('/Library/Printers/BrotherAutoConnection')
CACHE = Path('/Library/Caches/BrotherAutoConnection')
BACKEND = Path('/usr/libexec/cups/backend/brotherauto')
LABEL = 'local.brother-hl1210w.connection'


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=120,
                          env=dict(os.environ, LC_ALL='C')).stdout.strip()


def build_native():
    config = json.loads((SOURCE / 'printer.json').read_text())
    serial = config.get('serial')
    if not isinstance(serial, str) or not re.fullmatch(r'[A-Za-z0-9]+', serial):
        raise ValueError('printer.json requires an alphanumeric serial.')
    seed = config.get('seed_ip', '')
    if seed != '':
        try:
            if not isinstance(seed, str):
                raise ValueError
            seed = str(ipaddress.IPv4Address(seed))
        except ValueError:
            raise ValueError('seed_ip in printer.json must be an IPv4 address or empty.') from None

    # Supply the local settings through stdin without writing a generated header.
    source = (f'#define PRINTER_SERIAL "{serial}"\n'
              f'#define PRINTER_SEED_IP "{seed}"\n'
              '#line 1 "brother_native.c"\n'
              + (SOURCE / 'brother_native.c').read_text())
    native = SOURCE / 'brotherauto-native'
    subprocess.run(
        ['/usr/bin/xcrun', 'clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
         '-Wno-deprecated-declarations', '-O2', '-x', 'c', '-', '-lcups', '-o', str(native)],
        input=source, text=True, check=True, timeout=120,
    )
    print(run([str(native), '--self-test']))


def verify_scheduler(queue):
    """Submit only a connection-check job; native backend sends no print data."""
    job_id = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', prefix='brother-connection-check-', suffix='.txt',
                                         dir='/private/tmp') as document:
            document.write('Brother connection diagnostic: no print data will be sent.\n')
            document.flush()
            submitted = run(['/usr/bin/lp', '-d', queue, '-t', 'Brother connection check (no paper)',
                             '-o', 'raw', '-o', 'brother-connection-check-only=true', document.name])
        match = re.search(re.escape(queue) + r'-(\d+)', submitted)
        if not match:
            raise RuntimeError('Could not identify the diagnostic job: ' + submitted)
        job_id = match.group(1)
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            try:
                record = json.loads((CACHE / 'last-seen.json').read_text())
            except (OSError, ValueError):
                record = {}
            if str(record.get('job_id')) == job_id and record.get('mode') == 'connection-check-only':
                # Let CUPS reap the completed backend before final readback.
                for _ in range(20):
                    pending = run(['/usr/bin/lpstat', '-W', 'not-completed', '-o', queue])
                    if queue + '-' + job_id not in pending:
                        print('CUPS diagnostic job ' + job_id + ' passed; no print data sent.')
                        return record
                    time.sleep(0.5)
            time.sleep(0.5)
        raise RuntimeError('CUPS connection-check job failed or timed out; original queue URI will be restored.')
    finally:
        if job_id:
            # Only this synthetic job; never cancel any user job.
            subprocess.run(['/usr/bin/cancel', queue + '-' + job_id], capture_output=True)


def install_system():
    if os.geteuid() != 0:
        raise RuntimeError('System installation requires administrator rights. Use sudo with --install-system.')
    config = json.loads((SOURCE / 'printer.json').read_text())
    queue = config['queue']
    old_uri = run(['/usr/bin/lpstat', '-v', queue]).split(': ', 1)[1]
    if run(['/usr/bin/lpstat', '-W', 'not-completed', '-o', queue]):
        raise RuntimeError('The Brother queue has pending jobs. Finish or remove them before switching transports.')
    ppd = Path('/private/etc/cups/ppd') / (queue + '.ppd')
    original_ppd = ppd.read_bytes()
    if b'rastertobrother1210' not in original_ppd:
        raise RuntimeError('The queue does not have the expected HL-1210W driver.')
    for directory in (ROOT, CACHE):
        if directory.is_symlink():
            raise RuntimeError('Refusing a symlink installation directory: ' + str(directory))
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, 0, 0)
    ROOT.chmod(0o755)
    CACHE.chmod(0o700)
    native = SOURCE / 'brotherauto-native'
    if not native.is_file() or run([str(native), '--identity']) != config['serial']:
        raise RuntimeError('Run install_connection.py --build-native before installing.')
    run([str(native), '--self-test'])
    target = ROOT / 'brotherauto-native'
    if BACKEND.exists() or BACKEND.is_symlink():
        if not BACKEND.is_symlink() or BACKEND.resolve() not in (target, ROOT / 'brother_backend.py'):
            raise RuntimeError('An unrelated brotherauto backend already exists; leaving it untouched.')
    backup = ROOT / 'installation-backup.json'
    if not backup.exists():
        backup.write_text(json.dumps({'queue': queue, 'uri': old_uri,
                                     'ppd_sha256': hashlib.sha256(original_ppd).hexdigest()}, indent=2) + '\n')
        backup.chmod(0o600)
        (ROOT / 'original.ppd').write_bytes(original_ppd)
        (ROOT / 'original.ppd').chmod(0o600)
    for name in ('brotherauto-native', 'printer.json'):
        destination = ROOT / name
        if destination.is_symlink():
            raise RuntimeError('Refusing a symlink destination: ' + str(destination))
        shutil.copyfile(SOURCE / name, destination)
        os.chown(destination, 0, 0)
        destination.chmod(0o500 if name == 'brotherauto-native' else 0o600)
    print(run([str(target), '--diagnose']))
    if BACKEND.is_symlink() and BACKEND.resolve() != target:
        replacement = BACKEND.with_name('brotherauto-native-link.tmp')
        if replacement.exists() or replacement.is_symlink():
            raise RuntimeError('Temporary backend link already exists; refusing to overwrite it.')
        replacement.symlink_to(target)
        os.replace(replacement, BACKEND)
    elif not BACKEND.is_symlink():
        BACKEND.symlink_to(target)
    advertised = run(['/usr/sbin/lpinfo', '--include-schemes', 'brotherauto', '-v'])
    if not any(line.startswith('network brotherauto') for line in advertised.splitlines()):
        raise RuntimeError('CUPS could not start the backend in its sandbox; queue URI left unchanged.')
    desired = 'brotherauto://' + config['serial']
    try:
        run(['/usr/sbin/lpadmin', '-p', queue, '-v', desired])
        actual = run(['/usr/bin/lpstat', '-v', queue]).split(': ', 1)[1]
        if actual != desired or ppd.read_bytes() != original_ppd:
            raise RuntimeError('Queue readback or driver-preservation verification failed.')
        verify_scheduler(queue)
    except Exception:
        run(['/usr/sbin/lpadmin', '-p', queue, '-v', old_uri])
        raise
    print('Installed on-demand backend; queue URI: ' + desired)
    print('Driver preserved; CUPS connection-only job passed; no LaunchAgent or timer installed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--build-native', action='store_true', help='Build and test using local printer.json')
    mode.add_argument('--install-system', action='store_true')
    mode.add_argument('--restore-uri', action='store_true')
    args = parser.parse_args()
    if args.build_native:
        build_native()
        return
    if args.restore_uri:
        if os.geteuid() != 0:
            raise RuntimeError('Use sudo to restore the original queue URI.')
        backup = json.loads((ROOT / 'installation-backup.json').read_text())
        run(['/usr/sbin/lpadmin', '-p', backup['queue'], '-v', backup['uri']])
        print('Restored queue URI: ' + backup['uri'])
        return
    if args.install_system:
        install_system()
        return
    if os.geteuid() == 0:
        raise RuntimeError('Run the normal installer as your login user; use --install-system only with sudo.')
    old_plist = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    if old_plist.exists():
        job = 'gui/' + str(os.getuid()) + '/' + LABEL
        existing = subprocess.run(['/bin/launchctl', 'print', job], capture_output=True)
        if existing.returncode == 0:
            run(['/bin/launchctl', 'bootout', job])
        disabled = old_plist.with_suffix('.plist.disabled')
        if disabled.exists():
            raise RuntimeError('A disabled-job backup already exists: ' + str(disabled))
        old_plist.rename(disabled)
    print(run(['/usr/bin/sudo', '-n', '/usr/bin/python3', '-I', str(Path(__file__).resolve()), '--install-system']))
    queue = json.loads((SOURCE / 'printer.json').read_text())['queue']
    support = Path.home() / 'Library/Application Support/BrotherPrinterConnection'
    support.mkdir(parents=True, exist_ok=True)
    options = Path.home() / '.cups/lpoptions'
    default_backup = support / 'default-printer-backup.json'
    if not default_backup.exists():
        default_backup.write_text(json.dumps({'default': run(['/usr/bin/lpstat', '-d']),
                                             'lpoptions': options.read_text() if options.exists() else None}, indent=2) + '\n')
        default_backup.chmod(0o600)
    run(['/usr/bin/lpoptions', '-d', queue])
    print(run(['/usr/bin/lpstat', '-d']))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('Installation failed: ' + str(error), file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr, file=sys.stderr)
        sys.exit(1)
