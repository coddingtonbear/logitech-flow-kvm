import datetime
import ipaddress
import json
import os
import struct

import platformdirs
import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from hidpp_fakes import ScriptedTransport
from logitech_flow_kvm import util
from logitech_flow_kvm.exceptions import CannotChangeHost
from logitech_flow_kvm.exceptions import NoCertificateAvailable
from logitech_flow_kvm.hidpp.exceptions import ERROR_INVALID_SUBID
from logitech_flow_kvm.hidpp.exceptions import ERROR_RESOURCE_ERROR
from logitech_flow_kvm.hidpp.exceptions import ERROR_UNKNOWN_DEVICE
from logitech_flow_kvm.hidpp.exceptions import DeviceUnreachable
from logitech_flow_kvm.hidpp.exceptions import ProtocolError
from logitech_flow_kvm.hidpp.models import ReceiverInfo
from logitech_flow_kvm.hidpp.receiver import PairedDevice
from logitech_flow_kvm.hidpp.receiver import Receiver

BOLT_INFO = ReceiverInfo(
    path="/dev/hidraw4", product_id=0xC548, kind="bolt", interface=2
)


@pytest.fixture
def user_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        platformdirs, "user_data_dir", lambda *args, **kwargs: str(tmp_path)
    )
    return tmp_path


class TestParseConnectionStatus:
    def test_parses_connected_device(self):
        # connection_reason=1, link_status=0, encryption_status=1,
        # software_present=0, device_type=0b0010 (mouse)
        data = bytes([0b10100010]) + b"\x12\x34"

        result = util.parse_connection_status(data)

        assert result["connection_reason"] == 1
        assert result["link_status"] == 0
        assert result["encryption_status"] == 1
        assert result["software_present"] == 0
        assert result["device_type"] == 2
        assert result["wireless_pid"] == b"\x12\x34"

    def test_parses_disconnected_device(self):
        data = bytes([0b01000001]) + b"\x00\x00"

        result = util.parse_connection_status(data)

        assert result["link_status"] == 1
        assert result["device_type"] == 1


class TestGetValidFilename:
    def test_replaces_spaces_with_underscores(self):
        assert util.get_valid_filename("my server name") == "my_server_name"

    def test_strips_unsafe_characters(self):
        assert util.get_valid_filename("host!@#$%:1234") == "host1234"

    def test_preserves_safe_characters(self):
        assert util.get_valid_filename("host-1.local") == "host-1.local"


class TestGetAllIps:
    def test_returns_ipv4_addresses(self):
        ips = util.get_all_ips()

        assert ips
        for ip in ips:
            assert isinstance(ipaddress.ip_address(ip), ipaddress.IPv4Address)


class TestHostCertificateAndToken:
    def test_roundtrip(self, user_data_dir):
        util.set_host_certificate_and_token("myserver", "CERTIFICATE DATA", "my-token")

        certificate_path, token = util.get_host_certificate_path_and_token("myserver")

        assert token == "my-token"
        with open(certificate_path) as inf:
            assert inf.read() == "CERTIFICATE DATA"

    def test_returns_no_token_when_unpaired(self, user_data_dir):
        _, token = util.get_host_certificate_path_and_token("unknown-server")

        assert token is None

    def test_returns_no_token_for_invalid_token_file(self, user_data_dir):
        with open(os.path.join(user_data_dir, "myserver.json"), "w") as outf:
            outf.write("not json")

        _, token = util.get_host_certificate_path_and_token("myserver")

        assert token is None

    def test_token_file_contents(self, user_data_dir):
        util.set_host_certificate_and_token("myserver", "CERT", "tok")

        with open(os.path.join(user_data_dir, "myserver.json")) as inf:
            assert json.load(inf) == {"token": "tok"}


class TestGetCertificateKeyPath:
    def test_raises_when_no_certificate_exists(self, user_data_dir):
        with pytest.raises(NoCertificateAvailable):
            util.get_certificate_key_path("server")

    def test_creates_certificate_and_key(self, user_data_dir):
        cert_path, key_path = util.get_certificate_key_path("server", create=True)

        assert os.path.exists(cert_path)
        assert os.path.exists(key_path)

        with open(cert_path, "rb") as inf:
            certificate = x509.load_pem_x509_certificate(inf.read())
        with open(key_path, "rb") as inf:
            key = load_pem_private_key(inf.read(), password=None)

        assert isinstance(key, rsa.RSAPrivateKey)
        assert key.key_size == 4096

        san = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
        san_ips = {str(ip) for ip in san.value.get_values_for_type(x509.IPAddress)}
        assert san_ips == set(util.get_all_ips())

        lifetime = certificate.not_valid_after_utc - certificate.not_valid_before_utc
        assert lifetime == datetime.timedelta(days=10 * 365)

        # Without CA:TRUE, modern OpenSSL refuses to use this self-signed
        # cert as a trust anchor (requests' verify=<path> does exactly that).
        basic_constraints = certificate.extensions.get_extension_for_class(
            x509.BasicConstraints
        )
        assert basic_constraints.value.ca is True
        assert basic_constraints.critical is True

    def test_reuses_existing_certificate(self, user_data_dir):
        first = util.get_certificate_key_path("server", create=True)
        with open(first[0], "rb") as inf:
            first_contents = inf.read()

        second = util.get_certificate_key_path("server")

        assert first == second
        with open(second[0], "rb") as inf:
            assert inf.read() == first_contents

    def test_certificate_includes_requested_hostnames(self, user_data_dir):
        cert_path, _ = util.get_certificate_key_path(
            "server", create=True, hostnames=["foo.lan", "bar.lan"]
        )

        with open(cert_path, "rb") as inf:
            certificate = x509.load_pem_x509_certificate(inf.read())

        san = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
        assert set(san.value.get_values_for_type(x509.DNSName)) == {
            "foo.lan",
            "bar.lan",
        }

    def test_reuses_certificate_when_hostnames_unchanged(self, user_data_dir):
        first_path, _ = util.get_certificate_key_path(
            "server", create=True, hostnames=["foo.lan"]
        )
        with open(first_path, "rb") as inf:
            first_contents = inf.read()

        second_path, _ = util.get_certificate_key_path(
            "server", create=True, hostnames=["foo.lan"]
        )
        with open(second_path, "rb") as inf:
            assert inf.read() == first_contents

    def test_regenerates_when_hostnames_change(self, user_data_dir):
        first_path, _ = util.get_certificate_key_path(
            "server", create=True, hostnames=["foo.lan"]
        )
        with open(first_path, "rb") as inf:
            first_contents = inf.read()

        second_path, _ = util.get_certificate_key_path(
            "server", create=True, hostnames=["bar.lan"]
        )
        with open(second_path, "rb") as inf:
            second_contents = inf.read()

        assert second_contents != first_contents

        certificate = x509.load_pem_x509_certificate(second_contents)
        san = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
        assert set(san.value.get_values_for_type(x509.DNSName)) == {"bar.lan"}

    def test_ip_address_changes_do_not_trigger_regeneration(
        self, user_data_dir, monkeypatch
    ):
        monkeypatch.setattr(util, "get_all_ips", lambda: ["10.0.0.1"])
        first_path, _ = util.get_certificate_key_path(
            "server", create=True, hostnames=["foo.lan"]
        )
        with open(first_path, "rb") as inf:
            first_contents = inf.read()

        monkeypatch.setattr(util, "get_all_ips", lambda: ["10.0.0.2"])
        second_path, _ = util.get_certificate_key_path(
            "server", create=True, hostnames=["foo.lan"]
        )
        with open(second_path, "rb") as inf:
            assert inf.read() == first_contents


class TestChangeDeviceHost:
    """`change_device_host` has to tell two very different failures apart:
    a device that *answered* but can't be switched, and a device the receiver
    holds no link to at all. Only the latter says anything about where the
    device is, and `reconciler.Reconciler` acts on that distinction."""

    @staticmethod
    def _device(respond) -> tuple[PairedDevice, ScriptedTransport]:
        transport = ScriptedTransport(respond=respond)
        device = PairedDevice(
            receiver=Receiver(BOLT_INFO, transport=transport),
            number=1,
            wpid="0000",
            kind="mouse",
            serial="F262458A",
            codename=None,
        )
        return device, transport

    def test_switches_a_reachable_device(self):
        def respond(devnumber, payload, long_message):
            if payload[2:] == struct.pack("!H", 0x1814):  # root: locate 0x1814
                return payload[:2] + bytes([0x08, 0x00, 0x04])
            return payload[:2] + bytes([0x03, 0x00])  # 3 hosts, currently on #1

        device, transport = self._device(respond)

        util.change_device_host(device, 2)

        # The last write is setCurrentHost with the 0-indexed host on the wire.
        devnumber, payload, _ = transport.writes[-1]
        assert devnumber == 1
        assert payload[2:3] == bytes([1])

    def test_resource_error_is_reported_as_unreachable(self):
        # 0x09 (resource error) is what a receiver answers with once the
        # device has switched to another host -- exactly the case that used to
        # be retried, loudly, forever.
        def respond(devnumber, payload, long_message):
            return b"\x8f" + payload[:2] + bytes([ERROR_RESOURCE_ERROR])

        device, _ = self._device(respond)

        with pytest.raises(DeviceUnreachable) as caught:
            util.change_device_host(device, 2)

        assert caught.value.device_id == "F262458A"

    def test_unknown_device_error_is_reported_as_unreachable(self):
        def respond(devnumber, payload, long_message):
            return b"\x8f" + payload[:2] + bytes([ERROR_UNKNOWN_DEVICE])

        device, _ = self._device(respond)

        with pytest.raises(DeviceUnreachable):
            util.change_device_host(device, 2)

    def test_other_protocol_errors_are_not_mistaken_for_absence(self):
        # An invalid-subid error means the request was wrong, not that the
        # device is elsewhere; misreading it as absence would stop us driving
        # a device that is sitting right here.
        def respond(devnumber, payload, long_message):
            return b"\x8f" + payload[:2] + bytes([ERROR_INVALID_SUBID])

        device, _ = self._device(respond)

        with pytest.raises(ProtocolError):
            util.change_device_host(device, 2)

    def test_a_device_without_host_switching_raises_cannot_change_host(self):
        # Feature index 0 means "I don't implement 0x1814". This is the same
        # branch a silent device (asleep, mid-roam) lands in -- deliberately
        # kept as retry-and-warn, since neither is evidence about *where* the
        # device is, the way an unreachable error is.
        def respond(devnumber, payload, long_message):
            return payload[:2] + bytes([0x00, 0x00, 0x00])

        device, _ = self._device(respond)

        with pytest.raises(CannotChangeHost):
            util.change_device_host(device, 2)

    def test_a_host_outside_the_devices_range_raises_cannot_change_host(self):
        def respond(devnumber, payload, long_message):
            if payload[2:] == struct.pack("!H", 0x1814):
                return payload[:2] + bytes([0x08, 0x00, 0x04])
            return payload[:2] + bytes([0x02, 0x00])  # only 2 hosts

        device, _ = self._device(respond)

        with pytest.raises(CannotChangeHost):
            util.change_device_host(device, 3)
