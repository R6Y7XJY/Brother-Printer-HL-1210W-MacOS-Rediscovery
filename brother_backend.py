#!/usr/bin/python3 -I
"""CUPS on-demand Brother transport. Root-owned installation only."""
import concurrent.futures
import ctypes
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import sys
from datetime import datetime, timezone

# Installed beside this file; never import from the submitting user's directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from brother_connection import identify

ROOT = Path('/Library/Printers/BrotherAutoConnection')
CACHE = Path('/Library/Caches/BrotherAutoConnection')
IPP = '/usr/libexec/cups/backend/ipp'
RETRY = 6  # CUPS_BACKEND_RETRY: preserve the job for a later scheduler retry.


def local_networks():
    """Read en-interface IPv4/netmask pairs without route/configd subprocesses."""
    class Sockaddr(ctypes.Structure):
        _fields_ = [('length', ctypes.c_uint8), ('family', ctypes.c_uint8), ('data', ctypes.c_ubyte * 14)]

    class Interface(ctypes.Structure):
        pass

    Interface._fields_ = [('next', ctypes.POINTER(Interface)), ('name', ctypes.c_char_p),
                         ('flags', ctypes.c_uint), ('address', ctypes.POINTER(Sockaddr)),
                         ('netmask', ctypes.POINTER(Sockaddr)), ('destination', ctypes.c_void_p),
                         ('data', ctypes.c_void_p)]
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    libc.getifaddrs.argtypes = [ctypes.POINTER(ctypes.POINTER(Interface))]
    libc.getifaddrs.restype = ctypes.c_int
    libc.freeifaddrs.argtypes = [ctypes.POINTER(Interface)]
    head = ctypes.POINTER(Interface)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        raise OSError(ctypes.get_errno(), 'getifaddrs failed')
    private = [ipaddress.IPv4Network(s) for s in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    networks, addresses = set(), set()
    try:
        node = head
        while node:
            item = node.contents
            if (item.name and re.fullmatch(rb'en\d+', item.name) and item.flags & 1
                    and item.address and item.netmask and item.address.contents.family == socket.AF_INET):
                raw = bytes(item.address.contents.data)
                mask = bytes(item.netmask.contents.data)
                address = socket.inet_ntoa(raw[2:6])
                subnet = ipaddress.IPv4Network(address + '/' + socket.inet_ntoa(mask[2:6]), strict=False)
                if subnet.num_addresses <= 1024 and any(subnet.subnet_of(p) for p in private):
                    networks.add(subnet)
                    addresses.add(address)
            node = item.next
    finally:
        libc.freeifaddrs(head)
    # Bound scanning even on hosts with several active adapters.
    if not networks or sum(n.num_addresses for n in networks) > 2048:
        raise RuntimeError('No suitable private physical IPv4 subnet (maximum 2048 addresses total).')
    return sorted(networks, key=str), addresses


def find_printer(config, cache, force_scan=False):
    networks, self_addresses = local_networks()
    def on_lan(address):
        try:
            return any(ipaddress.IPv4Address(address) in subnet for subnet in networks)
        except (ValueError, TypeError):
            return False
    preferred = [config.get('seed_ip')]
    try:
        preferred.insert(0, json.loads((cache / 'last-seen.json').read_text())['ip'])
    except (OSError, ValueError, KeyError):
        pass
    if not force_scan:
        for address in dict.fromkeys(preferred):
            if on_lan(address):
                found = identify(address, config['serial'])
                if found:
                    return found
    candidates = list(dict.fromkeys(str(ip) for subnet in networks for ip in subnet.hosts()
                                   if str(ip) not in self_addresses))
    # Queries only: no print bytes have been read or sent at this stage.
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
        results = executor.map(lambda address: identify(address, config['serial']), candidates)
        for found in results:
            if found:
                return found
    raise RuntimeError('The configured HL-1210W serial number was not found; retaining the print job.')


def save_result(cache, result):
    temporary = cache / 'last-seen.tmp'
    temporary.write_text(json.dumps(result, indent=2) + '\n')
    temporary.chmod(0o600)
    os.replace(temporary, cache / 'last-seen.json')


def main():
    # CUPS discovery is cheap and does not scan or open the printer.
    if len(sys.argv) == 1:
        print('network brotherauto "Brother HL-1210W" "Brother HL-1210W (on-demand discovery)"')
        return 0
    diagnostic = sys.argv[1:] in (['--diagnose'], ['--diagnose', '--scan'])
    if not diagnostic and (len(sys.argv) not in (6, 7) or not sys.argv[1].isdigit()):
        print('ERROR: Invalid CUPS job arguments.', file=sys.stderr)
        return 4
    config_path = ROOT / 'printer.json'
    cache = CACHE
    # Explicit diagnostic mode is safe to run before installation from this workspace.
    if diagnostic and not config_path.exists():
        config_path = Path(__file__).resolve().with_name('printer.json')
        cache = Path(__file__).resolve().with_name('.backend-check')
    config = json.loads(config_path.read_text())
    serial = config['serial']
    if not re.fullmatch(r'[A-Za-z0-9]+', serial):
        raise RuntimeError('Invalid configured serial number.')
    expected_uri = 'brotherauto://' + serial
    if not diagnostic and os.environ.get('DEVICE_URI') != expected_uri:
        raise RuntimeError('This backend accepts only its configured printer identity.')
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (cache / 'discovery.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        found = find_printer(config, cache, '--scan' in sys.argv if diagnostic else False)
        uri = 'ipp://' + found['ip'] + '/'
        check_only = diagnostic or 'brother-connection-check-only=true' in shlex.split(sys.argv[5])
        result = dict(found, observed_at=datetime.now(timezone.utc).isoformat(), resolved_uri=uri,
                      job_id=None if diagnostic else sys.argv[1],
                      mode='connection-check-only' if check_only else 'print')
        save_result(cache, result)
    if check_only:
        print(json.dumps(result), file=sys.stderr)
        print('INFO: Verified Brother connection; diagnostic mode sent no print data.', file=sys.stderr)
        return 0
    print('INFO: Verified Brother HL-1210W at ' + found['ip'] + '; sending job via IPP.', file=sys.stderr, flush=True)
    environment = dict(os.environ, DEVICE_URI=uri)
    # Replace this process: native IPP handles stdin/file data, copies, side channels,
    # cancellation, and delivery status. Never retry a partly sent job ourselves.
    os.execve(IPP, [uri] + sys.argv[1:], environment)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print('WARNING: Brother on-demand discovery: ' + str(error), file=sys.stderr, flush=True)
        sys.exit(RETRY)
