#!/usr/bin/python3
"""Find one Brother printer by serial number and repair its existing CUPS URI."""
import argparse
import concurrent.futures
import fcntl
import html.parser
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone


def command(args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=15).stdout.strip()


class Fields(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tag = None
        self.parts = []
        self.key = None
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag in ('dt', 'dd'):
            self.tag, self.parts = tag, []

    def handle_data(self, data):
        if self.tag:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == self.tag:
            value = ' '.join(''.join(self.parts).split())
            if tag == 'dt':
                self.key = value.lower()
            elif self.key:
                self.values[self.key] = value
            self.tag = None


def identify(address, serial):
    connection = None
    try:
        with socket.create_connection((address, 631), timeout=0.6):
            pass
        connection = http.client.HTTPConnection(address, 80, timeout=2.5)
        connection.request('GET', '/general/information.html?kind=item', headers={'Connection': 'close'})
        response = connection.getresponse()
        if response.status != 200:
            return None
        body = response.read(262145)
        if len(body) > 262144:
            return None
        fields = Fields()
        fields.feed(body.decode('iso-8859-1'))
        model = fields.values.get('model name', '')
        actual_serial = fields.values.get('serial no.', '')
        if actual_serial == serial and re.fullmatch(r'Brother HL-1210W(?: series)?', model):
            return {'ip': address, 'model': model, 'serial': actual_serial}
    except (OSError, http.client.HTTPException, ValueError):
        return None
    finally:
        if connection:
            connection.close()
    return None


def network():
    route = command(['/sbin/route', '-n', 'get', 'default'])
    match = re.search(r'^\s*interface:\s*(en\d+)\s*$', route, re.M)
    if not match:
        raise RuntimeError('No physical en-interface on the default route; skipping network discovery.')
    interface = match.group(1)
    address = command(['/usr/sbin/ipconfig', 'getifaddr', interface])
    mask = command(['/usr/sbin/ipconfig', 'getoption', interface, 'subnet_mask'])
    subnet = ipaddress.IPv4Network(address + '/' + mask, strict=False)
    # Never scan public networks, VPN ranges, or unexpectedly large LANs.
    private_ranges = [ipaddress.IPv4Network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    if not any(subnet.subnet_of(private) for private in private_ranges) or subnet.num_addresses > 1024:
        raise RuntimeError('Discovery is restricted to a private local subnet of at most 1024 addresses.')
    return subnet, address


def queue_uri(queue):
    output = command(['/usr/bin/lpstat', '-v', queue])
    prefix = 'device for ' + queue + ': '
    if not output.startswith(prefix):
        raise RuntimeError('Could not read the configured printer URI.')
    return output[len(prefix):]


def discover(candidates, serial):
    candidates = list(dict.fromkeys(candidates))
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
        results = executor.map(lambda address: identify(address, serial), candidates)
        for result in results:
            if result:
                return result
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('printer.json'))
    parser.add_argument('--state-dir', type=Path, default=Path(__file__).with_name('.state'))
    parser.add_argument('--apply', action='store_true', help='Update the existing queue after exact identity verification')
    parser.add_argument('--quiet', action='store_true', help='Log only updates and errors')
    parser.add_argument('--scan', action='store_true', help='Exercise full subnet discovery instead of preferred-address checks')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    queue, serial = config['queue'], config['serial']
    if not re.fullmatch(r'[A-Za-z0-9_-]+', queue) or not re.fullmatch(r'[A-Za-z0-9]+', serial):
        raise RuntimeError('Invalid queue or serial configuration.')
    args.state_dir.mkdir(parents=True, exist_ok=True)
    with (args.state_dir / 'lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        subnet, local_address = network()
        uri = queue_uri(queue)  # Refuse to create a missing queue or substitute a driver.
        state_path = args.state_dir / 'last-seen.json'
        preferred = [config.get('seed_ip', '')]
        try:
            preferred.insert(0, json.loads(state_path.read_text())['ip'])
        except (OSError, ValueError, KeyError):
            pass
        current = re.fullmatch(r'ipp://([0-9.]+)(?::631)?/.*', uri)
        if current:
            preferred.insert(0, current.group(1))
        arp = command(['/usr/sbin/arp', '-an'])
        mac = config.get('mac', '').lower()
        for line in arp.splitlines():
            if mac and (' at ' + mac + ' ') in line.lower():
                match = re.search(r'\(([0-9.]+)\)', line)
                if match:
                    preferred.insert(0, match.group(1))
        preferred = [ip for ip in preferred if ip and ipaddress.IPv4Address(ip) in subnet]
        found = None if args.scan else discover(preferred, serial)
        if not found:
            found = discover([str(ip) for ip in subnet.hosts() if str(ip) != local_address], serial)
        if not found:
            raise RuntimeError('Exact Brother printer not found on ' + str(subnet) + '; queue left unchanged.')
        desired = 'ipp://' + found['ip'] + '/'
        changed = uri != desired
        if args.apply and changed:
            command(['/usr/sbin/lpadmin', '-p', queue, '-v', desired])
            if queue_uri(queue) != desired:
                raise RuntimeError('Printer URI update did not pass readback verification.')
        # Save discovery results even in diagnostic mode; --apply alone changes CUPS.
        found['observed_at'] = datetime.now(timezone.utc).isoformat()
        temporary = state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(found, indent=2) + '\n')
        os.replace(temporary, state_path)
        result = dict(found, queue=queue, previous_uri=uri, desired_uri=desired,
                      action=('updated' if args.apply else 'would-update') if changed else 'unchanged')
        if not args.quiet or changed:
            print(json.dumps(result), flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(datetime.now(timezone.utc).isoformat() + ' ERROR: ' + str(error), file=sys.stderr, flush=True)
        sys.exit(1)
