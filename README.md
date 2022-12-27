Quickly switch between paired devices when using a mouse and keyboard that supports Logitech Flow.


# Installation

```
pip install logitech-flow-kvm
```

You can also install the in-development version with:

```

pip install https://github.com/coddingtonbear/logitech-flow-kvm/archive/master.zip

```

# Use

## Finding available devices

You can get a list of available devices using the `list-devices` subcommand:

```
> logitech-flow-kvm list-devices

Finding devices... ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 0:00:00
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ ID             ┃ Product ┃ Name                       ┃ Serial   ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━┩
│ /dev/hidraw4:1 │ B369    │ MX Keys Mini               │ 08F5F681 │
│ /dev/hidraw5:1 │ 4082    │ MX Master 3 Wireless Mouse │ 0F591C09 │
└────────────────┴─────────┴────────────────────────────┴──────────┘
```

## Switching which host a device is connected to

You can change the relevant device to your desired host number using the `switch-to-host` command:

```
> logitech-flow-kvm switch-to-host /dev/hidraw4:1 2
```

The above command will tell the device having the id `/dev/hidraw4:1` to connect to whichever device is paired as its #`2` device.

## Running a command when a device connects or disconnects

You can see when a device connects or disconnects from the receiver using the following example:

```
> logitech-flow-kvm watch /dev/hidraw4:1
```

If you'd like to run a command when a device connects or disconnects, use the `--on-disconnect-execute` or `--on-connect-execute` arguments.  See the "How To" section below for how you might use this.


# How to

## How to automatically switch your mouse to a different hose when your keyboard disconnects

If you have two devices:

```
> logitech-flow-kvm list-devices

┏━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ ID             ┃ Product ┃ Name                       ┃ Serial   ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━┩
│ /dev/hidraw4:1 │ B369    │ MX Keys Mini               │ 08F5F681 │
│ /dev/hidraw5:1 │ 4082    │ MX Master 3 Wireless Mouse │ 0F591C09 │
└────────────────┴─────────┴────────────────────────────┴──────────┘
```

You can respond run a command that will listen to when the "MX Keys Mini" device above disconnects, and when it does, ask the "MX Master 3 Wireless Mouse" to connect to a specific host:

```
> logitech-flow-kvm watch --on-disconnect-execute="logitech-flow-kvm switch-to-host /dev/hidraw5:1 2" /dev/hidraw4:1
```

# Credits

Much of what this tool does is dependent upon the work put together by the folks working on [Solaar](https://github.com/pwr-Solaar/Solaar) -- really: all of the functionality provided here can also be done by Solaar alone, albeit just a little bit slower.
