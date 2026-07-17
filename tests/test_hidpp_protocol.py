import struct

import pytest

from hidpp_fakes import ScriptedReply
from hidpp_fakes import ScriptedTransport
from hidpp_fakes import register_matcher
from logitech_flow_kvm.hidpp.exceptions import ProtocolError
from logitech_flow_kvm.hidpp.protocol import HidppConnection
from logitech_flow_kvm.hidpp.protocol import make_notification

SHORT_TIMEOUT = 0.05


class TestMakeNotification:
    def test_connection_notification_is_a_notification(self):
        data = bytes([0x41, 0x00, 0x00, 0x12, 0x34])
        notification = make_notification(0x10, 1, data)

        assert notification is not None
        assert notification.sub_id == 0x41
        assert notification.devnumber == 1
        assert notification.data == data[2:]

    def test_register_error_reply_is_not_a_notification(self):
        data = bytes([0x8F, 0x81, 0x02, 0x09, 0x00])
        assert make_notification(0x10, 0xFF, data) is None

    def test_no_op_notification_is_ignored(self):
        data = bytes([0x00, 0x00, 0x00, 0x00, 0x00])
        assert make_notification(0x10, 1, data) is None

    def test_feature_notification_with_zero_software_id_is_a_notification(self):
        # address & 0x0F == 0 marks a HID++2.0 feature notification (SoftwareId 0)
        data = bytes([0x02, 0x00, 0x00, 0x00, 0x00])
        assert make_notification(0x10, 1, data) is not None


class TestRequest:
    def test_matches_reply_by_echoed_header(self):
        transport = ScriptedTransport(
            [
                ScriptedReply(
                    register_matcher(0xFF, bytes([0x03])), bytes([0x03, 0xAA, 0xBB])
                )
            ]
        )
        conn = HidppConnection(transport)

        reply = conn.request(0xFF, 0x8100 | 0x2B5, bytes([0x03]), timeout=SHORT_TIMEOUT)

        assert reply == bytes([0x03, 0xAA, 0xBB])

    def test_no_reply_true_does_not_wait_for_a_reply(self):
        transport = ScriptedTransport()
        conn = HidppConnection(transport)

        reply = conn.request(1, 0x0910, bytes([0x00]), no_reply=True)

        assert reply is None
        assert len(transport.writes) == 1

    def test_times_out_returning_none_when_nothing_matches(self):
        transport = ScriptedTransport()
        conn = HidppConnection(transport)

        reply = conn.request(1, 0x0000, timeout=SHORT_TIMEOUT)

        assert reply is None

    def test_short_error_reply_raises_protocol_error(self):
        def respond(devnumber, payload, long_message):
            return b"\x8f" + payload[:2] + bytes([0x09])

        transport = ScriptedTransport(respond=respond)
        conn = HidppConnection(transport)

        with pytest.raises(ProtocolError) as exc_info:
            conn.request(0xFF, 0x8100 | 0x2B5, timeout=SHORT_TIMEOUT)

        assert exc_info.value.error_code == 0x09


class TestRegisters:
    def test_read_register_encodes_short_get_request(self):
        transport = ScriptedTransport()
        conn = HidppConnection(transport)

        conn.read_register(0xFF, 0x02, timeout=SHORT_TIMEOUT)

        header = struct.unpack("!H", transport.writes[0][1][:2])[0]
        assert header & 0xFF00 == 0x8100

    def test_read_register_encodes_long_get_request(self):
        transport = ScriptedTransport()
        conn = HidppConnection(transport)

        conn.read_register(0xFF, 0x2B5, timeout=SHORT_TIMEOUT)

        header = struct.unpack("!H", transport.writes[0][1][:2])[0]
        assert header & 0xFF00 == 0x8300

    def test_write_register_encodes_set_request(self):
        transport = ScriptedTransport()
        conn = HidppConnection(transport)

        conn.write_register(
            0xFF, 0x00, bytes([0x00, 0x00, 0x00]), timeout=SHORT_TIMEOUT
        )

        header = struct.unpack("!H", transport.writes[0][1][:2])[0]
        assert header & 0xFF00 == 0x8000


class TestPing:
    def test_returns_protocol_version_on_matching_reply(self):
        def respond(devnumber, payload, long_message):
            marker = payload[4:5]
            return payload[:2] + bytes([4, 5]) + marker

        transport = ScriptedTransport(respond=respond)
        conn = HidppConnection(transport)

        version = conn.ping(1, timeout=SHORT_TIMEOUT)

        assert version == 4.5

    def test_resource_error_means_unreachable(self):
        def respond(devnumber, payload, long_message):
            return b"\x8f" + payload[:2] + bytes([0x09])

        transport = ScriptedTransport(respond=respond)
        conn = HidppConnection(transport)

        assert conn.ping(1, timeout=SHORT_TIMEOUT) is None

    def test_invalid_subid_means_hidpp1_device(self):
        def respond(devnumber, payload, long_message):
            return b"\x8f" + payload[:2] + bytes([0x01])

        transport = ScriptedTransport(respond=respond)
        conn = HidppConnection(transport)

        assert conn.ping(1, timeout=SHORT_TIMEOUT) == 1.0

    def test_no_reply_means_unreachable(self):
        transport = ScriptedTransport()
        conn = HidppConnection(transport)

        assert conn.ping(1, timeout=SHORT_TIMEOUT) is None


class TestGetFeatureIndex:
    def test_returns_index_from_reply(self):
        transport = ScriptedTransport(
            [
                ScriptedReply(
                    register_matcher(1, struct.pack("!H", 0x1814)),
                    bytes([0x08, 0x00, 0x04]),
                )
            ]
        )
        conn = HidppConnection(transport)

        assert conn.get_feature_index(1, 0x1814) == 0x08

    def test_returns_none_when_feature_unsupported(self):
        transport = ScriptedTransport(
            [
                ScriptedReply(
                    register_matcher(1, struct.pack("!H", 0x1814)),
                    bytes([0x00, 0x00, 0x00]),
                )
            ]
        )
        conn = HidppConnection(transport)

        assert conn.get_feature_index(1, 0x1814) is None
