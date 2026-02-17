"""
聊天响应处理器
"""

import asyncio
import uuid
import re
from typing import Any, AsyncGenerator, AsyncIterable

import orjson
from curl_cffi.requests.errors import RequestsError

from app.core.config import get_config
from app.core.logger import logger
from app.core.exceptions import UpstreamException
from .base import (
    BaseProcessor,
    StreamIdleTimeoutError,
    _with_idle_timeout,
    _normalize_stream_line,
    _collect_image_urls,
    _is_http2_stream_error,
)


def extract_tool_text(raw: str, rollout_id: str = "") -> str:
    """Parse ``<xai:tool_usage_card>`` XML into ``[rolloutId][Type] content``.

    Handles web_search, news_search, x_search, code_interpreter and generic
    tool calls.  Returns empty string when the card cannot be parsed.
    """
    if not raw:
        return ""
    name_match = re.search(
        r"<xai:tool_name>(.*?)</xai:tool_name>", raw, flags=re.DOTALL
    )
    args_match = re.search(
        r"<xai:tool_args>(.*?)</xai:tool_args>", raw, flags=re.DOTALL
    )

    name = name_match.group(1) if name_match else ""
    if name:
        name = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", name, flags=re.DOTALL).strip()

    args = args_match.group(1) if args_match else ""
    if args:
        args = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", args, flags=re.DOTALL).strip()

    payload = None
    if args:
        try:
            payload = orjson.loads(args)
        except orjson.JSONDecodeError:
            payload = None

    label = name
    text = args
    prefix = f"[{rollout_id}]" if rollout_id else ""

    if name == "web_search":
        label = f"{prefix}[WebSearch]"
        if isinstance(payload, dict):
            text = payload.get("query") or payload.get("q") or ""
    elif name == "news_search":
        label = f"{prefix}[NewsSearch]"
        if isinstance(payload, dict):
            text = payload.get("query") or payload.get("q") or ""
    elif name == "x_search":
        label = f"{prefix}[XSearch]"
        if isinstance(payload, dict):
            text = payload.get("query") or payload.get("q") or ""
    elif name == "code_interpreter":
        label = f"{prefix}[CodeInterpreter]"
        if isinstance(payload, dict):
            text = payload.get("code") or payload.get("language") or ""
    elif name:
        label = f"{prefix}[{name}]"
        if isinstance(payload, dict):
            text = str(payload)
    else:
        return ""

    if not text:
        return label
    return f"{label} {text}"


class StreamProcessor(BaseProcessor):
    """流式响应处理器"""

    def __init__(self, model: str, token: str = "", think: bool = None):
        super().__init__(model, token)
        self.response_id: str = None
        self.fingerprint: str = ""
        self.rollout_id: str = ""
        self.think_opened: bool = False
        self.role_sent: bool = False
        self.filter_tags = get_config("chat.filter_tags")
        self.image_format = get_config("app.image_format")

        # tool_usage_card streaming state machine
        self.tool_usage_enabled = (
            "xai:tool_usage_card" in (self.filter_tags or [])
        )
        self._tool_usage_opened = False
        self._tool_usage_buffer = ""

        if think is None:
            self.show_think = get_config("chat.thinking")
        else:
            self.show_think = think

    # ------------------------------------------------------------------
    # tag filtering
    # ------------------------------------------------------------------

    def _filter_tool_card(self, token: str) -> str:
        """Handle ``<xai:tool_usage_card>`` across streamed token chunks.

        Uses a simple state machine: when an opening tag is found the card
        body is buffered until the closing tag arrives.  Once complete the
        card is parsed via *extract_tool_text* and emitted as a single
        ``[rolloutId][Type] content`` line.
        """
        if not token or not self.tool_usage_enabled:
            return token

        output_parts: list[str] = []
        rest = token
        start_tag = "<xai:tool_usage_card"
        end_tag = "</xai:tool_usage_card>"

        while rest:
            if self._tool_usage_opened:
                end_idx = rest.find(end_tag)
                if end_idx == -1:
                    self._tool_usage_buffer += rest
                    return "".join(output_parts)
                end_pos = end_idx + len(end_tag)
                self._tool_usage_buffer += rest[:end_pos]
                line = extract_tool_text(self._tool_usage_buffer, self.rollout_id)
                if line:
                    if output_parts and not output_parts[-1].endswith("\n"):
                        output_parts[-1] += "\n"
                    output_parts.append(f"{line}\n")
                self._tool_usage_buffer = ""
                self._tool_usage_opened = False
                rest = rest[end_pos:]
                continue

            start_idx = rest.find(start_tag)
            if start_idx == -1:
                output_parts.append(rest)
                break

            if start_idx > 0:
                output_parts.append(rest[:start_idx])

            end_idx = rest.find(end_tag, start_idx)
            if end_idx == -1:
                self._tool_usage_opened = True
                self._tool_usage_buffer = rest[start_idx:]
                break

            end_pos = end_idx + len(end_tag)
            raw_card = rest[start_idx:end_pos]
            line = extract_tool_text(raw_card, self.rollout_id)
            if line:
                if output_parts and not output_parts[-1].endswith("\n"):
                    output_parts[-1] += "\n"
                output_parts.append(f"{line}\n")
            rest = rest[end_pos:]

        return "".join(output_parts)

    def _filter_token(self, token: str) -> str:
        """Filter special tags in the current streamed token."""
        if not token:
            return token

        # tool_usage_card gets structural parsing rather than removal
        if self.tool_usage_enabled:
            token = self._filter_tool_card(token)
            if not token:
                return ""

        if not self.filter_tags:
            return token

        for tag in self.filter_tags:
            if tag == "xai:tool_usage_card":
                continue
            if f"<{tag}" in token or f"</{tag}" in token:
                return ""

        return token

    # ------------------------------------------------------------------
    # SSE helpers
    # ------------------------------------------------------------------

    def _sse(self, content: str = "", role: str = None, finish: str = None) -> str:
        """构建 SSE 响应"""
        delta = {}
        if role:
            delta["role"] = role
            delta["content"] = ""
        elif content:
            delta["content"] = content

        chunk = {
            "id": self.response_id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "system_fingerprint": self.fingerprint,
            "choices": [
                {"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}
            ],
        }
        return f"data: {orjson.dumps(chunk).decode()}\n\n"

    # ------------------------------------------------------------------
    # main processing loop
    # ------------------------------------------------------------------

    async def process(
        self, response: AsyncIterable[bytes]
    ) -> AsyncGenerator[str, None]:
        """处理流式响应"""
        idle_timeout = get_config("timeout.stream_idle_timeout")

        try:
            async for line in _with_idle_timeout(response, idle_timeout, self.model):
                line = _normalize_stream_line(line)
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})
                is_thinking = bool(resp.get("isThinking"))

                if (llm := resp.get("llmInfo")) and not self.fingerprint:
                    self.fingerprint = llm.get("modelHash", "")
                if rid := resp.get("responseId"):
                    self.response_id = rid
                if rid := resp.get("rolloutId"):
                    self.rollout_id = str(rid)

                if not self.role_sent:
                    yield self._sse(role="assistant")
                    self.role_sent = True

                # 图像生成进度
                if img := resp.get("streamingImageGenerationResponse"):
                    if not self.show_think:
                        continue
                    if is_thinking and not self.think_opened:
                        yield self._sse("<think>\n")
                        self.think_opened = True
                    if (not is_thinking) and self.think_opened:
                        yield self._sse("\n</think>\n")
                        self.think_opened = False
                    idx = img.get("imageIndex", 0) + 1
                    progress = img.get("progress", 0)
                    yield self._sse(
                        f"正在生成第{idx}张图片中，当前进度{progress}%\n"
                    )
                    continue

                # modelResponse
                if mr := resp.get("modelResponse"):
                    # 处理生成的图片
                    for url in _collect_image_urls(mr):
                        parts = url.split("/")
                        img_id = parts[-2] if len(parts) >= 2 else "image"

                        if self.image_format == "base64":
                            try:
                                dl_service = self._get_dl()
                                base64_data = await dl_service.to_base64(
                                    url, self.token, "image"
                                )
                                if base64_data:
                                    yield self._sse(f"![{img_id}]({base64_data})\n")
                                else:
                                    final_url = await self.process_url(url, "image")
                                    yield self._sse(f"![{img_id}]({final_url})\n")
                            except Exception as e:
                                logger.warning(
                                    f"Failed to convert image to base64, falling back to URL: {e}"
                                )
                                final_url = await self.process_url(url, "image")
                                yield self._sse(f"![{img_id}]({final_url})\n")
                        else:
                            final_url = await self.process_url(url, "image")
                            yield self._sse(f"![{img_id}]({final_url})\n")

                    if (
                        (meta := mr.get("metadata", {}))
                        .get("llm_info", {})
                        .get("modelHash")
                    ):
                        self.fingerprint = meta["llm_info"]["modelHash"]
                    continue

                # cardAttachment (inline image cards)
                if card := resp.get("cardAttachment"):
                    json_data = card.get("jsonData")
                    if isinstance(json_data, str) and json_data.strip():
                        try:
                            card_data = orjson.loads(json_data)
                        except orjson.JSONDecodeError:
                            card_data = None
                        if isinstance(card_data, dict):
                            image = card_data.get("image") or {}
                            original = image.get("original")
                            title = image.get("title") or ""
                            if original:
                                title_safe = title.replace("\n", " ").strip()
                                if title_safe:
                                    yield self._sse(f"![{title_safe}]({original})\n")
                                else:
                                    yield self._sse(f"![image]({original})\n")
                    continue

                # 普通 token — 带 isThinking 思维链包裹
                if (token := resp.get("token")) is not None:
                    if not token:
                        continue
                    filtered = self._filter_token(token)
                    if not filtered:
                        continue
                    if is_thinking:
                        if not self.show_think:
                            continue
                        if not self.think_opened:
                            yield self._sse("<think>\n")
                            self.think_opened = True
                    else:
                        if self.think_opened:
                            yield self._sse("\n</think>\n")
                            self.think_opened = False
                    yield self._sse(filtered)

            if self.think_opened:
                yield self._sse("</think>\n")
            yield self._sse(finish="stop")
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            logger.debug("Stream cancelled by client", extra={"model": self.model})
        except StreamIdleTimeoutError as e:
            raise UpstreamException(
                message=f"Stream idle timeout after {e.idle_seconds}s",
                status_code=504,
                details={
                    "error": str(e),
                    "type": "stream_idle_timeout",
                    "idle_seconds": e.idle_seconds,
                },
            )
        except RequestsError as e:
            if _is_http2_stream_error(e):
                logger.warning(f"HTTP/2 stream error: {e}", extra={"model": self.model})
                raise UpstreamException(
                    message="Upstream connection closed unexpectedly",
                    status_code=502,
                    details={"error": str(e), "type": "http2_stream_error"},
                )
            logger.error(f"Stream request error: {e}", extra={"model": self.model})
            raise UpstreamException(
                message=f"Upstream request failed: {e}",
                status_code=502,
                details={"error": str(e)},
            )
        except Exception as e:
            logger.error(
                f"Stream processing error: {e}",
                extra={"model": self.model, "error_type": type(e).__name__},
            )
            raise
        finally:
            await self.close()


class CollectProcessor(BaseProcessor):
    """非流式响应处理器"""

    def __init__(self, model: str, token: str = ""):
        super().__init__(model, token)
        self.image_format = get_config("app.image_format")
        self.filter_tags = get_config("chat.filter_tags")

    def _filter_content(self, content: str) -> str:
        """过滤内容中的特殊标签，tool_usage_card 做结构化解析"""
        if not content or not self.filter_tags:
            return content

        result = content

        # tool_usage_card: structural parsing → [rolloutId][Type] content
        if "xai:tool_usage_card" in self.filter_tags:
            rollout_id = ""
            rollout_match = re.search(
                r"<rolloutId>(.*?)</rolloutId>", result, flags=re.DOTALL
            )
            if rollout_match:
                rollout_id = rollout_match.group(1).strip()

            result = re.sub(
                r"<xai:tool_usage_card[^>]*>.*?</xai:tool_usage_card>",
                lambda match: (
                    f"{extract_tool_text(match.group(0), rollout_id)}\n"
                    if extract_tool_text(match.group(0), rollout_id)
                    else ""
                ),
                result,
                flags=re.DOTALL,
            )

        # other filter tags: simple removal
        for tag in self.filter_tags:
            if tag == "xai:tool_usage_card":
                continue
            pattern = rf"<{re.escape(tag)}[^>]*>.*?</{re.escape(tag)}>|<{re.escape(tag)}[^>]*/>"
            result = re.sub(pattern, "", result, flags=re.DOTALL)

        return result

    async def process(self, response: AsyncIterable[bytes]) -> dict[str, Any]:
        """处理并收集完整响应"""
        response_id = ""
        fingerprint = ""
        content = ""
        idle_timeout = get_config("timeout.stream_idle_timeout")

        try:
            async for line in _with_idle_timeout(response, idle_timeout, self.model):
                line = _normalize_stream_line(line)
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})

                if (llm := resp.get("llmInfo")) and not fingerprint:
                    fingerprint = llm.get("modelHash", "")

                if mr := resp.get("modelResponse"):
                    response_id = mr.get("responseId", "")
                    content = mr.get("message", "")

                    # cardAttachmentsJson → inline image rendering
                    card_map: dict[str, tuple[str, str]] = {}
                    for raw in mr.get("cardAttachmentsJson") or []:
                        if not isinstance(raw, str) or not raw.strip():
                            continue
                        try:
                            card_data = orjson.loads(raw)
                        except orjson.JSONDecodeError:
                            continue
                        if not isinstance(card_data, dict):
                            continue
                        card_id = card_data.get("id")
                        image = card_data.get("image") or {}
                        original = image.get("original")
                        if not card_id or not original:
                            continue
                        title = image.get("title") or ""
                        card_map[card_id] = (title, original)

                    if content and card_map:
                        def _render_card(match: re.Match) -> str:
                            card_id = match.group(1)
                            item = card_map.get(card_id)
                            if not item:
                                return ""
                            title, original = item
                            title_safe = title.replace("\n", " ").strip() or "image"
                            prefix = ""
                            if match.start() > 0:
                                prev = content[match.start() - 1]
                                if prev not in ("\n", "\r"):
                                    prefix = "\n"
                            return f"{prefix}![{title_safe}]({original})"

                        content = re.sub(
                            r'<grok:render[^>]*card_id="([^"]+)"[^>]*>.*?</grok:render>',
                            _render_card,
                            content,
                            flags=re.DOTALL,
                        )

                    if urls := _collect_image_urls(mr):
                        content += "\n"
                        for url in urls:
                            parts = url.split("/")
                            img_id = parts[-2] if len(parts) >= 2 else "image"

                            if self.image_format == "base64":
                                try:
                                    dl_service = self._get_dl()
                                    base64_data = await dl_service.to_base64(
                                        url, self.token, "image"
                                    )
                                    if base64_data:
                                        content += f"![{img_id}]({base64_data})\n"
                                    else:
                                        final_url = await self.process_url(url, "image")
                                        content += f"![{img_id}]({final_url})\n"
                                except Exception as e:
                                    logger.warning(
                                        f"Failed to convert image to base64, falling back to URL: {e}"
                                    )
                                    final_url = await self.process_url(url, "image")
                                    content += f"![{img_id}]({final_url})\n"
                            else:
                                final_url = await self.process_url(url, "image")
                                content += f"![{img_id}]({final_url})\n"

                    if (
                        (meta := mr.get("metadata", {}))
                        .get("llm_info", {})
                        .get("modelHash")
                    ):
                        fingerprint = meta["llm_info"]["modelHash"]

        except asyncio.CancelledError:
            logger.debug("Collect cancelled by client", extra={"model": self.model})
        except StreamIdleTimeoutError as e:
            logger.warning(f"Collect idle timeout: {e}", extra={"model": self.model})
        except RequestsError as e:
            if _is_http2_stream_error(e):
                logger.warning(
                    f"HTTP/2 stream error in collect: {e}", extra={"model": self.model}
                )
            else:
                logger.error(f"Collect request error: {e}", extra={"model": self.model})
        except Exception as e:
            logger.error(
                f"Collect processing error: {e}",
                extra={"model": self.model, "error_type": type(e).__name__},
            )
        finally:
            await self.close()

        content = self._filter_content(content)

        return {
            "id": response_id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "system_fingerprint": fingerprint,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "refusal": None,
                        "annotations": [],
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "prompt_tokens_details": {
                    "cached_tokens": 0,
                    "text_tokens": 0,
                    "audio_tokens": 0,
                    "image_tokens": 0,
                },
                "completion_tokens_details": {
                    "text_tokens": 0,
                    "audio_tokens": 0,
                    "reasoning_tokens": 0,
                },
            },
        }


__all__ = ["StreamProcessor", "CollectProcessor"]
