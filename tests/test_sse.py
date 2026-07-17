from logitech_flow_kvm.sse import EventBroadcaster
from logitech_flow_kvm.sse import format_sse
from logitech_flow_kvm.sse import parse_sse_stream


class TestFormatSse:
    def test_formats_event_and_data(self):
        assert format_sse("leader-host", "2") == "event: leader-host\ndata: 2\n\n"


class TestParseSseStream:
    def test_parses_a_single_event(self):
        lines = ["event: leader-host", "data: 2", ""]

        assert list(parse_sse_stream(lines)) == [("leader-host", "2")]

    def test_defaults_to_message_type_without_an_event_field(self):
        lines = ["data: hello", ""]

        assert list(parse_sse_stream(lines)) == [("message", "hello")]

    def test_parses_multiple_events_in_one_stream(self):
        lines = [
            "event: leader-host",
            "data: 1",
            "",
            "event: host-connected",
            "data: 2",
            "",
        ]

        assert list(parse_sse_stream(lines)) == [
            ("leader-host", "1"),
            ("host-connected", "2"),
        ]

    def test_skips_comment_lines_used_for_keepalives(self):
        lines = [": keepalive", "event: leader-host", "data: 3", ""]

        assert list(parse_sse_stream(lines)) == [("leader-host", "3")]

    def test_skips_none_lines_from_iter_lines(self):
        lines = [None, "event: leader-host", "data: 3", ""]

        assert list(parse_sse_stream(lines)) == [("leader-host", "3")]

    def test_yields_a_trailing_event_without_a_final_blank_line(self):
        lines = ["event: leader-host", "data: 4"]

        assert list(parse_sse_stream(lines)) == [("leader-host", "4")]

    def test_joins_multiple_data_lines_with_newlines(self):
        lines = ["data: line one", "data: line two", ""]

        assert list(parse_sse_stream(lines)) == [("message", "line one\nline two")]

    def test_round_trips_through_format_sse(self):
        message = format_sse("leader-host", "2")

        assert list(parse_sse_stream(message.split("\n"))) == [("leader-host", "2")]


class TestEventBroadcaster:
    def test_starts_with_no_state(self):
        broadcaster = EventBroadcaster()

        assert broadcaster.state is None

    def test_subscribe_returns_current_state(self):
        broadcaster = EventBroadcaster()
        broadcaster.set_state("leader-host", "1")

        _, current = broadcaster.subscribe()

        assert current == "1"

    def test_subscribe_before_any_state_is_set_returns_none(self):
        broadcaster = EventBroadcaster()

        _, current = broadcaster.subscribe()

        assert current is None

    def test_set_state_broadcasts_to_existing_subscribers(self):
        broadcaster = EventBroadcaster()
        q, _ = broadcaster.subscribe()

        broadcaster.set_state("leader-host", "2")

        assert q.get_nowait() == format_sse("leader-host", "2")

    def test_set_state_updates_state_visible_to_new_subscribers(self):
        broadcaster = EventBroadcaster()
        broadcaster.set_state("leader-host", "1")

        broadcaster.set_state("leader-host", "2")
        _, current = broadcaster.subscribe()

        assert current == "2"

    def test_broadcast_reaches_all_subscribers(self):
        broadcaster = EventBroadcaster()
        q1, _ = broadcaster.subscribe()
        q2, _ = broadcaster.subscribe()

        broadcaster.broadcast("host-connected", "3")

        assert q1.get_nowait() == format_sse("host-connected", "3")
        assert q2.get_nowait() == format_sse("host-connected", "3")

    def test_broadcast_can_exclude_one_subscriber(self):
        broadcaster = EventBroadcaster()
        q1, _ = broadcaster.subscribe()
        q2, _ = broadcaster.subscribe()

        broadcaster.broadcast("host-connected", "3", exclude=q1)

        assert q1.empty()
        assert q2.get_nowait() == format_sse("host-connected", "3")

    def test_unsubscribe_stops_future_broadcasts(self):
        broadcaster = EventBroadcaster()
        q, _ = broadcaster.subscribe()

        broadcaster.unsubscribe(q)
        broadcaster.set_state("leader-host", "1")

        assert q.empty()

    def test_unsubscribe_is_idempotent(self):
        broadcaster = EventBroadcaster()
        q, _ = broadcaster.subscribe()

        broadcaster.unsubscribe(q)
        broadcaster.unsubscribe(q)  # should not raise
