"""Opt-in, text-only Chat Completions transport (no SDK, retries or persistence).

The caller is responsible for showing the exact messages and getting explicit
consent before calling this function. This module never reads media or files,
executes model output, follows redirects, or consults proxy/environment secrets.
Keys live only in memory; do not serialize ``ProviderConfig`` into project files.

Protocol checked against https://api-docs.deepseek.com/api/create-chat-completion/
on 2026-09-30. Providers must implement the same non-streaming text contract.
"""

from __future__ import annotations

import http.client
import errno
import io
import ipaddress
import json
import math
import re
import select
import socket
import ssl
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit


class ProviderError(RuntimeError):
    """A sanitized error, safe to show without reflecting remote bodies/keys."""


class ProviderConfigurationError(ProviderError, ValueError):
    """Invalid local configuration; no request has been sent."""


class ProviderCancelled(ProviderError):
    """Client stopped waiting; the server may still run and charge the request."""


class ProviderTimeout(ProviderError):
    """Client deadline expired; do not automatically repeat a billable request."""


class PartialCompletionError(ProviderError):
    """A billable response arrived but ended before the provider completed it."""

    def __init__(self, content: str, finish_reason: str, diagnostics=None):
        super().__init__(f"模型返回了未完成内容（结束原因：{finish_reason}）。")
        self.content = content
        self.finish_reason = finish_reason
        self.diagnostics = diagnostics or {}


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 120.0
    max_request_bytes: int = 4 * 1024 * 1024
    max_response_bytes: int = 8 * 1024 * 1024
    max_output_tokens: int = 8192

    def __post_init__(self) -> None:
        completion_url(self)
        if not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 200:
            raise ProviderConfigurationError("请填写有效的模型名称（不超过 200 字符）。")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.model):
            raise ProviderConfigurationError("模型名称不能包含控制字符。")
        if not isinstance(self.api_key, str) or len(self.api_key) > 4096:
            raise ProviderConfigurationError("API Key 格式无效。")
        # Headers are ASCII; reject whitespace rather than silently trimming a key.
        if any(ord(char) < 33 or ord(char) > 126 for char in self.api_key):
            raise ProviderConfigurationError("API Key 不能包含空格、换行或非 ASCII 字符。")
        if not self.api_key and urlsplit(self.base_url).scheme.lower() == "https":
            raise ProviderConfigurationError("请填写 API Key；密钥只在本次窗口内使用，不会保存。")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (float, int)):
            raise ProviderConfigurationError("超时必须是 0.1 到 600 秒之间的数字。")
        if not math.isfinite(self.timeout_seconds) or not 0.1 <= self.timeout_seconds <= 600:
            raise ProviderConfigurationError("超时必须是 0.1 到 600 秒之间的数字。")
        for value, ceiling in ((self.max_request_bytes, 16 * 1024 * 1024),
                               (self.max_response_bytes, 32 * 1024 * 1024),
                               (self.max_output_tokens, 393216)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
                raise ProviderConfigurationError("请求、响应或输出长度上限无效。")


def completion_url(config: ProviderConfig) -> str:
    """Validate and normalize a base URL, also accepting the complete endpoint.

    Cleartext HTTP is limited to exact localhost or literal loopback addresses.
    No redirects are ever followed, including same-origin redirects.
    """
    value = config.base_url
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ProviderConfigurationError("API 地址为空或过长。")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise ProviderConfigurationError("API 地址不能包含空白、控制字符或反斜线。")
    if "?" in value or "#" in value:
        raise ProviderConfigurationError("API 地址不能携带查询参数或片段；密钥请填入独立字段。")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise ProviderConfigurationError("API 地址或端口格式无效。") from None
    if parsed.scheme not in {"http", "https"} or not host:
        raise ProviderConfigurationError("API 地址必须是 HTTPS；本机服务可用 HTTP。")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ProviderConfigurationError("API 地址不能包含用户名、密码或密钥。")
    if port is not None and not 1 <= port <= 65535:
        raise ProviderConfigurationError("API 端口必须在 1 到 65535 之间。")
    if "%" in host or host.endswith("."):
        raise ProviderConfigurationError("API 主机名不支持转义或尾随句点。")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise ProviderConfigurationError("请使用有效的 ASCII API 主机名。") from None
        if any(not part or len(part) > 63 or part.startswith("-") or part.endswith("-")
               for part in host.split(".")):
            raise ProviderConfigurationError("API 主机名格式无效。")
    if parsed.scheme == "http" and not (host == "localhost" or literal and literal.is_loopback):
        raise ProviderConfigurationError("远程 API 必须使用 HTTPS；HTTP 只允许本机回环地址。")
    # Restrict paths to an unambiguous API prefix (no escapes or traversal).
    path = parsed.path.rstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9/_.-]*", path) or "//" in path or any(
        part in {".", ".."} for part in path.split("/")
    ):
        raise ProviderConfigurationError("API 路径不能包含转义、空格或相对路径片段。")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    authority = f"[{host}]" if literal and literal.version == 6 else host
    if port is not None:
        authority += f":{port}"
    return urlunsplit((parsed.scheme, authority, path, "", ""))


def _payload(config: ProviderConfig, messages: Sequence[Mapping[str, str]]) -> bytes:
    if not isinstance(messages, (list, tuple)) or not messages or len(messages) > 1000:
        raise ProviderConfigurationError("分析内容必须是 1 到 1000 条纯文本消息。")
    copied = []
    for message in messages:
        if not isinstance(message, Mapping) or set(message) != {"role", "content"}:
            raise ProviderConfigurationError("分析消息只支持 role 和 content，不支持文件或图片附件。")
        if not isinstance(message["role"], str) or message["role"] not in {"system", "user", "assistant"}:
            raise ProviderConfigurationError("分析消息的角色无效；不执行模型工具调用。")
        if not isinstance(message["content"], str) or not message["content"].strip():
            raise ProviderConfigurationError("分析消息必须包含非空纯文本。")
        copied.append({"role": message["role"], "content": message["content"]})
    request = {"model": config.model, "messages": copied, "stream": False,
               "max_tokens": config.max_output_tokens}
    # DeepSeek enables reasoning by default. In the failed requests all
    # completion tokens were reasoning_tokens and content was empty. Disable
    # reasoning only for the official DeepSeek endpoint; do not send this
    # provider-specific field to other OpenAI-compatible services.
    if (urlsplit(config.base_url).hostname or "").lower() == "api.deepseek.com":
        request["thinking"] = {"type": "disabled"}
    try:
        body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    except (UnicodeError, ValueError):
        raise ProviderConfigurationError("分析文本不是有效的 UTF-8 文本。") from None
    if len(body) > config.max_request_bytes:
        raise ProviderConfigurationError("分析文本超过请求大小上限，请缩小素材或台词范围。")
    return body


def request_size_bytes(config: ProviderConfig, messages: Sequence[Mapping[str, str]]) -> int:
    """Exact encoded request-body bytes; local validation only, no network."""
    return len(_payload(config, messages))


def _check_interruption(cancel_event: threading.Event | None, deadline: float) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ProviderCancelled("已停止本地等待。服务端可能仍在生成并计费；不会自动重试。")
    if time.monotonic() >= deadline:
        raise ProviderTimeout("API 请求超时。服务端可能仍在生成并计费；不会自动重试。")


def _extract_content(data: bytes) -> str:
    try:
        decoded = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError):
        raise ProviderError("API 返回的不是有效 JSON；未导入任何方案。") from None
    if not isinstance(decoded, dict) or "error" in decoded:
        raise ProviderError("API 返回错误或不兼容的数据；未导入任何方案。")
    choices = decoded.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ProviderError("API 未返回唯一的文本结果；请检查模型与接口类型。")
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    message = choice.get("message")
    usage = decoded.get("usage")
    usage_summary = {}
    if isinstance(usage, dict):
        for key, value in usage.items():
            if isinstance(value, int) and not isinstance(value, bool):
                usage_summary[key] = value
            elif isinstance(value, dict):
                nested = {name: number for name, number in value.items()
                          if isinstance(number, int) and not isinstance(number, bool)}
                if nested:
                    usage_summary[key] = nested
    diagnostics = {
        "response_id": decoded.get("id") if isinstance(decoded.get("id"), str) else "",
        "model": decoded.get("model") if isinstance(decoded.get("model"), str) else "",
        "usage": usage_summary,
    }
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ProviderError("API 结果不是普通助手文本；不执行工具调用。")
    content = message.get("content")
    if message.get("tool_calls"):
        if isinstance(content, str) and content.strip():
            raise PartialCompletionError(content, "tool_calls", diagnostics)
        raise ProviderError("模型尝试调用工具；本工具不执行工具调用。")
    if finish_reason != "stop":
        raise PartialCompletionError(content if isinstance(content, str) else "",
                                     str(finish_reason or "unknown"), diagnostics)
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("API 正常结束但没有返回可用正文；仅思考内容不能作为剪辑方案。")
    return content


class _InterruptibleSocket:
    """Nonblocking socket with a blocking, cancellation-aware HTTP file facade.

    Retrying *socket readiness*, unlike retrying HTTP requests, cannot duplicate
    a generation. The wrapper keeps socket lifetime independent of HTTP's file
    references, including servers which reply with Connection: close.
    """

    def __init__(self, sock: socket.socket, cancel: threading.Event | None, deadline: float):
        self.sock = sock
        self.cancel = cancel
        self.deadline = deadline
        self.files = 0
        self.closed = False

    def wait(self, *, write: bool = False) -> None:
        while True:
            _check_interruption(self.cancel, self.deadline)
            readable, writable, _ = select.select(
                [] if write else [self.sock], [self.sock] if write else [], [],
                min(0.05, max(0, self.deadline - time.monotonic())),
            )
            if readable or writable:
                return

    def recv_into(self, buffer) -> int:
        while True:
            _check_interruption(self.cancel, self.deadline)
            try:
                return self.sock.recv_into(buffer)
            except ssl.SSLWantWriteError:
                self.wait(write=True)
            except (BlockingIOError, ssl.SSLWantReadError):
                self.wait()

    def sendall(self, data) -> None:
        remaining = memoryview(data)
        while remaining:
            _check_interruption(self.cancel, self.deadline)
            try:
                count = self.sock.send(remaining)
                if count == 0:
                    raise ConnectionError("closed")
                remaining = remaining[count:]
            except ssl.SSLWantReadError:
                self.wait()
            except (BlockingIOError, ssl.SSLWantWriteError):
                self.wait(write=True)

    def makefile(self, mode: str):
        if mode != "rb":
            raise ValueError("unsupported HTTP file mode")
        self.files += 1
        return io.BufferedReader(_ResponseReader(self))

    def close(self) -> None:
        self.closed = True
        if not self.files:
            self.sock.close()


class _ResponseReader(io.RawIOBase):
    def __init__(self, owner: _InterruptibleSocket):
        super().__init__()
        self.owner = owner

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        return self.owner.recv_into(buffer)

    def close(self) -> None:
        if not self.closed:
            self.owner.files -= 1
            if self.owner.closed and not self.owner.files:
                self.owner.sock.close()
        super().close()


def _connect(host: str, port: int, secure: bool, cancel: threading.Event | None, deadline: float):
    addresses: list = []
    resolution_done = threading.Event()
    resolution_failed = threading.Event()

    # OS DNS cannot always be interrupted. This short-lived daemon knows only
    # hostname/port, never credentials/messages; the caller can still exit.
    def resolve() -> None:
        try:
            addresses.extend(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError:
            resolution_failed.set()
        finally:
            resolution_done.set()

    threading.Thread(target=resolve, name="ai-provider-dns", daemon=True).start()
    while not resolution_done.wait(0.05):
        _check_interruption(cancel, deadline)
    _check_interruption(cancel, deadline)
    if resolution_failed.is_set():
        raise OSError("DNS lookup failed")
    pending_codes = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, 10035, 10036, 10037}
    for family, kind, protocol, _, address in addresses[:8]:
        if not secure and not ipaddress.ip_address(address[0]).is_loopback:
            raise ProviderConfigurationError("本机 HTTP 地址解析到了非回环地址，已拒绝发送。")
        sock = socket.socket(family, kind, protocol)
        wrapped = _InterruptibleSocket(sock, cancel, deadline)
        try:
            sock.setblocking(False)
            error = sock.connect_ex(address)
            if error in pending_codes:
                wrapped.wait(write=True)
                error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error:
                sock.close()
                continue  # Only try another DNS address before sending any bytes.
            if secure:
                tls = ssl.create_default_context().wrap_socket(sock, server_hostname=host, do_handshake_on_connect=False)
                wrapped.sock = tls
                while True:
                    _check_interruption(cancel, deadline)
                    try:
                        tls.do_handshake()
                        break
                    except ssl.SSLWantReadError:
                        wrapped.wait()
                    except ssl.SSLWantWriteError:
                        wrapped.wait(write=True)
            return wrapped
        except BaseException:
            wrapped.close()
            raise
    raise OSError("Unable to connect")


def request_completion(
    config: ProviderConfig,
    messages: Sequence[Mapping[str, str]],
    cancel_event: threading.Event | None = None,
) -> str:
    """Send one explicitly authorized, bounded text request, without retries.

    Run on the GUI's worker thread. Cancellation closes the local connection;
    it does not promise server-side cancellation/refunds. Nonblocking I/O checks
    cancellation/deadline every 50 ms. OS DNS may finish later in a separate
    daemon with hostname only; it never holds request content or credentials.
    """
    url = completion_url(config)
    body = _payload(config, messages)
    deadline = time.monotonic() + config.timeout_seconds
    _check_interruption(cancel_event, deadline)
    parsed = urlsplit(url)
    # HTTPConnection only formats/parses HTTP. _connect supplies a verified TLS
    # socket for HTTPS, bypassing environment proxies and redirect handlers.
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection = http.client.HTTPConnection(parsed.hostname, port, timeout=config.timeout_seconds)
    response = None
    try:
        connection.sock = _connect(parsed.hostname, port, parsed.scheme == "https", cancel_event, deadline)
        _check_interruption(cancel_event, deadline)
        headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json",
                   "Accept-Encoding": "identity", "Connection": "close"}
        if config.api_key:
            headers["Authorization"] = "Bearer " + config.api_key
        connection.request("POST", parsed.path, body=body, headers=headers)
        _check_interruption(cancel_event, deadline)
        response = connection.getresponse()
        _check_interruption(cancel_event, deadline)
        if 300 <= response.status <= 399:
            raise ProviderError("API 返回重定向，已拒绝转发密钥。请填写最终接口地址后重试。")
        if response.status != 200:
            hints = {400: "请求格式或模型不兼容", 401: "API Key 无效或已过期", 402: "余额不足",
                     403: "服务拒绝访问", 404: "接口地址或模型不存在", 413: "分析文本过大",
                     429: "请求限流或额度不足"}
            hint = hints.get(response.status, "服务端或网络代理返回错误")
            # Never echo remote reason/body: a gateway can reflect Authorization.
            raise ProviderError(f"API HTTP {response.status}：{hint}。未自动重试。")
        encoding = response.getheader("Content-Encoding", "identity").strip().lower()
        if encoding not in {"", "identity"}:
            raise ProviderError("API 返回压缩内容而未遵守 identity 请求；已停止，避免解压超限。")
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type and content_type != "application/json" and not content_type.endswith("+json"):
            raise ProviderError("API 响应不是 JSON 类型；请检查接口地址。")
        length = response.getheader("Content-Length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                raise ProviderError("API 响应长度无效。") from None
            if declared < 0 or declared > config.max_response_bytes:
                raise ProviderError("API 响应超过大小上限；已停止接收，未导入任何方案。")
        chunks = bytearray()
        while True:
            _check_interruption(cancel_event, deadline)
            chunk = response.read1(min(65536, config.max_response_bytes + 1 - len(chunks)))
            _check_interruption(cancel_event, deadline)
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > config.max_response_bytes:
                raise ProviderError("API 响应超过大小上限；已停止接收，未导入任何方案。")
        if length is not None and len(chunks) != declared:
            raise ProviderError("API 响应传输不完整；未导入任何方案，也未自动重试。")
        content = _extract_content(bytes(chunks))
        _check_interruption(cancel_event, deadline)
        return content
    except ProviderError:
        raise
    except (TimeoutError, socket.timeout):
        _check_interruption(cancel_event, deadline)
        raise ProviderTimeout("API 连接或读取超时；结果状态不确定，不会自动重试。") from None
    except (OSError, http.client.HTTPException, ValueError):
        _check_interruption(cancel_event, deadline)
        raise ProviderError("API 连接或响应读取失败；请核对网络与接口。未自动重试，服务端可能已接收请求。") from None
    finally:
        if response is not None:
            response.close()
        connection.close()
