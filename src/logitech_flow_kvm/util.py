import os
import re
import uuid
from typing import Any
from typing import Iterable
from typing import TypedDict
from typing import cast

import appdirs
import netifaces
from bitstruct import unpack_dict
from hidapi.udev import DeviceInfo
from logitech_receiver import Device
from logitech_receiver import Receiver
from logitech_receiver.base import receivers
from logitech_receiver.settings_templates import check_feature_setting
from OpenSSL import crypto
from solaar.cli.config import select_choice

from . import constants
from .exceptions import CannotChangeHost
from .exceptions import ChangeHostFailed
from .exceptions import DeviceNotFound
from .exceptions import NoCertificateAvailable


class DeviceStatus(TypedDict):
    """Indicates the status of a device when it connects

    See section 3.2 of
    https://lekensteyn.nl/files/logitech/
        logitech_hidpp10_specification_for_Unifying_Receivers.pdf
    for details as to the meaning of these fields
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


def get_valid_filename(s: str):
    s = str(s).strip().replace(" ", "_")
    return re.sub(r"(?u)[^-\w.]", "", s)


def get_all_ips() -> list[str]:
    ips = set()

    for interface in netifaces.interfaces():
        try:
            ips.add(netifaces.ifaddresses(interface)[netifaces.AF_INET][0]["addr"])
        except KeyError:
            pass

    return list(ips)


def get_host_certificate_path(name: str) -> str:
    user_data_dir = appdirs.user_data_dir(constants.APP_NAME, constants.APP_AUTHOR)
    os.makedirs(user_data_dir, exist_ok=True)

    server_filename = get_valid_filename(name)

    return os.path.join(user_data_dir, f"{server_filename}.cert")


def get_certificate_key_path(name: str, create=False) -> tuple[str, str]:
    user_data_dir = appdirs.user_data_dir(constants.APP_NAME, constants.APP_AUTHOR)

    os.makedirs(user_data_dir, exist_ok=True)
    cert_path = os.path.join(
        user_data_dir,
        f"{name}.cert",
    )
    key_path = os.path.join(user_data_dir, f"{name}.key")
    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        if create:
            key = crypto.PKey()
            key.generate_key(crypto.TYPE_RSA, 4096)

            cert = crypto.X509()
            subject = cert.get_subject()
            subject.C = "US"
            subject.ST = "WA"
            subject.L = "Seattle"
            subject.O = "coddingtonbear"  # noqa: E741
            subject.OU = "logitech-flow-kvm"
            subject.emailAddress = "none@none.com"

            names = [f"IP:{addr}" for addr in get_all_ips()]
            cert.add_extensions(
                [
                    crypto.X509Extension(
                        b"subjectAltName", False, ",".join(names).encode("utf-8")
                    )
                ]
            )

            cert.set_serial_number(uuid.uuid4().int)
            cert.gmtime_adj_notBefore(0)
            cert.gmtime_adj_notAfter(10 * 365 * 24 * 60 * 60)
            cert.set_issuer(subject)
            cert.set_pubkey(key)
            cert.sign(key, "sha512")

            cert_data = crypto.dump_certificate(crypto.FILETYPE_PEM, cert).decode(
                "utf-8"
            )
            key_data = crypto.dump_privatekey(crypto.FILETYPE_PEM, key).decode("utf-8")

            with open(cert_path, "wt") as f:
                f.write(cert_data)
            with open(key_path, "wt") as f:
                f.write(key_data)
        else:
            raise NoCertificateAvailable()

    return (cert_path, key_path)
