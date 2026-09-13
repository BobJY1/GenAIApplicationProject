#!/usr/bin/env python3
"""Find out what your endpoint actually supports, before you rely on it.

    python check_gateway.py

Gateways proxy the Messages API to varying depths. Rather than guess, this
sends one small request per feature the pipeline depends on and reports what
came back. Total cost is a few cents.

Four features are tested, in the order they would bite you:

  1. basic call        nothing works without it
  2. images            stage 1 reads whiteboard frames; no images, no pipeline
  3. structured outputs the schema-constrained JSON every stage depends on
  4. message batches    the 50%-cheaper bulk path (optional)

Failures of 3 or 4 are survivable; the script tells you what to change.
"""

from __future__ import annotations

import base64
import io
import json
import sys

import anthropic

import config
from claude_client import make_client

PASS, FAIL, SKIP = "PASS", "FAIL", "----"
results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    print(f"  {status}  {name}" + (f"\n        {detail}" if detail else ""))


def tiny_png() -> dict:
    """A 8x8 red square, so the image test does not need a file on disk."""
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, format="PNG")
        raw = buf.getvalue()
    except ImportError:
        raw = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/png",
                       "data": base64.standard_b64encode(raw).decode()}}


def main() -> int:
    model = config.EXTRACT_MODEL
    print(f"endpoint : {config.API_BASE_URL or 'https://api.anthropic.com (direct)'}")
    print(f"model    : {config.resolve_model(model)}")
    print(f"headers  : {sorted(config.EXTRA_HEADERS) or 'none'}\n")

    client = make_client()

    # -- 1. basic call ------------------------------------------------------
    try:
        m = client.messages.create(
            model=config.resolve_model(model), max_tokens=16,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}])
        text = "".join(b.text for b in m.content if getattr(b, "type", None) == "text")
        record("basic messages call", PASS, f"replied {text.strip()[:30]!r}")
    except Exception as exc:
        record("basic messages call", FAIL, f"{type(exc).__name__}: {str(exc)[:220]}")
        print("\nNothing else can work until this does. Check the model prefix "
              "(MW_MODEL_PREFIX), the gateway key, and the base URL.")
        return 1

    # -- 2. images ----------------------------------------------------------
    try:
        m = client.messages.create(
            model=config.resolve_model(model), max_tokens=24,
            messages=[{"role": "user", "content": [
                tiny_png(),
                {"type": "text", "text": "What colour is this square? One word."}]}])
        text = "".join(b.text for b in m.content if getattr(b, "type", None) == "text")
        record("image input", PASS, f"replied {text.strip()[:40]!r}")
    except Exception as exc:
        record("image input", FAIL, f"{type(exc).__name__}: {str(exc)[:220]}")

    # -- 3. structured outputs ---------------------------------------------
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"answer": {"type": "number", "description": "the numeric result"},
                       "confidence": {"type": "number", "description": "0.0 to 1.0"}},
        "required": ["answer", "confidence"],
    }
    try:
        params = {
            "model": config.resolve_model(model), "max_tokens": 128,
            "messages": [{"role": "user", "content": "What is 17 times 3?"}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        try:
            m = client.messages.create(**params)
        except TypeError:
            oc = params.pop("output_config")
            m = client.messages.create(**params, extra_body={"output_config": oc})

        text = "".join(b.text for b in m.content if getattr(b, "type", None) == "text")
        data = json.loads(text)
        if data.get("answer") == 51:
            record("structured outputs", PASS, f"schema honoured, got {data}")
        else:
            record("structured outputs", PASS,
                   f"valid JSON but unexpected value: {data}")
    except json.JSONDecodeError:
        record("structured outputs", FAIL,
               "returned prose, not JSON — the gateway stripped output_config. "
               "Set MW_JSON_MODE=prompt to fall back to prompted JSON.")
    except Exception as exc:
        record("structured outputs", FAIL,
               f"{type(exc).__name__}: {str(exc)[:220]}\n"
               "        Set MW_JSON_MODE=prompt to fall back to prompted JSON.")

    # -- 4. every model the pipeline uses -----------------------------------
    # The probe only exercised EXTRACT_MODEL. Stage 2 defaults to Opus, and a
    # gateway account often has only some models provisioned -- which would
    # fail on every solve call, hours in.
    wanted = {"extract": config.EXTRACT_MODEL, "solve": config.SOLVE_MODEL,
              "escalation": config.ESCALATION_MODEL}
    missing = []
    for role, name in sorted(set((r, m) for r, m in wanted.items())):
        if name == model:
            continue
        try:
            client.messages.create(
                model=config.resolve_model(name), max_tokens=8,
                messages=[{"role": "user", "content": "hi"}])
            record(f"model available: {name} ({role})", PASS)
        except Exception as exc:
            missing.append((role, name))
            record(f"model available: {name} ({role})", FAIL,
                   f"{type(exc).__name__}: {str(exc)[:150]}")

    # -- 5. message batches -------------------------------------------------
    # Portkey's /messages endpoint resolves the provider from the "@slug/model"
    # prefix; the batch endpoint does not and wants an explicit header. Try
    # plain first, then with the provider header.
    def try_batch(cl, label):
        batch = cl.messages.batches.create(requests=[{
            "custom_id": "probe-1",
            "params": {"model": config.resolve_model(model), "max_tokens": 16,
                       "messages": [{"role": "user", "content": "say ok"}]},
        }])
        try:
            cl.messages.batches.cancel(batch.id)
        except Exception:
            pass
        return batch.id

    batch_ok = False
    try:
        bid = try_batch(client, "plain")
        record("message batches", PASS, f"created and cancelled {bid}")
        batch_ok = True
    except Exception as exc:
        first = f"{type(exc).__name__}: {str(exc)[:130]}"
        if config.PORTKEY_PROVIDER:
            try:
                bid = try_batch(make_client(for_batches=True), "with provider header")
                record("message batches", PASS,
                       f"created {bid} once x-portkey-provider="
                       f"{config.PORTKEY_PROVIDER} was added (batch_pipeline.py "
                       f"sends this automatically)")
                batch_ok = True
            except Exception as exc2:
                record("message batches", FAIL,
                       f"without provider header: {first}\n"
                       f"        with provider header:    {type(exc2).__name__}: {str(exc2)[:130]}\n"
                       "        Use pipeline.py (synchronous). Costs 2x, works identically.")
        else:
            record("message batches", FAIL,
                   f"{first}\n        Set MW_MODEL_PREFIX so a provider can be derived, "
                   "or use pipeline.py (synchronous).")

    # -- verdict ------------------------------------------------------------
    print()
    by = dict((n, s) for n, s, _ in results)
    if missing:
        print("Some models are not provisioned on this account. Either enable them "
              "in your gateway, or point the pipeline at what you do have:")
        for role, name in missing:
            var = {"extract": "MW_EXTRACT_MODEL", "solve": "MW_SOLVE_MODEL",
                   "escalation": "MW_ESCALATION_MODEL"}[role]
            print(f"    {var}={model}   # instead of {name}")
        print()

    if by.get("basic messages call") == PASS and by.get("image input") == PASS:
        if by.get("structured outputs") == PASS:
            print("Ready. Run:  python pipeline.py run --limit 5")
            if not batch_ok:
                print("Batches unavailable, so use pipeline.py rather than "
                      "batch_pipeline.py for the full run (2x cost, same output).")
        else:
            print("Usable with MW_JSON_MODE=prompt, which asks for JSON in the prompt "
                  "instead of constraining the decoder.\nExpect occasional parse "
                  "retries; the pipeline already retries on bad JSON.")
    else:
        print("Not usable yet — fix the failures above first.")
    return 0 if by.get("basic messages call") == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
