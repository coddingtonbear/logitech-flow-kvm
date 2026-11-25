import re

from .node_protocol import DeviceAdvertisement

MINIMUM_ID_PREFIX = 8
UNKNOWN_KIND = "unknown"


def normalize_device_id(device_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", device_id).upper()


def common_prefix_length(left: str, right: str) -> int:
    left_normalized = normalize_device_id(left)
    right_normalized = normalize_device_id(right)
    length = 0
    for left_character, right_character in zip(
        left_normalized, right_normalized, strict=False
    ):
        if left_character != right_character:
            break
        length += 1
    return length


def match_device(
    remote: DeviceAdvertisement, local: list[DeviceAdvertisement]
) -> DeviceAdvertisement | None:
    candidates: list[tuple[int, DeviceAdvertisement]] = []
    for device in local:
        if remote.product and device.product and remote.product != device.product:
            continue
        # A Bluetooth device does not report its kind, while the same device
        # behind a receiver does; only two known kinds can rule a match out.
        kinds = {remote.kind, device.kind} - {"", UNKNOWN_KIND}
        if len(kinds) > 1:
            continue
        prefix = common_prefix_length(remote.id, device.id)
        if prefix >= MINIMUM_ID_PREFIX:
            candidates.append((prefix, device))
    if not candidates:
        return None
    candidates.sort(key=lambda candidate: candidate[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    return candidates[0][1]
