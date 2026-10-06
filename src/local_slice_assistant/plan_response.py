"""Read one unambiguous AI edit plan without interpreting prose as commands.

Web AI may return a plain JSON object or wrap it in a labelled Markdown JSON
fence. Both routes share strict decoding; schema and source checks remain in
``manifest`` so an API response has exactly the same import boundary as a file.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from .errors import ManifestValidationError


MAX_PLAN_RESPONSE_BYTES = 16 * 1024 * 1024
_FENCE_START = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)$")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestValidationError("AI 方案含重复 JSON 字段，不能确定采用哪一个。", detail=key)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ManifestValidationError("AI 方案不能包含 NaN 或 Infinity 等非有限数值。")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        _reject_constant(value)
    return number


def _check_decoded_values(raw: dict[str, Any]) -> None:
    # The JSON C decoder's recursion allowance varies by Python/platform. Use
    # our own fixed limit and reject escaped lone surrogates before saving any
    # response as UTF-8. Iterators avoid a second large flat list allocation.
    stack = [iter((raw,))]
    while stack:
        try:
            value = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError as exc:
                raise ManifestValidationError("AI 方案包含无效的 Unicode 字符，请重新导出 UTF-8 文件。") from exc
        elif isinstance(value, dict):
            if len(stack) >= 100:
                raise ManifestValidationError("AI 方案嵌套过深，请简化后重新导入。")
            stack.append(iter(item for pair in value.items() for item in pair))
        elif isinstance(value, list):
            if len(stack) >= 100:
                raise ManifestValidationError("AI 方案嵌套过深，请简化后重新导入。")
            stack.append(iter(value))


def _json_fence(text: str) -> str:
    """Extract one closed, explicitly labelled JSON fence, not arbitrary braces."""
    candidates: list[str] = []
    other_candidates = 0
    marker: str | None = None
    is_json = False
    body: list[str] = []
    for line in text.splitlines():
        if marker is None:
            match = _FENCE_START.fullmatch(line)
            if match:
                marker = match.group(1)
                is_json = match.group(2).strip().casefold() == "json"
                body = []
            continue
        if re.fullmatch(r"[ \t]{0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*", line):
            if is_json:
                candidates.append("\n".join(body))
            elif "\n".join(body).lstrip().startswith(("{", "[")):
                other_candidates += 1
            marker = None
            body = []
        else:
            body.append(line)
    if marker is not None and is_json:
        raise ManifestValidationError("AI 方案的 JSON 代码块没有结束，请完整保存返回内容。")
    if len(candidates) > 1 or (candidates and other_candidates):
        raise ManifestValidationError("AI 返回了多个 JSON 方案，请只保留一个正式方案后导入。")
    if not candidates:
        raise ManifestValidationError(
            "没有找到可执行的剪辑方案。请导入纯 JSON，或含一个标明 json 的代码块的 MD/TXT。",
            detail="普通 SRT、时间戳台词或自然语言设计稿不是剪辑方案；请在“已有台词”中导入字幕。",
        )
    return candidates[0]


def decode_plan_response(text: str) -> dict[str, Any]:
    """Decode a JSON object or one Markdown JSON block; raise user-facing errors.

    Does not change media, infer timings, discard duplicate keys, or pick among
    alternatives. Returned fields are intact for subsequent manifest validation.
    """
    if not isinstance(text, str):
        raise ManifestValidationError("AI 返回方案必须是 UTF-8 文本。")
    if len(text) > MAX_PLAN_RESPONSE_BYTES:
        raise ManifestValidationError("AI 方案超过 16 MiB，请拆分成较小的方案。")
    try:
        if len(text.encode("utf-8")) > MAX_PLAN_RESPONSE_BYTES:
            raise ManifestValidationError("AI 方案超过 16 MiB，请拆分成较小的方案。")
    except UnicodeError as exc:
        raise ManifestValidationError("AI 方案包含无效的 Unicode 字符，请重新导出 UTF-8 文件。") from exc
    text = text.lstrip("\ufeff").strip()
    if not text:
        raise ManifestValidationError("AI 方案文件为空。")
    payload = text if text[0] in "{[" else _json_fence(text)
    try:
        raw = json.loads(payload, object_pairs_hook=_unique_object,
                         parse_constant=_reject_constant, parse_float=_finite_float)
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(
            "AI 方案不是有效 JSON，请让 AI 按剪辑模板重新返回完整方案。",
            detail=f"第 {exc.lineno} 行，第 {exc.colno} 列。不要拼接多个方案或在 JSON 中添加注释。",
        ) from exc
    except (ValueError, RecursionError) as exc:
        raise ManifestValidationError("AI 方案的数字过长或嵌套过深，请简化后重新导入。") from exc
    if not isinstance(raw, dict):
        raise ManifestValidationError("剪辑清单根节点必须是对象，不能是数组或其它值。")
    _check_decoded_values(raw)
    return raw


def read_plan_response(path: str | Path) -> dict[str, Any]:
    """Read at most 16 MiB from a user-selected UTF-8/UTF-8-BOM plan file."""
    path = Path(path)
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_PLAN_RESPONSE_BYTES + 1)
    except FileNotFoundError as exc:
        raise ManifestValidationError("找不到剪辑清单文件。", detail=str(path)) from exc
    except OSError as exc:
        raise ManifestValidationError("无法读取剪辑清单文件。", detail=str(path)) from exc
    if len(content) > MAX_PLAN_RESPONSE_BYTES:
        raise ManifestValidationError("AI 方案超过 16 MiB，请拆分成较小的方案。")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ManifestValidationError("剪辑清单不是有效的 UTF-8 文本，请另存为 UTF-8 后重试。") from exc
    return decode_plan_response(text)
