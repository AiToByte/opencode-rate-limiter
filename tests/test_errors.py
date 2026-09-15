"""Unified error classification tests (errors.py)."""

from opencode_rate_limiter.errors import (
    classify_http,
    classify_opencode_log_line,
    classify_transport,
    explain_kind,
)


class TestClassifyHttp:
    def test_429_wins_over_body(self):
        r = classify_http(429, "FreeUsageLimitError", None)
        assert r.kind == "rate_limited"

    def test_429_unknown_type_normalized(self):
        r = classify_http(429, None, None)
        assert r.kind == "rate_limited"
        assert r.normalized_type == "RateLimitUnknown"

    def test_non_429_rate_message_falls_back_to_limited(self):
        r = classify_http(400, None, "Rate limit exceeded. Please try again later.")
        assert r.kind == "rate_limited"
        assert r.normalized_type == "RateLimitUnknown"

    def test_reasoning_markers_win_over_upstream(self):
        r = classify_http(
            400,
            "invalid_request_error",
            "Upstream request failed: reasoning `encrypted_content` was not issued to this caller",
        )
        assert r.kind == "reasoning_replay"

    def test_upstream_without_reasoning(self):
        r = classify_http(500, None, "Upstream request failed: timeout")
        assert r.kind == "upstream"

    def test_5xx_is_server(self):
        r = classify_http(502, None, "Bad Gateway")
        assert r.kind == "server"

    def test_case_insensitive(self):
        r = classify_http(None, None, "RATE LIMIT EXCEEDED")
        assert r.kind == "rate_limited"

    def test_unknown_stays_unknown(self):
        r = classify_http(400, "MissingSessionID", "nope")
        assert r.kind == "unknown"


class TestClassifyTransport:
    def test_socket_closed_is_transient(self):
        r = classify_transport(
            "Cannot connect to API: The socket connection was closed unexpectedly."
        )
        assert r.kind == "transient_transport"

    def test_econnreset_is_transient(self):
        assert classify_transport("ECONNRESET read failed").kind == "transient_transport"

    def test_reasoning_in_pasted_line_wins(self):
        r = classify_transport("socket closed ... encrypted_content was not issued")
        assert r.kind == "reasoning_replay"

    def test_empty_is_unknown(self):
        assert classify_transport(None).kind == "unknown"
        assert classify_transport("").kind == "unknown"


class TestClassifyLogLine:
    def test_three_real_world_lines(self):
        e1 = (
            "Cannot connect to API: The socket connection was closed unexpectedly. "
            "For more information, pass `verbose: true` in fetch()"
        )
        e2 = "Error from provider (Console): Rate limit exceeded. Please try again later."
        e3 = (
            "Error from provider (Console): Upstream request failed: "
            "[invalid_request_error] reasoning `encrypted_content` was not issued"
        )
        assert classify_opencode_log_line(e1).kind == "transient_transport"
        assert classify_opencode_log_line(e2).kind == "rate_limited"
        assert classify_opencode_log_line(e3).kind == "reasoning_replay"

    def test_empty_is_unknown(self):
        assert classify_opencode_log_line("").kind == "unknown"


class TestExplainCopy:
    def test_all_kinds_have_copy_without_secrets(self):
        from opencode_rate_limiter.errors import ErrorKind

        kinds: list[ErrorKind] = [
            "rate_limited",
            "transient_transport",
            "reasoning_replay",
            "upstream",
            "auth",
            "server",
            "unknown",
        ]
        for kind in kinds:
            title, detail, remedy = explain_kind(kind)
            assert title and detail and remedy
