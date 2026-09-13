"""Thin wrapper around the Messages API.

Everything the two stages need from the API lives here: image preparation,
schema-constrained JSON, retries, and per-call cost accounting.
"""

from __future__ import annotations

import base64
import io
import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

import config


# ==========================================================================
# Errors
# ==========================================================================
class RefusalError(RuntimeError):
    """The model declined the request. Retrying the same input will not help."""


class TruncatedError(RuntimeError):
    """Hit max_tokens mid-JSON. Retry with a larger budget."""


# ==========================================================================
# Cost accounting
# ==========================================================================
@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    calls: int = 0
    by_model: dict = field(default_factory=dict)

    def add(self, model: str, resp_usage, batch: bool = False) -> None:
        i = getattr(resp_usage, "input_tokens", 0) or 0
        o = getattr(resp_usage, "output_tokens", 0) or 0
        c = getattr(resp_usage, "cache_read_input_tokens", 0) or 0
        self.input_tokens += i
        self.output_tokens += o
        self.cache_read_tokens += c
        self.calls += 1
        slot = self.by_model.setdefault(model, {"input": 0, "output": 0, "calls": 0, "batch": batch})
        slot["input"] += i
        slot["output"] += o
        slot["calls"] += 1

    def cost_usd(self) -> float:
        total = 0.0
        for model, u in self.by_model.items():
            price = config.PRICING.get(config.base_model(model))
            if not price:
                continue
            mult = config.BATCH_DISCOUNT if u.get("batch") else 1.0
            total += (u["input"] / 1e6 * price["input"] + u["output"] / 1e6 * price["output"]) * mult
        return total

    def summary(self) -> str:
        return (
            f"{self.calls} calls | {self.input_tokens:,} in / {self.output_tokens:,} out "
            f"| ~${self.cost_usd():.2f}"
        )


# ==========================================================================
# Image preparation
# ==========================================================================
def encode_frame(path: Path, max_edge: int = config.MAX_IMAGE_EDGE) -> dict:
    """Return an image content block, downscaled to a predictable token cost.

    Claude bills an image at roughly (width x height) / 750 tokens and resizes
    anything past 1568px on its long edge anyway, so shrinking locally is free
    accuracy-wise and makes 3,000 frames cost what you expect. A 1920x1080
    frame is ~2,765 tokens; the same frame at 1568x882 is ~1,845.
    """
    try:
        from PIL import Image
    except ImportError:  # send it untouched rather than fail the run
        raw = path.read_bytes()
        media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return _image_block(raw, media)

    with Image.open(path) as im:
        im = im.convert("RGB")
        if max(im.size) > max_edge:
            scale = max_edge / max(im.size)
            im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=config.JPEG_QUALITY, optimize=True)
    return _image_block(buf.getvalue(), "image/jpeg")


def _image_block(raw: bytes, media_type: str) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(raw).decode("ascii"),
        },
    }


# ==========================================================================
# Request construction
# ==========================================================================
_JSON_INSTRUCTION = """

## Output format

Respond with a single JSON object and nothing else. No preamble, no commentary,
no markdown code fences. It must validate against this JSON Schema exactly,
including every required key:

{schema}
"""


def build_params(
    *,
    model: str,
    system: str,
    content: list[dict] | str,
    schema: dict,
    max_tokens: int,
    effort: str | None = None,
    cache_system: bool = True,
    for_batches: bool = False,
) -> dict:
    """Assemble Messages API params shared by the sync and batch paths."""
    prompted = config.JSON_MODE == "prompt"
    if prompted:
        # Gateway strips output_config, so ask in the prompt instead. Strictly
        # worse -- nothing stops the model emitting prose -- but the retry loop
        # in json_call already handles a failed parse.
        system = system + _JSON_INSTRUCTION.format(
            schema=json.dumps(schema, indent=2, ensure_ascii=False))

    if cache_system:
        # Identical across every video, so it is worth caching. Only takes
        # effect once the prefix exceeds the model's minimum cacheable length;
        # below that the API silently ignores it, which is harmless.
        system_block: Any = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}
        ]
    else:
        system_block = system

    params: dict[str, Any] = {
        "model": config.resolve_model(model, for_batches=for_batches),
        "max_tokens": max_tokens,
        "system": system_block,
        "messages": [{"role": "user", "content": content}],
    }
    if not prompted:
        params["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    if effort:
        params["effort"] = effort
    return params


def parse_json_response(message) -> dict:
    """Pull the JSON payload out of a structured-output response."""
    stop = getattr(message, "stop_reason", None)
    if stop == "refusal":
        raise RefusalError("model returned stop_reason=refusal")
    if stop == "max_tokens":
        raise TruncatedError("response hit max_tokens before the JSON closed")

    text = "".join(b.text for b in message.content if getattr(b, "type", None) == "text")
    if not text.strip():
        raise ValueError("no text content in response")

    # Under MW_JSON_MODE=prompt the model sometimes wraps the object in fences
    # or adds a sentence before it. Recover rather than burning a retry.
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.S).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start != -1 and end > start:
            return json.loads(stripped[start:end + 1])
        raise


# ==========================================================================
# Synchronous client
# ==========================================================================
def make_client(api_key: str | None = None, *, for_batches: bool = False
                ) -> "anthropic.Anthropic":
    """Anthropic SDK client, pointed at a gateway when one is configured.

    `for_batches` adds the provider header that Portkey's batch endpoint
    requires but its messages endpoint does not.
    """
    kwargs: dict[str, Any] = {}
    key = api_key or config.API_KEY
    if key:
        kwargs["api_key"] = key
    if config.API_BASE_URL:
        kwargs["base_url"] = config.API_BASE_URL
    headers = config.batch_headers() if for_batches else config.EXTRA_HEADERS
    if headers:
        kwargs["default_headers"] = headers
    return anthropic.Anthropic(**kwargs)


class ClaudeClient:
    def __init__(self, api_key: str | None = None):
        self.client = make_client(api_key)
        self.usage = Usage()

    def json_call(
        self,
        *,
        model: str,
        system: str,
        content: list[dict] | str,
        schema: dict,
        max_tokens: int,
        effort: str | None = None,
    ) -> dict:
        """One schema-constrained call, with retries. Returns the parsed dict."""
        params = build_params(
            model=model, system=system, content=content, schema=schema,
            max_tokens=max_tokens, effort=effort,
        )
        last: Exception | None = None

        for attempt in range(config.MAX_RETRIES):
            try:
                message = self._create(params)
                self.usage.add(model, message.usage)
                return parse_json_response(message)

            except RefusalError:
                raise  # deterministic; retrying burns tokens for nothing

            except TruncatedError as exc:
                last = exc
                params["max_tokens"] = min(int(params["max_tokens"] * 1.6), 32000)

            except (anthropic.RateLimitError, anthropic.APIStatusError,  # noqa: E501
                    anthropic.APIConnectionError, json.JSONDecodeError, ValueError) as exc:
                if isinstance(exc, anthropic.APIStatusError) and exc.status_code < 500 \
                        and not isinstance(exc, anthropic.RateLimitError):
                    raise  # 4xx other than rate limiting is a bug in the request
                last = exc

            delay = config.RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 1)
            time.sleep(delay)

        raise RuntimeError(f"failed after {config.MAX_RETRIES} attempts: {last}") from last

    def _create(self, params: dict):
        """Call messages.create, tolerating SDKs predating `output_config`."""
        try:
            return self.client.messages.create(**params)
        except TypeError:
            p = dict(params)
            oc = p.pop("output_config", None)
            if oc is None:
                raise
            return self.client.messages.create(**p, extra_body={"output_config": oc})
