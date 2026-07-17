import dataclasses

DEVICE_KIND: dict[int, str] = {
    0x00: "unknown",
    0x01: "keyboard",
    0x02: "mouse",
    0x03: "numpad",
    0x04: "presenter",
    0x07: "remote",
    0x08: "trackball",
    0x09: "touchpad",
    0x0D: "headset",
}


@dataclasses.dataclass(frozen=True)
class ReceiverInfo:
    """A receiver discovered on the system, before it has been opened."""

    path: str
    product_id: int
    kind: str
    interface: int | None


@dataclasses.dataclass(frozen=True)
class ChangeHostInfo:
    feature_index: int
    num_hosts: int
    current_host: int


@dataclasses.dataclass(frozen=True)
class Notification:
    report_id: int
    devnumber: int
    sub_id: int
    address: int
    data: bytes
