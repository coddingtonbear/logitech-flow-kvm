from typing import cast, Iterable

from logitech_receiver import Device, Receiver
from logitech_receiver.base import receivers_and_devices
from hidapi.udev import DeviceInfo


def get_receivers() -> Iterable[Device | Receiver]:
    for device_info in cast(Iterable[DeviceInfo], receivers_and_devices()):
        if device_info.isDevice:
            yield Device.open(device_info)
        else:
            yield Receiver.open(device_info)


def get_devices() -> Iterable[Device]:
    for receiver in get_receivers():
        if isinstance(receiver, Device):
            yield receiver
            continue
        else:
            try:
                for device in receiver:
                    yield device
            except Exception:
                pass

