from typing import cast, Iterable

from logitech_receiver import Device, Receiver
from logitech_receiver.base import receivers_and_devices
from hidapi.udev import DeviceInfo

from .exceptions import DeviceNotFound


def get_device_id(device: Device) -> str:
    if device.receiver:
        return f"{device.receiver.path}:{device.number}"
    return device.path


def get_device_by_id(device_id: str) -> Device:
    for device_info in cast(Iterable[DeviceInfo], receivers_and_devices()):
        if ":" in device_id:
            receiver_id, device_idx = device_id.split(":")

            if receiver_id == device_info.path:
                receiver = Receiver.open(device_info)
                device = receiver[int(device_idx)]
                if not device:
                    break
                return device
        else:
            if device_id == device_info.path:
                return Device.open(device_info)

    raise DeviceNotFound(device_id)
