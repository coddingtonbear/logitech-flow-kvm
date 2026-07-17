import datetime
import ipaddress
import json
import os

import platformdirs
import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from logitech_flow_kvm import util
from logitech_flow_kvm.exceptions import NoCertificateAvailable


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

    def test_reuses_existing_certificate(self, user_data_dir):
        first = util.get_certificate_key_path("server", create=True)
        with open(first[0], "rb") as inf:
            first_contents = inf.read()

        second = util.get_certificate_key_path("server")

        assert first == second
        with open(second[0], "rb") as inf:
            assert inf.read() == first_contents
