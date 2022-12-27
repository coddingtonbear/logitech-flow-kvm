from typing import cast, Iterable, Any, TypedDict

from bitstruct import unpack_dict
from solaar.cli.config import select_choice
from logitech_receiver import Device, Receiver
from logitech_receiver.base import receivers
from logitech_receiver.settings_templates import check_feature_setting
from hidapi.udev import DeviceInfo

from .exceptions import CannotChangeHost, ChangeHostFailed, DeviceNotFound


class DeviceStatus(TypedDict):
    """Indicates the status of a device when it connects

    See section 3.2 of https://lekensteyn.nl/files/logitech/logitech_hidpp10_specification_for_Unifying_Receivers.pdf for details
    as to the meaning of these fields

    """

    # 0 = packet without payload
    # 1 = packet with payload
    connection_reason: int

    # 0 = Link established (in range)
    # 1 = Link not established (out of range)
    link_status: int

    # 0 = Link not encrypted
    # 1 = link encrypted
    encryption_status: int

    # reflects flag in register 0x00, r1, bit 3
    software_present: int

    # 0x00 = Unknown
    # 0x01 = Keyboard
    # 0x02 = Mouse
    # 0x03 = Numpad
    # 0x04 = Presenter
    # 0x05 = Reserved for future
    # 0x06 = Reserved for future
    # 0x07 =Reserved for future
    # 0x08 =Trackball
    # 0x09 =Touchpad
    # 0x0A..0x0F = Reserved
    device_type: int

    wireless_id: bytes


def get_device_id(device: Device) -> str:
    if device.receiver:
        return f"{device.receiver.path}:{device.number}"
    return device.path


def get_device_by_id(device_id: str) -> Device:
    for device_info in cast(Iterable[DeviceInfo], receivers()):
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


def parse_connection_status(data: bytes) -> DeviceStatus:
    names = [
        "connection_reason",
        "link_status",
        "encryption_status",
        "software_present",
        "device_type",
        "wireless_pid",
    ]
    return unpack_dict(">u1u1u1u1u4r16", names, data)


def get_device_host_setting(device: Device) -> Any:
    setting = check_feature_setting(device, "change-host")
    if setting:
        return setting

    if device.descriptor and device.descriptor.settings:
        for setting_class in device.descriptor.settings:
            if setting_class.register and setting_class.name == "change-host":
                return setting_class.build(device)


def change_device_host(device: Device, host: int) -> None:
    setting = get_device_host_setting(device)
    if not setting:
        raise CannotChangeHost(device)

    target_value = select_choice(str(host), setting.choices, setting, None)
    result = setting.write(target_value, save=False)

    if not result or result != target_value:
        raise ChangeHostFailed()
