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

First, find the ID of the device you'd like to switch:

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

Next, change the relevant device to your desired host number using the `switch-to-host` command:

```
> logitech-flow-kvm switch-to-host /dev/hidraw4:1 2
```

# Credits

Much of what this tool does is dependent upon the work put together by the folks working on [Solaar](https://github.com/pwr-Solaar/Solaar) -- really: all of the functionality provided here can also be done by Solaar alone, albeit just a little bit slower.
