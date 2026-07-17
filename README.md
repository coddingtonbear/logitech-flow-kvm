![header-image](http://coddingtonbear-public.s3.amazonaws.com/github/logitech-flow-kvm/mx_keys_buttons.jpg)

Quickly switch between paired devices when using a Logitech mouse and keyboard that supports connecting to multiple devices.

Do you use Logitech devices that support multiple hosts and find it a little frustrating how tedious it is to switch between hosts when you're using Linux due to Logitech's "Flow" features being unsupported there?  Good news: you can have "Flow"-like features on Linux now, too.

This utility works by monitoring one of your attached Logitech devices to see what host it is currently connected to, and then, if that host changes, instructing other connected devices to switch to the same host.

# Features

- Automatically switches all devices from one host to another when just one of your devices switches hosts.  This is particularly useful if you are using a a device like the MX Keys Mini which includes buttons that can be used for switching hosts with a single keypress.
- Securely keeps clipboards in sync when switching between hosts. Now you can copy/paste from one host to another without thinking anything about it.

# Installation

Requires Python 3.10 or later.

```
pip install logitech-flow-kvm
```

You can also install the in-development version with:

```
pip install https://github.com/coddingtonbear/logitech-flow-kvm/archive/main.zip
```

# Basic Use

One of your computers will serve as the "server" for managing this, and all others will be "clients".

## Server

On the computer you've decided to use as your server, get the ID of the device you'd like to serve as your "leader", and the IDs of other devices you'd like to use as "followers".  The leader device -- I recommend using your keyboard -- will be the one we watch.  The followers will just be told which host to connect to if your leader device's host changes.

```
> logitech-flow-kvm list-devices

┏━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┓
┃ ID       ┃ Product ┃ Name           ┃ Path           ┃
┡━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━┩
│ 08F5F681 │ B369    │ MX Keys Mini   │ /dev/hidraw4:1 │
│ F262458A │ 406A    │ MX Anywhere 2S │ /dev/hidraw5:1 │
└──────────┴─────────┴────────────────┴────────────────┘
```

In this example, I'll be using my keyboard (`08F5F681`) as my "leader", and my mouse (`F262458A`) as a follower. Note that we're using the device's ID (its serial number) here, not its path -- paths depend on enumeration order and USB topology, which can differ from machine to machine or even across reboots of the same machine, while the serial number is a stable, permanent identifier for the physical device.

You can run a command like this:

```
> logitech-flow-kvm flow-server 1 08F5F681 F262458A
```

Note that the `1` in the above command immediately after `flow-server` indicates that the host number of your server is `1`.  This should match the host number you've paired your mouse and keyboard with your device as (i.e. when your mouse or keyboard is connected to this computer, the light for `1` is lit on the keyboard or mouse's device selector).

When run in a terminal, `flow-server` shows an interactive display: a status panel at the top (bind address/port, leader and follower connection state, and which clients are currently connected) and a scrolling log underneath. The bind address is also logged as a plain line when the server starts, and looks something like this:

![flow-server](http://coddingtonbear-public.s3.amazonaws.com/github/logitech-flow-kvm/flow-server.png)

> [!info]
> If you'd rather connect using a hostname (e.g. an mDNS `.local`/`.lan` name) instead of an IP address, pass it with `--hostname`/`-H` (repeatable) so it gets included in the server's certificate -- otherwise clients connecting by that hostname will fail TLS verification, since the certificate only lists the server's IP addresses by default:
> ```
> logitech-flow-kvm flow-server --hostname coddingtonbear-t14.lan 1 08F5F681 F262458A
> ```

## Client

![flow-client](http://coddingtonbear-public.s3.amazonaws.com/github/logitech-flow-kvm/flow-client.png)

On the other computers you'd like to use this feature with, you can run the following command:

```
> logitech-flow-kvm flow-client 2 10.224.224.120
```

...where `10.224.224.120` is the IP of the server you ran above. Note that the `2` above after `flow-client` indicates the host number your devices have paired with your computer under.  See "Server" above for details.

![flow-server-pairing](http://coddingtonbear-public.s3.amazonaws.com/github/logitech-flow-kvm/flow-server-pairing.png)

If this is your first time connecting to this server, you will be walked through a brief pairing process for establishing a secure connection between your server and client instances: the client displays a pairing code, and you enter it on the server -- as a popup in `flow-server`'s display if it's running in a terminal, or as an ordinary prompt if it's running non-interactively.  Afterward, the client will connect, gather some configuration options from the server, and will instruct your "follower" devices to change their hosts as necessary in the future.

Like `flow-server`, `flow-client` shows the same kind of interactive display (device/leader status on top, a scrolling log below) when run in a terminal, and falls back to plain logging otherwise.

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

If you'd like to run a command when a device connects or disconnects, use the `--on-disconnect-execute` or `--on-connect-execute` arguments.  See the "Automatically switch your mouse to a different host when your keyboard disconnects" section below for a concrete example of how you might use this.

# Logs

`flow-server` and `flow-client` both write everything they log to a rotating log file, in addition to wherever it's also shown (the interactive display's scrolling log, or plain stdout when running non-interactively) -- so you can always go back and check what happened even if it's scrolled off-screen or you weren't watching the terminal. The log file lives in your platform's standard per-app log directory (via [platformdirs](https://pypi.org/project/platformdirs/)); on Linux, that's:

```
~/.local/state/Logitech Flow KVM/log/logitech-flow-kvm.log
```

It's capped at 5MB, rotating through up to 5 backups (`logitech-flow-kvm.log.1`, `.2`, ...) before the oldest is discarded, so it won't grow without bound.

# Credits

This tool's HID++ implementation was developed with reference to the protocol knowledge documented by the folks working on [Solaar](https://github.com/pwr-Solaar/Solaar).
