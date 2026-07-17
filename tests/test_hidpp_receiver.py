import struct

from hidpp_fakes import ScriptedReply
from hidpp_fakes import ScriptedTransport
from hidpp_fakes import register_matcher
from logitech_flow_kvm.hidpp.models import ReceiverInfo
from logitech_flow_kvm.hidpp.receiver import SUB_BOLT_PAIRING_INFO
from logitech_flow_kvm.hidpp.receiver import SUB_RECEIVER_INFORMATION
from logitech_flow_kvm.hidpp.receiver import SUB_UNIFYING_DEVICE_NAME
from logitech_flow_kvm.hidpp.receiver import SUB_UNIFYING_EXTENDED_PAIRING_INFO
from logitech_flow_kvm.hidpp.receiver import SUB_UNIFYING_PAIRING_INFO
from logitech_flow_kvm.hidpp.receiver import Receiver

BOLT_INFO = ReceiverInfo(
    path="/dev/hidraw4", product_id=0xC548, kind="bolt", interface=2
)
UNIFYING_INFO = ReceiverInfo(
    path="/dev/hidraw5", product_id=0xC52B, kind="unifying", interface=2
)


def _no_codename_reply(first_param_byte: int) -> ScriptedReply:
    # An empty name reply, so tests that don't care about codenames don't have
    # to wait out the real request timeout for an unscripted register read.
    def matcher(devnumber: int, payload: bytes) -> bool:
        return payload[2] == first_param_byte

    return ScriptedReply(matcher, bytes([first_param_byte, 0]))


def _max_devices_reply(count: int = 6) -> ScriptedReply:
    return ScriptedReply(
        register_matcher(0xFF, bytes([SUB_RECEIVER_INFORMATION])),
        bytes([SUB_RECEIVER_INFORMATION, 0, 0, 0, 0, 0, count]),
    )


class TestBoltPairedDevice:
    def test_parses_wpid_kind_and_serial(self):
        # Regression check: these exact bytes reproduce what was observed against
        # the user's real MX Keys Mini (wpid B369, keyboard, serial 08F5F681).
        pairing_reply = bytes(
            [SUB_BOLT_PAIRING_INFO + 1, 0x01, 0x69, 0xB3, 0x08, 0xF5, 0xF6, 0x81]
        )
        transport = ScriptedTransport(
            [
                ScriptedReply(
                    register_matcher(0xFF, bytes([SUB_BOLT_PAIRING_INFO + 1])),
                    pairing_reply,
                ),
                _no_codename_reply(0x60 + 1),
            ]
        )
        receiver = Receiver(BOLT_INFO, transport=transport)

        device = receiver.get_device(1)

        assert device is not None
        assert device.wpid == "B369"
        assert device.kind == "keyboard"
        assert device.serial == "08F5F681"
        assert device.receiver is receiver
        assert device.path == "/dev/hidraw4:1"

    def test_missing_slot_returns_none(self):
        transport = ScriptedTransport()
        receiver = Receiver(BOLT_INFO, transport=transport)

        assert receiver.get_device(1) is None

    def test_max_devices_defaults_to_six_without_a_register_read(self):
        transport = ScriptedTransport()
        receiver = Receiver(BOLT_INFO, transport=transport)

        assert receiver.max_devices == 6


class TestUnifyingPairedDevice:
    def test_parses_wpid_kind_and_serial_from_two_reads(self):
        # Regression check: reproduces the user's real MX Anywhere 2S
        # (wpid 406A, mouse, serial F262458A).
        pairing_reply = bytes(
            [SUB_UNIFYING_PAIRING_INFO, 0x00, 0x08, 0x40, 0x6A, 0x00, 0x00, 0x02]
        )
        extended_reply = bytes(
            [SUB_UNIFYING_EXTENDED_PAIRING_INFO, 0xF2, 0x62, 0x45, 0x8A]
        )
        transport = ScriptedTransport(
            [
                _max_devices_reply(),
                ScriptedReply(
                    register_matcher(0xFF, bytes([SUB_UNIFYING_PAIRING_INFO])),
                    pairing_reply,
                ),
                ScriptedReply(
                    register_matcher(0xFF, bytes([SUB_UNIFYING_EXTENDED_PAIRING_INFO])),
                    extended_reply,
                ),
                _no_codename_reply(SUB_UNIFYING_DEVICE_NAME),
            ]
        )
        receiver = Receiver(UNIFYING_INFO, transport=transport)

        device = receiver.get_device(1)

        assert device is not None
        assert device.wpid == "406A"
        assert device.kind == "mouse"
        assert device.serial == "F262458A"

    def test_max_devices_read_from_receiver_information_register(self):
        info_reply = bytes([SUB_RECEIVER_INFORMATION, 0, 0, 0, 0, 0, 3])
        transport = ScriptedTransport(
            [
                ScriptedReply(
                    register_matcher(0xFF, bytes([SUB_RECEIVER_INFORMATION])),
                    info_reply,
                )
            ]
        )
        receiver = Receiver(UNIFYING_INFO, transport=transport)

        assert receiver.max_devices == 3

    def test_max_devices_falls_back_to_default_when_unreadable(self):
        transport = ScriptedTransport()
        receiver = Receiver(UNIFYING_INFO, transport=transport)

        assert receiver.max_devices == 6

    def test_codename_is_decoded_from_ascii_bytes(self):
        pairing_reply = bytes(
            [SUB_UNIFYING_PAIRING_INFO, 0x00, 0x08, 0x40, 0x6A, 0x00, 0x00, 0x02]
        )
        name = b"MX Anywhere 2S"
        name_reply = bytes([SUB_UNIFYING_DEVICE_NAME, len(name)]) + name
        transport = ScriptedTransport(
            [
                _max_devices_reply(),
                ScriptedReply(
                    register_matcher(0xFF, bytes([SUB_UNIFYING_PAIRING_INFO])),
                    pairing_reply,
                ),
                ScriptedReply(
                    register_matcher(0xFF, bytes([SUB_UNIFYING_EXTENDED_PAIRING_INFO])),
                    bytes([SUB_UNIFYING_EXTENDED_PAIRING_INFO, 0, 0, 0, 0]),
                ),
                ScriptedReply(
                    register_matcher(0xFF, bytes([SUB_UNIFYING_DEVICE_NAME])),
                    name_reply,
                ),
            ]
        )
        receiver = Receiver(UNIFYING_INFO, transport=transport)

        device = receiver.get_device(1)

        assert device is not None
        assert device.codename == "MX Anywhere 2S"


class TestChangeHost:
    def test_get_change_host_info_reads_feature_index_and_state(self):
        transport = ScriptedTransport(
            [
                ScriptedReply(
                    register_matcher(1, struct.pack("!H", 0x1814)),
                    bytes([0x08, 0x00, 0x04]),
                ),
                ScriptedReply(
                    lambda dev, payload: payload[2:] == b"", bytes([0x03, 0x01])
                ),
            ]
        )
        receiver = Receiver(BOLT_INFO, transport=transport)

        info = receiver.get_change_host_info(1)

        assert info is not None
        assert info.feature_index == 0x08
        assert info.num_hosts == 3
        assert info.current_host == 1

    def test_set_current_host_sends_a_fire_and_forget_write(self):
        transport = ScriptedTransport()
        receiver = Receiver(BOLT_INFO, transport=transport)

        receiver.set_current_host(1, feature_index=0x08, host=0)

        assert len(transport.writes) == 1
        devnumber, payload, _long_message = transport.writes[0]
        assert devnumber == 1
        header = struct.unpack("!H", payload[:2])[0]
        assert header & 0xFF00 == 0x0800
        assert header & 0x00F0 == 0x0010  # function 0x10 = setCurrentHost
        assert payload[2:3] == bytes([0])


class TestNotifications:
    def test_enable_connection_notifications_writes_expected_flags(self):
        flags = (0x100000 | 0x000100 | 0x000800).to_bytes(3, "big")
        transport = ScriptedTransport(
            [ScriptedReply(register_matcher(0xFF, flags), flags)]
        )
        receiver = Receiver(BOLT_INFO, transport=transport)

        receiver.enable_connection_notifications()

        assert len(transport.writes) == 1
        devnumber, payload, _long_message = transport.writes[0]
        assert devnumber == 0xFF
        header = struct.unpack("!H", payload[:2])[0]
        assert header & 0xFF00 == 0x8000  # set short register
        assert payload[2:5] == flags

    def test_notify_devices_writes_receiver_connection_register(self):
        transport = ScriptedTransport(
            [ScriptedReply(register_matcher(0xFF, bytes([0x02])), bytes([0x02]))]
        )
        receiver = Receiver(BOLT_INFO, transport=transport)

        receiver.notify_devices()

        assert len(transport.writes) == 1
        devnumber, payload, _long_message = transport.writes[0]
        assert devnumber == 0xFF
        assert payload[2:3] == bytes([0x02])
