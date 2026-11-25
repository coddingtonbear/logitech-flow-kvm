![PeerKVM for Logitech](docs/assets/img/peerkvm.jpeg)

# PeerKVM for Logitech

[![CI](https://github.com/coddingtonbear/logitech-flow-kvm/actions/workflows/ci.yml/badge.svg)](https://github.com/coddingtonbear/logitech-flow-kvm/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> [!IMPORTANT]
> **PeerKVM for Logitech is an independent, unofficial project. It is not
> affiliated with, endorsed by, or supported by Logitech.** This project is a
> derivative of Adam Coddington's original
> [`logitech-flow-kvm`](https://github.com/coddingtonbear/logitech-flow-kvm),
> whose MIT-licensed history and attribution are preserved.

Logitech's "Flow" lets your mouse and keyboard roam across multiple paired hosts with a single keypress -- but Logitech only supports it between Windows and macOS. `logitech-flow-kvm` brings that same one-keypress host switching to Linux, and keeps your clipboard in sync across hosts while it's at it.

It runs the same peer process on every host. When a leader device (typically a
keyboard) connects to one peer, that peer announces the destination host over
an authenticated, encrypted LAN broadcast. Whichever peer still holds a
follower device can then switch it to the same host.

Its HID++ implementation builds on the protocol knowledge documented by the [Solaar](https://github.com/pwr-Solaar/Solaar) project, but where Solaar is a general device manager, this tool focuses specifically on Flow-style host switching.

If you'd rather see what it does before reading how to set it up, there's a project page at **<https://coddingtonbear.github.io/logitech-flow-kvm/>**.

## Contents

- [Features](#features)
- [Installation](#installation)
- [Basic Use](#basic-use)
- [How to](#how-to)
- [Logs](#logs)
- [Credits](#credits)

# Features

- Automatically switches all devices from one host to another when just one of your devices switches hosts.  This is particularly useful if you are using a device like the MX Keys Mini which includes buttons that can be used for switching hosts with a single keypress.
- Securely keeps clipboards in sync when switching between hosts. Now you can copy/paste from one host to another without thinking anything about it.
- Optionally switches one or more DDC/CI monitor inputs with `ddcutil` when the
  leader keyboard arrives on a host.
- Symmetric peer discovery over authenticated, encrypted LAN broadcasts -- no
  server role, certificates, or pairing-code workflow.
- A live TUI for setup and status, with clear waiting/error states when a
  Bluetooth device is currently attached to another host.
- A rotating log file kept on disk regardless of how it's run, so you can always see what happened after the fact.

# Installation

Requires Python 3.10 or later.

To control Bluetooth-connected devices, install the included udev rule and
then reconnect the device (or reboot):

```
sudo install -m 0644 rules.d/42-logitech-flow-kvm.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
```

The packaged rule grants access to the active desktop session. If `flow-node`
will run exclusively through SSH or as a service without an active desktop
session, configure a dedicated group with `MODE="0660"` in the local udev rule
and run the service as a member of that group.

Run directly from a clone:

```
git clone https://github.com/coddingtonbear/logitech-flow-kvm.git
cd logitech-flow-kvm
uv run --frozen logitech-flow-kvm flow-node --secret 'choose-a-shared-secret'
```

The first `uv run` creates an isolated environment from `uv.lock`; it does not
install the project into the system Python.

All peers must be on a LAN that permits IPv4 broadcast traffic on UDP port
`24801`. Host firewalls and Wi-Fi client isolation must allow that traffic.

## Optional monitor input switching

Install `ddcutil` and make sure the user running `flow-node` can access the
relevant `/dev/i2c-*` devices. Distribution packages commonly configure an
`i2c` group; after adding your user to it, log out and back in. Verify the setup
before starting Flow:

```
ddcutil detect --brief
```

See the [ddcutil I²C permissions documentation](https://www.ddcutil.com/i2c_permissions/)
if `detect` reports `EACCES`. Monitors must have DDC/CI enabled in their on-screen
settings.

# Basic Use

Run the same command on every computer, using the same shared secret:

```
logitech-flow-kvm flow-node --secret 'choose-a-shared-secret'
```

There is no server, client, certificate, or pairing code. Nodes discover one
another using authenticated, encrypted LAN broadcasts and identify one another
by hostname and source IP. The secret is kept only in process memory and is
never written to the configuration file.

On every interactive start, the setup screen lets you choose this computer's
host number, leader keyboard, followers, and clipboard preference. Only these
non-secret choices are saved. If a Bluetooth device is not currently shown,
switch it to this computer and choose **Refresh devices**.

Configure each computer as follows:

1. Start `flow-node` in a terminal.
2. Enter the host number printed on the Logitech device's host-selection key
   (the Easy-Switch channel, 1-3). The keyboard and mouse must use the same
   channel number for this computer.
3. Select the keyboard whose host key will initiate switching as the leader.
4. Select the mouse and any other devices that should follow it.
5. For each detected monitor that should follow this host, select one of the
   advertised Feature `0x60` input values. Leave it blank to disable switching
   for that monitor.
6. Choose **Save and start**.

The setup screen runs `ddcutil detect`, then reads `ddcutil capabilities` for
each monitor. It displays the reported MCCS version and the values advertised
for Feature `0x60` (Input Source), for example `0x11: HDMI-1` or
`0x0f: DisplayPort-1`. The selected value is local to this node: choose the
input physically connected to that computer.

Some monitors publish incomplete or incorrect capability strings. Flow uses
the monitor-reported values as requested and reports any failing `setvcp`
operation in the runtime TUI instead of silently trying another input.

When configuring a Bluetooth device for the first time, it must be connected to
that computer long enough to appear in the setup screen. Once saved, the entry
remains visible as **not currently detected** while the device is connected to
another host. That state is expected and does not prevent startup.

When the leader appears on a node, that node broadcasts its host number. Every
other node then switches only the configured followers that are connected to
it at that moment. A follower which is currently on another host is therefore
normal and never causes startup to fail.

A device is identified by its Logitech unit ID (for example `694821BA`), the
same 8-digit ID whether it is reached through a Bolt/Unifying receiver or
directly over Bluetooth. Each host may therefore connect to the same keyboard
and mouse differently -- for example through a Bolt receiver on one computer
and Bluetooth on another -- and switching works between them. Because the
unit ID does not change when a Bluetooth device is re-paired, a saved
configuration also survives re-pairing.

Configurations saved by older versions name Bluetooth devices by their
Bluetooth address (for example `D7:C3:34:36:65:E1`). Those keep working: the
address is still recognised as the same device. Re-running setup replaces it
with the unit ID. If a Bluetooth device does not answer the unit ID request,
it falls back to its Bluetooth address.

Nodes exchange their detected device inventories and automatically correlate
devices using product, kind, and the longest unambiguous normalized ID prefix.
No manual ID mapping is required.

## What happens during a switch

Suppose the keyboard and mouse are connected to host 1 and the keyboard's host
2 button is pressed:

1. Host 1 observes the keyboard disconnect. It keeps controlling the mouse and
   broadcasts the clipboard, if enabled.
2. The keyboard connects to host 2. Dynamic udev monitoring detects the new
   `hidraw` device without restarting `flow-node`.
3. Host 2 broadcasts that the leader is now on host 2.
4. Host 2 sends `ddcutil setvcp 60 VALUE` to every monitor configured on that
   node.
5. Host 1 receives the announcement and instructs the mouse it still holds to
   switch to host 2.

Missing devices are never treated as a startup error: only the peer that can
currently reach a follower attempts to switch it.

## Configuration and secrets

Non-secret settings are stored in the platform configuration directory. On
Linux the file is:

```
~/.config/Logitech Flow KVM/node.json
```

The shared secret is never stored there. It must be supplied on every start and
is intentionally allowed to appear in shell history and the process list:

```
logitech-flow-kvm flow-node --secret 'the-same-value-on-every-peer'
```

Starting without `--secret` fails immediately with CLI usage help. Packets from
a peer using a different secret are ignored and reported in the TUI.

## Non-interactive startup

Run `flow-node` interactively at least once on each computer to save its node
configuration. Later launches from a service or another non-interactive context
reuse that configuration but still require the secret:

```
logitech-flow-kvm flow-node --secret 'the-same-value-on-every-peer'
```

If no saved configuration exists, the process exits with an instruction to run
the interactive setup instead of guessing defaults.

Clipboard sharing requires access to the local desktop clipboard provider.
Start `flow-node` from the local graphical session, or explicitly propagate
that session's environment when starting it from SSH or a service. Depending
on the desktop, this includes variables such as `DISPLAY` and `XAUTHORITY` for
X11, or `WAYLAND_DISPLAY` and `XDG_RUNTIME_DIR` for Wayland (and, where
applicable, the session D-Bus environment). If these variables and their
associated permissions are not available, device switching still works but
clipboard synchronization cannot read or update the clipboard.

# How to

## Finding available devices

You can get a list of available devices using the `list-devices` subcommand:

```
> logitech-flow-kvm list-devices

Finding devices... ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 0:00:00
┏━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┓
┃ ID       ┃ Product ┃ Name           ┃ Path           ┃
┡━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━┩
│ 08F5F681 │ B369    │ MX Keys Mini   │ /dev/hidraw4:1 │
│ F262458A │ 406A    │ MX Anywhere 2S │ /dev/hidraw5:1 │
└──────────┴─────────┴────────────────┴────────────────┘
```

## Running a command when a device connects or disconnects

You can see when a device connects or disconnects from the receiver using the following example:

```
> logitech-flow-kvm watch /dev/hidraw4:1
```

If you'd like to run a command when a device connects or disconnects, use the
`--on-disconnect-execute` or `--on-connect-execute` arguments.

## Troubleshooting

The TUI reports common problems directly:

- **No peers discovered**: start `flow-node` on another host with the same
  secret, then check UDP port `24801`, the firewall, and Wi-Fi client isolation.
- **Shared secrets differ**: restart every peer with exactly the same
  `--secret` value.
- **Host number is also used by another node**: reopen setup and choose the
  host number assigned to this computer on the Logitech devices.
- **Device discovery failed / permission denied**: reinstall the udev rule,
  reload the rules, and reconnect the device or reboot.
- **Not currently detected**: the Bluetooth device is on another host. This is
  expected after its local ID has been saved.
- **ddcutil EACCES**: grant the runtime user access to the relevant
  `/dev/i2c-*` devices, then log out and back in.
- **Feature 0x60 has no advertised values**: enable DDC/CI in the monitor menu;
  if it remains absent, that monitor cannot be configured automatically.

# Logs

`flow-node` writes everything it logs to a rotating log file, in addition to
the interactive display's scrolling log (or plain stdout when run
non-interactively). The log file lives in your platform's standard per-app log
directory; on Linux, that's:

```
~/.local/state/Logitech Flow KVM/log/logitech-flow-kvm.log
```

It's capped at 5MB, rotating through up to 5 backups (`logitech-flow-kvm.log.1`, `.2`, ...) before the oldest is discarded, so it won't grow without bound.

# Credits

PeerKVM for Logitech is derived from Adam Coddington's original
[`logitech-flow-kvm`](https://github.com/coddingtonbear/logitech-flow-kvm).
We gratefully preserve its MIT-licensed history and attribution. This fork has
since evolved into a substantially different peer-to-peer product, but it
would not exist without that original work.

This project is independent and unofficial. It is not affiliated with,
endorsed by, or supported by Logitech.

The HID++ implementation was also developed with reference to the protocol
knowledge documented by the contributors to
[Solaar](https://github.com/pwr-Solaar/Solaar).
