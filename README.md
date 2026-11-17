# Brother HL-1210W: automatic IP rediscovery on macOS

If the Brother HL-1210W doesn't appear through Bonjour, you can add it to macOS
using its IP address. If the printer later gets a new address, that queue keeps
using the old one. The printer may appear offline until you update the queue
or re-add the printer.

This project automates IP rediscovery so you can keep printing from the same
queue after the printer's address changes.

During setup, the installer points the existing CUPS queue to
`brotherauto://<PRINTER_SERIAL>` and keeps the installed Brother driver. When CUPS
attempts a print job, the backend verifies the printer by model and serial
number, rediscovers it if its IP has changed, and passes the current IP to
macOS's native IPP backend for that job. The queue keeps the same name and
device URI across IP changes.

The backend runs on demand and exits after each attempt. No login job, timer,
or idle process is installed. CUPS can retry a queued job if the printer is
offline. Select this queue once in apps that remember an older printer
selection.

## Install

### Prerequisites

- A Mac with Xcode or Apple's Command Line Tools installed. Run
  `xcode-select --install` if you need the Command Line Tools.
- Administrator access on the Mac to run the system installer with `sudo`.
- An existing macOS print queue using the Brother HL-1210W driver, with
  no pending print jobs. Select your macOS version on the
  [Brother HL-1210W downloads page](https://support.brother.com/g/b/downloadtop.aspx?c=th&lang=en&prod=hl1210w_eu_as).
  Install the Printer Driver if listed, or use the Full Software Package for
  your macOS version.
- A powered-on Brother HL-1210W on the same private IPv4 subnet as the Mac.
  The Mac must be able to reach the printer's HTTP maintenance page on TCP
  port 80 and its IPP service on TCP port 631.

### Create a print queue if needed

If you already have a working HL-1210W queue using the Brother driver, skip to
[Build and install](#build-and-install). Otherwise, after installing the
driver, paste this into Terminal and enter the printer's current IP address when
prompted.

```sh
printf 'Printer IP address: '
IFS= read -r printer_ip
sudo /usr/sbin/lpadmin -p Brother_HL1210W -E \
  -v "ipp://${printer_ip}/" \
  -m 'Library/Printers/PPDs/Contents/Resources/Brother HL-1210W series CUPS.gz'
```

If macOS reports that the model is unavailable, run
`/usr/sbin/lpinfo -m | grep -i '1210w'` to check whether macOS registered the
Brother driver.

Print a one-page document to the new queue and wait for it to finish. Then check
that the queue exists and has no pending jobs.

```sh
/usr/bin/lpstat -v Brother_HL1210W
/usr/bin/lpstat -W not-completed -o Brother_HL1210W
```

The second command should return no output. Use `Brother_HL1210W` as the queue
name in `printer.json`.

### Build and install

If you need a local configuration, copy the example to `printer.json`.

```sh
cp -n printer.example.json printer.json
```

Replace the sample values with your existing CUPS queue name (shown by
`/usr/bin/lpstat -v`), printer serial number, and MAC address. Use the printer's
current LAN IP for `seed_ip` or leave it empty to use discovery.

Build and test the native backend from this project folder. The build reads
`serial` and `seed_ip` from the local `printer.json` and writes the ignored
`brotherauto-native` executable. Rebuild if either setting changes.

```sh
/usr/bin/python3 -I install_connection.py --build-native
```

Run the following from this project folder. Replace `<QUEUE_NAME>` with the
queue name in `printer.json`.

```sh
sudo /usr/bin/python3 -I install_connection.py --install-system
/usr/bin/lpoptions -d '<QUEUE_NAME>'
```

The installer copies a root-owned native executable and configuration to
`/Library/Printers/BrotherAutoConnection`, creates the cache directory
`/Library/Caches/BrotherAutoConnection` for the last verified address, registers
`/usr/libexec/cups/backend/brotherauto`, and changes the existing queue URI.
It preserves and checks the driver, backs up the original URI and PPD, and
refuses to change a queue with pending jobs. The backend is native code linked
only to macOS's system CUPS and C libraries, with no Python runtime.
The installer requires successful discovery through CUPS itself before switching
the queue URI, then submits a connection-only job that sends no print data. It
verifies the resulting job ID in the backend cache and waits for completion.
Any failure restores the previous URI and cancels only the synthetic test job.

`install_connection.py` without arguments attempts the same installation via
noninteractive sudo and sets the current user's default. It also disables the
earlier timer if one was installed.

## Check without printing a page

Before installation, this scans the subnet and verifies the printer identity:

```sh
./brotherauto-native --diagnose --scan
```

After installation, check the installed copy with:

```sh
sudo /Library/Printers/BrotherAutoConnection/brotherauto-native --diagnose
```

Installation automatically performs a CUPS backend check using a small raw test
document with `-o brother-connection-check-only=true`. That backend option verifies the
connection and deliberately sends no print data to the printer. The cache's
`last-seen.json` records the job ID and `connection-check-only` mode to verify
that CUPS invoked it.

## Undo the queue change

```sh
sudo /usr/bin/python3 -I install_connection.py --restore-uri
```

This restores the backed-up IPP URI. To restore the earlier per-user default,
replace `<PREVIOUS_DEFAULT_QUEUE>` with the name of that queue.

```sh
/usr/bin/lpoptions -d '<PREVIOUS_DEFAULT_QUEUE>'
```

## Behavior and limits

The backend first verifies the cached or original IP by reading the printer's
maintenance page and checking its model and serial. If that fails, it scans
active physical `en` interfaces' private IPv4 subnets: at most 1024 addresses
per subnet and 2048 total, with 24 workers and short socket timeouts. It does
not scan VPN interfaces or public addresses. No print data is sent until the
identity check succeeds. If discovery fails, it returns CUPS's retry status;
after handing off to IPP, delivery and cancellation belong to the native sender.

The backend cannot reconnect the printer to Wi-Fi or turn it on. Serial matching
prevents accidental delivery to another ordinary device. A device impersonating
the printer can still pass the HTTP identity check.

The build checks printer identity matching, HTTP response framing, private
address validation, and parsing of the connection-only option. The installer's
connection-only job verifies that CUPS can invoke the backend. The raw check
does not exercise the Brother raster filter or physical printing.

`brother_connection.py` remains a manual queue-repair tool. `brother_backend.py`
and `test_backend.py` preserve the earlier Python prototype and its tests; they
are not installed or invoked by the native backend. No periodic job is installed.

Reference: [CUPS backend interface](https://www.cups.org/doc/man-backend.html)
and [macOS CUPS sandbox and file permissions](https://www.cups.org/doc/api-filter.html).

## License

This project is licensed under the [MIT License](LICENSE).
