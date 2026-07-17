import datetime
import ipaddress
import json
import os
import re
import socket
from collections.abc import Iterable
from json.decoder import JSONDecodeError
from typing import TypedDict

import platformdirs
import psutil
from bitstruct import unpack_dict
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from . import constants
from .exceptions import CannotChangeHost
from .exceptions import DeviceNotFound
from .exceptions import NoCertificateAvailable
from .hidpp import PairedDevice
from .hidpp import Receiver
from .hidpp import find_receivers


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

    wireless_pid: bytes


def get_theoretical_max_device_count() -> int:
    max_count = 0

    # Receivers are opened transiently here just to read max_devices; the
    # file descriptors are not reused by get_devices(), which opens its own.
    for info in find_receivers():
        with Receiver(info) as receiver:
            max_count += receiver.max_devices

    return max_count


def get_devices() -> Iterable[PairedDevice | None]:
    # Receivers opened here are intentionally left open for the lifetime of
    # the process: callers (e.g. flow_server's leader/follower devices) keep
    # using `device.receiver` afterward to enable notifications and switch hosts.
    for info in find_receivers():
        receiver = Receiver(info)
        for number in range(1, receiver.max_devices + 1):
            yield receiver.get_device(number)


def get_device_by_path(device_path: str) -> PairedDevice:
    if ":" not in device_path:
        raise DeviceNotFound(device_path)

    receiver_path, _, number_text = device_path.rpartition(":")
    try:
        number = int(number_text)
    except ValueError:
        raise DeviceNotFound(device_path) from None

    for info in find_receivers():
        if info.path != receiver_path:
            continue
        receiver = Receiver(info)
        device = receiver.get_device(number)
        if device is None:
            receiver.close()
            raise DeviceNotFound(device_path)
        return device

    raise DeviceNotFound(device_path)


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


def change_device_host(device: PairedDevice, host: int) -> None:
    """Switch `device` to `host`. `host` is 1-indexed, matching the CLI and README."""
    info = device.receiver.get_change_host_info(device.number)
    if info is None or not 1 <= host <= info.num_hosts:
        raise CannotChangeHost(device.id)

    device.receiver.set_current_host(device.number, info.feature_index, host - 1)


def get_valid_filename(s: str) -> str:
    s = str(s).strip().replace(" ", "_")
    return re.sub(r"(?u)[^-\w.]", "", s)


def get_all_ips() -> list[str]:
    ips = set()

    for addresses in psutil.net_if_addrs().values():
        for address in addresses:
            if address.family == socket.AF_INET:
                ips.add(address.address)

    return list(ips)


def get_host_certificate_path(name: str) -> str:
    user_data_dir = platformdirs.user_data_dir(constants.APP_NAME, constants.APP_AUTHOR)
    os.makedirs(user_data_dir, exist_ok=True)

    server_filename = get_valid_filename(name)

    return os.path.join(user_data_dir, f"{server_filename}.cert")


def get_host_token_path(name: str) -> str:
    user_data_dir = platformdirs.user_data_dir(constants.APP_NAME, constants.APP_AUTHOR)
    os.makedirs(user_data_dir, exist_ok=True)

    server_filename = get_valid_filename(name)

    return os.path.join(user_data_dir, f"{server_filename}.json")


def get_host_certificate_path_and_token(name: str) -> tuple[str, str | None]:
    certificate_path = get_host_certificate_path(name)
    token_path = get_host_token_path(name)

    token: str | None = None

    try:
        with open(token_path) as inf:
            token_data = json.load(inf)
            token = token_data["token"]
    except (FileNotFoundError, JSONDecodeError):
        pass

    return certificate_path, token


def set_host_certificate_and_token(name: str, certificate: str, token: str) -> None:
    certificate_path = get_host_certificate_path(name)
    token_path = get_host_token_path(name)

    with open(certificate_path, "w") as outf:
        outf.write(certificate)

    with open(token_path, "w") as outf:
        json.dump({"token": token}, outf)


def _certificate_dns_names(cert_path: str) -> set[str]:
    with open(cert_path, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return set()
    return set(san.value.get_values_for_type(x509.DNSName))


def _write_certificate(cert_path: str, key_path: str, hostnames: set[str]) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)

    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "WA"),
            x509.NameAttribute(NameOID.LOCALITY_NAME, "Seattle"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "coddingtonbear"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "logitech-flow-kvm"),
            x509.NameAttribute(NameOID.EMAIL_ADDRESS, "none@none.com"),
        ]
    )
    subject_alt_names: list[x509.GeneralName] = [
        x509.IPAddress(ipaddress.ip_address(addr)) for addr in get_all_ips()
    ]
    subject_alt_names += [x509.DNSName(hostname) for hostname in sorted(hostnames)]

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=10 * 365))
        .add_extension(x509.SubjectAlternativeName(subject_alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA512())
    )

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )


def get_certificate_key_path(
    name: str, create: bool = False, hostnames: Iterable[str] = ()
) -> tuple[str, str]:
    user_data_dir = platformdirs.user_data_dir(constants.APP_NAME, constants.APP_AUTHOR)

    os.makedirs(user_data_dir, exist_ok=True)
    cert_path = os.path.join(user_data_dir, f"{name}.cert")
    key_path = os.path.join(user_data_dir, f"{name}.key")

    hostnames = set(hostnames)
    exists = os.path.exists(cert_path) and os.path.exists(key_path)
    # Only hostnames (explicit, operator-provided) trigger regeneration --
    # not IP addresses, which are auto-discovered and can change on their
    # own (e.g. DHCP) without the operator asking for a new certificate.
    stale = exists and _certificate_dns_names(cert_path) != hostnames

    if not exists or stale:
        if not create:
            raise NoCertificateAvailable()
        if stale:
            print(
                "Certificate hostnames changed; regenerating the server certificate. "
                "Any already-running flow-client instances must be restarted -- "
                "they will not recover on their own -- and will need to re-pair."
            )
        _write_certificate(cert_path, key_path, hostnames)

    return (cert_path, key_path)
