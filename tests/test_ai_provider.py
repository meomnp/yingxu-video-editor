from __future__ import annotations

import json
import socket
import ssl
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from local_slice_assistant.ai_provider import (
    ProviderCancelled,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderError,
    ProviderTimeout,
    PartialCompletionError,
    _StreamingReply,
    _payload,
    completion_url,
    request_completion,
)


def response_json(content="剪辑方案：第二集在前，第一集在后。", **choice_fields):
    choice = {"finish_reason": "stop", "message": {"role": "assistant", "content": content}}
    choice.update(choice_fields)
    return json.dumps({"choices": [choice]}, ensure_ascii=False).encode("utf-8")


@contextmanager
def local_server(*, data=None, status=200, headers=None, delay=0, chunked=False, raw_length=None, body_delay=0, content_type="application/json", tail_delay=0):
    records = []
    received = threading.Event()
    reply = response_json() if data is None else data

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass  # Never let a test accidentally print an Authorization header.

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            current_reply = reply(json.loads(body)) if callable(reply) else reply
            records.append({"path": self.path, "body": body, "headers": dict(self.headers)})
            received.set()
            if delay:
                time.sleep(delay)
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                if chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                elif raw_length is not None:
                    self.send_header("Content-Length", raw_length)
                else:
                    self.send_header("Content-Length", str(len(current_reply)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if body_delay:
                    self.wfile.flush()
                    time.sleep(body_delay)
                if chunked:
                    for start in range(0, len(current_reply), 13):
                        piece = current_reply[start:start + 13]
                        self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    self.wfile.write(current_reply)
                self.wfile.flush()
                if tail_delay:
                    time.sleep(tail_delay)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", records, received
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


MESSAGES = [{"role": "system", "content": "只返回剪辑方案"},
            {"role": "user", "content": "这是明确选择的台词文字，不是视频"}]


class StreamingTests(unittest.TestCase):
    @staticmethod
    def event(content=None, reasoning=None, finish=None):
        delta = {}
        if content is not None:
            delta['content'] = content
        if reasoning is not None:
            delta['reasoning_content'] = reasoning
        return ('data: ' + json.dumps({'id': 'stream-test', 'choices': [
            {'index': 0, 'delta': delta, 'finish_reason': finish}]}, ensure_ascii=False)
            + '\r\n\r\n').encode('utf-8')

    def test_fragmented_utf8_keepalive_usage_and_done(self):
        progress = []
        reply = _StreamingReply(progress.append)
        body = (b': keep-alive\r\n\r\n' + self.event(reasoning='思考中')
                + self.event(content='完整设计稿', finish='stop')
                + b'data: {"choices":[],"usage":{"prompt_tokens":23,"completion_tokens":9}}\n\n'
                + b'data: [DONE]\n\n')
        for byte in body:
            reply.feed(bytes([byte]))
        result = reply.result()
        self.assertEqual(result, '完整设计稿')
        self.assertEqual(result.diagnostics['usage']['completion_tokens'], 9)
        self.assertEqual(reply.reasoning_characters, 3)
        self.assertEqual(progress[-1]['content'], result)

    def test_interruption_preserves_text_but_never_marks_complete(self):
        for error, reason in [(ProviderTimeout('超时'), 'timeout'),
                              (ProviderCancelled('取消'), 'cancelled'),
                              (ProviderError('断开'), 'stream_interrupted')]:
            reply = _StreamingReply()
            reply.feed(self.event(content='已经收到的正文'))
            partial = reply.preserve_error(error)
            self.assertIsInstance(partial, PartialCompletionError)
            self.assertEqual(partial.content, '已经收到的正文')
            self.assertEqual(partial.finish_reason, reason)
            with self.assertRaises(PartialCompletionError):
                reply.result()

    def test_thinking_only_timeout_is_not_a_design(self):
        reply = _StreamingReply()
        reply.feed(self.event(reasoning='只有思考'))
        error = ProviderTimeout('超时')
        self.assertIs(reply.preserve_error(error), error)
        self.assertEqual(reply.content, '')

    def test_actual_http_stream_no_second_request(self):
        data = self.event(content='方案正文', finish='stop') + b'data: [DONE]\n\n'
        with local_server(data=data, content_type='text/event-stream', chunked=True) as (url, records, _):
            result = request_completion(ProviderConfig(url, 'test-model', 'test-key', stream=True), MESSAGES)
        self.assertEqual(result, '方案正文')
        self.assertEqual(len(records), 1)
        payload = json.loads(records[0]['body'])
        self.assertTrue(payload['stream'])
        self.assertEqual(payload['stream_options'], {'include_usage': True})

    def test_actual_http_disconnection_saves_partial(self):
        with local_server(data=self.event(content='不完整正文'), content_type='text/event-stream') as (url, records, _):
            with self.assertRaises(PartialCompletionError) as caught:
                request_completion(ProviderConfig(url, 'test-model', 'test-key', stream=True), MESSAGES)
        self.assertEqual(caught.exception.content, '不完整正文')
        self.assertEqual(len(records), 1)

    def test_json_compatibility_and_expanded_default_wait(self):
        with local_server() as (url, records, _):
            config = ProviderConfig(url, 'test-model', 'test-key', stream=True)
            self.assertEqual(config.timeout_seconds, 7200)
            result = request_completion(config, MESSAGES)
        self.assertTrue(result)
        self.assertEqual(len(records), 1)

    def test_real_socket_timeout_after_visible_text_preserves_it(self):
        with local_server(data=self.event(content='超时前已收到'), content_type='text/event-stream',
                          raw_length='99999', tail_delay=0.3) as (url, records, _):
            with self.assertRaises(PartialCompletionError) as caught:
                request_completion(ProviderConfig(url, 'model', 'key', stream=True, timeout_seconds=0.12), MESSAGES)
        self.assertEqual(caught.exception.content, '超时前已收到')
        self.assertEqual(caught.exception.finish_reason, 'timeout')
        self.assertEqual(len(records), 1)

    def test_real_socket_cancellation_after_text_preserves_it(self):
        cancel = threading.Event()
        def progress(details):
            if details['content']:
                cancel.set()
        with local_server(data=self.event(content='取消前已收到'), content_type='text/event-stream',
                          raw_length='99999', tail_delay=0.3) as (url, records, _):
            with self.assertRaises(PartialCompletionError) as caught:
                request_completion(ProviderConfig(url, 'model', 'key', stream=True,
                    progress_callback=progress), MESSAGES, cancel_event=cancel)
        self.assertEqual(caught.exception.content, '取消前已收到')
        self.assertEqual(caught.exception.finish_reason, 'cancelled')
        self.assertEqual(len(records), 1)


class ProviderConfigTests(unittest.TestCase):
    def test_supported_urls_normalize(self):
        cases = {
            "https://api.deepseek.com": "https://api.deepseek.com/chat/completions",
            "https://api.example.com/v1/": "https://api.example.com/v1/chat/completions",
            "https://api.example.com/v1/chat/completions/": "https://api.example.com/v1/chat/completions",
            "http://localhost:8123/v1": "http://localhost:8123/v1/chat/completions",
            "http://[::1]:1234": "http://[::1]:1234/chat/completions",
            "http://127.0.0.2": "http://127.0.0.2/chat/completions",
        }
        for supplied, expected in cases.items():
            with self.subTest(url=supplied):
                self.assertEqual(completion_url(ProviderConfig(supplied, "chosen-model", "key")), expected)

    def test_unsafe_urls_rejected_before_connection(self):
        bad_urls = [
            "http://api.deepseek.com", "http://localhost.evil.test", "http://0.0.0.0",
            "http://192.168.1.1", "http://127.1", "http://2130706433", "http://[::ffff:192.168.1.1]",
            "https://user:password@example.com", "https://user@example.com", "https://example.com?key=secret",
            "https://example.com?", "https://example.com#", "https://example.com/#fragment",
            "https://example.com\n", " https://example.com", "https://example.com/with space",
            "file:///secret.txt", "javascript:alert(1)", "ftp://example.com", "//example.com",
            "https://example.com\\other", "https://example.com:65536", "https://example.com:0",
            "http://localhost.", "http://local%68ost", "https://example..com", "https://-bad.com",
            "https://[::1%25eth0]", "https://example.com/../v1", "https://example.com/%2fsecret",
            "https://example.com/v1//api", "https://", "", None,
        ]
        with patch("http.client.HTTPConnection.connect") as connect:
            for url in bad_urls:
                with self.subTest(url=url):
                    with self.assertRaises(ProviderConfigurationError):
                        ProviderConfig(url, "model", "secret")
            connect.assert_not_called()

    def test_credentials_hidden_and_not_accepted_in_headers(self):
        self.assertNotIn("my-secret-key", repr(ProviderConfig("https://example.com", "model", "my-secret-key")))
        for bad in ["", "hello world", "key\r\nX-Extra: 1", "非ASCII", None]:
            with self.subTest(key_type=type(bad).__name__):
                with self.assertRaises(ProviderConfigurationError) as caught:
                    ProviderConfig("https://example.com", "model", bad)
                if bad:
                    self.assertNotIn(bad, str(caught.exception))
        # Keyless local models are supported, without transmitting Authorization.
        self.assertEqual(ProviderConfig("http://localhost", "model", "").api_key, "")

    def test_configuration_limits(self):
        for overrides in [{"timeout_seconds": float("nan")}, {"timeout_seconds": float("inf")},
                          {"timeout_seconds": True}, {"timeout_seconds": 0}, {"timeout_seconds": 7201},
                          {"model": ""}, {"model": "a\n"}, {"max_response_bytes": 0},
                          {"max_request_bytes": 20 * 1024 * 1024}, {"max_output_tokens": True},
                          {"max_output_tokens": 0}, {"thinking_mode": "unrecognized"}]:
            values = {"base_url": "https://example.com", "model": "m", "api_key": "secret", **overrides}
            with self.subTest(overrides=overrides), self.assertRaises(ProviderConfigurationError):
                ProviderConfig(**values)


class ProviderRequestTests(unittest.TestCase):
    def test_deepseek_defaults_to_official_maximum_not_8k(self):
        config = ProviderConfig("https://api.deepseek.com", "deepseek-flash", "local-test-key")
        payload = json.loads(_payload(config, MESSAGES))
        self.assertEqual(payload["max_tokens"], 393216)
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "low")

    def test_deepseek_thinking_can_be_disabled_and_uses_configured_output_cap(self):
        config = ProviderConfig("https://api.deepseek.com", "deepseek-flash", "local-test-key",
                                max_output_tokens=393216, thinking_mode="disabled")
        payload = json.loads(_payload(config, MESSAGES))
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["max_tokens"], 393216)

    def test_deepseek_thinking_effort_is_explicit_and_other_hosts_are_untouched(self):
        for effort in ("low", "high", "max"):
            payload = json.loads(_payload(ProviderConfig(
                "https://api.deepseek.com", "deepseek-flash", "local-test-key", thinking_mode=effort
            ), MESSAGES))
            self.assertEqual(payload["thinking"], {"type": "enabled"})
            self.assertEqual(payload["reasoning_effort"], effort)
        payload = json.loads(_payload(ProviderConfig(
            "https://example.com", "model", "local-test-key", thinking_mode="max"
        ), MESSAGES))
        self.assertNotIn("thinking", payload)
        self.assertNotIn("reasoning_effort", payload)

    def test_deepseek_rejects_values_above_its_official_output_maximum(self):
        config = ProviderConfig("https://api.deepseek.com", "deepseek-flash", "local-test-key",
                                max_output_tokens=393217)
        with self.assertRaisesRegex(ProviderConfigurationError, "384K"):
            _payload(config, MESSAGES)

    def test_other_providers_can_use_their_own_output_limits(self):
        config = ProviderConfig("https://example.com", "chosen-model", "local-test-key",
                                max_output_tokens=500000)
        payload = json.loads(_payload(config, MESSAGES))
        self.assertEqual(payload["max_tokens"], 500000)

    def test_generic_compatible_provider_leaves_output_policy_to_provider(self):
        with local_server() as (url, records, _), patch.dict("os.environ", {"HTTP_PROXY": "http://bad.invalid:3"}):
            result = request_completion(ProviderConfig(url + "/v1", "deepseek-flash", "local-test-key"), MESSAGES)
        self.assertIn("第二集", result)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["path"], "/v1/chat/completions")
        self.assertEqual(record["headers"]["Authorization"], "Bearer local-test-key")
        self.assertEqual(record["headers"]["Accept-Encoding"], "identity")
        payload = json.loads(record["body"])
        self.assertEqual(payload, {"model": "deepseek-flash", "messages": MESSAGES,
                                   "stream": False})
        self.assertNotIn("local-test-key", record["body"].decode())

    def test_local_keyless_and_complete_endpoint(self):
        with local_server() as (url, records, _):
            request_completion(ProviderConfig(url + "/chat/completions", "local", ""), MESSAGES)
        self.assertNotIn("Authorization", records[0]["headers"])
        self.assertEqual(records[0]["path"], "/chat/completions")

    def test_refuses_attachments_empty_text_and_unknown_message_fields(self):
        invalid = [[], "not a list", [{"role": "user", "content": [{"type": "image_url", "url": "x"}]}],
                   [{"role": "user", "content": " "}], [{"role": "tool", "content": "x"}],
                   [{"role": [], "content": "x"}], [{"role": "user", "content": "x", "file": "x.mp4"}]]
        with local_server() as (url, records, _):
            config = ProviderConfig(url, "model", "key")
            for messages in invalid:
                with self.subTest(messages=messages), self.assertRaises(ProviderConfigurationError):
                    request_completion(config, messages)
            self.assertEqual(records, [])

    def test_request_size_checked_before_send(self):
        with local_server() as (url, records, _):
            with self.assertRaisesRegex(ProviderConfigurationError, "大小上限"):
                request_completion(ProviderConfig(url, "model", "key", max_request_bytes=10), MESSAGES)
            self.assertEqual(records, [])

    def test_redirect_does_not_forward_key_or_retry(self):
        with local_server() as (target_url, target_records, _):
            for status in [301, 302, 303, 307, 308]:
                with local_server(status=status, headers={"Location": target_url + "/other"}) as (url, records, _):
                    with self.subTest(status=status), self.assertRaisesRegex(ProviderError, "重定向"):
                        request_completion(ProviderConfig(url, "m", "never-forward"), MESSAGES)
                    self.assertEqual(len(records), 1)
            self.assertEqual(target_records, [])

    def test_http_errors_do_not_leak_remote_body_and_no_retry(self):
        for status in [400, 401, 402, 403, 404, 429, 500, 503]:
            with local_server(status=status, data=b'{"error":"super-secret-key"}') as (url, records, _):
                with self.subTest(status=status), self.assertRaises(ProviderError) as caught:
                    request_completion(ProviderConfig(url, "m", "super-secret-key"), MESSAGES)
                self.assertIn(str(status), str(caught.exception))
                self.assertNotIn("super-secret-key", str(caught.exception))
                self.assertEqual(len(records), 1)

    def test_declared_and_chunked_size_limits(self):
        for chunked in [False, True]:
            with local_server(data=b"x" * 3000, chunked=chunked) as (url, records, _):
                with self.subTest(chunked=chunked), self.assertRaisesRegex(ProviderError, "大小上限"):
                    request_completion(ProviderConfig(url, "m", "k", max_response_bytes=200), MESSAGES)
                self.assertEqual(len(records), 1)

    def test_chunked_success_and_truncated_length_fail(self):
        with local_server(chunked=True) as (url, _, _):
            self.assertIn("第二集", request_completion(ProviderConfig(url, "m", "k"), MESSAGES))
        with local_server(data=response_json(), raw_length="9999") as (url, _, _):
            with self.assertRaisesRegex(ProviderError, "不完整"):
                request_completion(ProviderConfig(url, "m", "k"), MESSAGES)

    def test_bad_or_unfinished_result_never_returned(self):
        bad = [b"not-json", b"[]", b'{"error":{"message":"secret"}}', b'{"choices":[]}',
               response_json(finish_reason="length"), response_json(finish_reason="content_filter"),
               response_json(message={"role": "assistant", "content": None, "reasoning_content": "reasoning only"}),
               response_json(message={"role": "assistant", "content": "x", "tool_calls": [{"name": "exec"}]}),
               response_json(message={"role": "user", "content": "x"}), response_json(" ")]
        for data in bad:
            with local_server(data=data) as (url, _, _):
                with self.subTest(data=data[:40]), self.assertRaises(ProviderError):
                    request_completion(ProviderConfig(url, "m", "k"), MESSAGES)

    def test_reject_compressed_response(self):
        with local_server(headers={"Content-Encoding": "gzip"}) as (url, _, _):
            with self.assertRaisesRegex(ProviderError, "压缩"):
                request_completion(ProviderConfig(url, "m", "k"), MESSAGES)

    def test_already_cancelled_sends_nothing(self):
        cancel = threading.Event()
        cancel.set()
        with local_server() as (url, records, _):
            with self.assertRaises(ProviderCancelled):
                request_completion(ProviderConfig(url, "m", "k"), MESSAGES, cancel)
            self.assertEqual(records, [])

    def test_cancel_while_waiting_for_headers_closes_connection(self):
        cancel = threading.Event()
        with local_server(delay=2) as (url, records, received):
            def set_cancel():
                received.wait(1)
                cancel.set()
            trigger = threading.Thread(target=set_cancel, daemon=True)
            trigger.start()
            started = time.monotonic()
            with self.assertRaises(ProviderCancelled) as caught:
                request_completion(ProviderConfig(url, "m", "k", timeout_seconds=5), MESSAGES, cancel)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertIn("服务端可能", str(caught.exception))
            self.assertEqual(len(records), 1)
            trigger.join(timeout=1)

    def test_timeout_does_not_retry(self):
        with local_server(delay=2) as (url, records, _):
            started = time.monotonic()
            with self.assertRaises(ProviderTimeout):
                request_completion(ProviderConfig(url, "m", "k", timeout_seconds=0.15), MESSAGES)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(len(records), 1)

    def test_cancel_during_body_receive(self):
        cancel = threading.Event()
        with local_server(body_delay=2) as (url, records, received):
            def set_cancel():
                received.wait(1)
                time.sleep(0.1)
                cancel.set()
            trigger = threading.Thread(target=set_cancel, daemon=True)
            trigger.start()
            started = time.monotonic()
            with self.assertRaises(ProviderCancelled):
                request_completion(ProviderConfig(url, "m", "k", timeout_seconds=5), MESSAGES, cancel)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(len(records), 1)
            trigger.join(timeout=1)

    def test_dns_wait_is_cancellable_and_has_not_sent(self):
        cancel = threading.Event()
        resolving = threading.Event()
        release = threading.Event()
        def blocked_dns(*_args, **_kwargs):
            resolving.set()
            release.wait(2)
            raise OSError("test lookup stopped")
        def set_cancel():
            resolving.wait(1)
            cancel.set()
        trigger = threading.Thread(target=set_cancel, daemon=True)
        with patch("socket.getaddrinfo", side_effect=blocked_dns), patch("socket.socket") as create_socket:
            trigger.start()
            started = time.monotonic()
            try:
                with self.assertRaises(ProviderCancelled):
                    request_completion(ProviderConfig("https://example.com", "m", "k"), MESSAGES, cancel)
                self.assertLess(time.monotonic() - started, 1.5)
                create_socket.assert_not_called()
            finally:
                release.set()
                trigger.join(timeout=1)

    def test_http_localhost_dns_cannot_resolve_to_remote(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 80))]
        with patch("socket.getaddrinfo", return_value=addresses), patch("socket.socket") as create_socket:
            with self.assertRaisesRegex(ProviderConfigurationError, "非回环"):
                request_completion(ProviderConfig("http://localhost", "m", "k"), MESSAGES)
            create_socket.assert_not_called()

    def test_https_uses_verified_context_and_never_falls_back_to_plaintext(self):
        real_context = ssl.create_default_context()
        self.assertTrue(real_context.check_hostname)
        self.assertEqual(real_context.verify_mode, ssl.CERT_REQUIRED)
        with local_server() as (url, records, _), patch("ssl.create_default_context") as create_context:
            # Fail TLS verification before HTTP; no Authorization/body reaches the server.
            create_context.return_value.wrap_socket.side_effect = ssl.SSLCertVerificationError("key-must-not-leak")
            with self.assertRaises(ProviderError) as caught:
                request_completion(ProviderConfig(url.replace("http:", "https:"), "m", "key-must-not-leak"), MESSAGES)
            self.assertNotIn("key-must-not-leak", str(caught.exception))
            self.assertEqual(records, [])
            kwargs = create_context.return_value.wrap_socket.call_args.kwargs
            self.assertEqual(kwargs["server_hostname"], "127.0.0.1")
            self.assertFalse(kwargs["do_handshake_on_connect"])


if __name__ == "__main__":
    unittest.main()
