#!/usr/bin/env python3
"""Work out which header/model combination the gateway's batch endpoint wants.

    python check_batches.py

`check_gateway.py` established that Portkey reads `x-portkey-provider` on batch
requests but rejected the value derived from the model prefix. The remaining
unknown is small and purely empirical: which spelling of the provider does it
accept, and does the model need the "@slug/" prefix once the provider is named
in a header?

This tries the combinations, stops at the first that works, and prints the
exact settings to keep. Each attempt creates and immediately cancels a
one-request batch, so the cost is negligible.

Batches bill at half rate, which on a full 3,000-frame run is roughly $190 of
difference — worth a couple of minutes, not worth an afternoon. If nothing here
works, run the synchronous pipeline and move on.
"""

from __future__ import annotations

import sys

import anthropic

import config


def attempt(provider_header: str | None, provider_value: str | None,
            model: str) -> tuple[bool, str]:
    headers = dict(config.EXTRA_HEADERS)
    if provider_header and provider_value:
        headers[provider_header] = provider_value

    kwargs = {"api_key": config.API_KEY or "gateway", "default_headers": headers}
    if config.API_BASE_URL:
        kwargs["base_url"] = config.API_BASE_URL
    client = anthropic.Anthropic(**kwargs)

    try:
        batch = client.messages.batches.create(requests=[{
            "custom_id": "probe-1",
            "params": {"model": model, "max_tokens": 16,
                       "messages": [{"role": "user", "content": "say ok"}]},
        }])
        try:
            client.messages.batches.cancel(batch.id)
        except Exception:
            pass
        return True, batch.id
    except Exception as exc:
        msg = str(exc)
        # Surface Portkey's own message rather than the SDK wrapper noise.
        for marker in ("'message': '", '"message": "'):
            if marker in msg:
                tail = msg.split(marker, 1)[1]
                msg = tail.split("'", 1)[0] if marker.endswith("'") else tail.split('"', 1)[0]
                break
        return False, msg[:120]


def round_trip(provider_value: str, model: str, minutes: int) -> bool:
    """Submit one tiny request and wait for it to actually complete.

    Creating a batch and completing one are different things. A gateway can
    accept the create call, hand back a real Anthropic batch object, and still
    never route the enqueued requests -- which looks like a batch that sits at
    zero for hours. Two minutes here beats discovering it after ninety.
    """
    import time

    headers = dict(config.EXTRA_HEADERS)
    headers["x-portkey-provider"] = provider_value
    kwargs = {"api_key": config.API_KEY or "gateway", "default_headers": headers}
    if config.API_BASE_URL:
        kwargs["base_url"] = config.API_BASE_URL
    client = anthropic.Anthropic(**kwargs)

    batch = client.messages.batches.create(requests=[{
        "custom_id": "roundtrip-1",
        "params": {"model": model, "max_tokens": 16,
                   "messages": [{"role": "user", "content": "Reply with: ok"}]},
    }])
    print(f"  submitted {batch.id}; waiting up to {minutes} min for it to complete")

    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        time.sleep(20)
        b = client.messages.batches.retrieve(batch.id)
        done = sum(getattr(b.request_counts, k, 0) or 0
                   for k in ("succeeded", "errored", "canceled", "expired"))
        mins = (deadline - time.time()) / 60
        print(f"    {b.processing_status}  {done}/1 done  ({minutes - mins:.0f} min elapsed)")
        if b.processing_status == "ended":
            got = list(client.messages.batches.results(batch.id))
            ok = bool(got) and got[0].result.type == "succeeded"
            print(f"  {'PASS' if ok else 'FAIL'} batch completed and returned "
                  f"{got[0].result.type if got else 'nothing'}")
            return ok

    print(f"  FAIL nothing completed in {minutes} min — batches are accepted but")
    print("       not processed on this gateway. Use pipeline.py (synchronous).")
    try:
        client.messages.batches.cancel(batch.id)
        print("       test batch cancelled (unprocessed requests are not billed)")
    except Exception:
        pass
    return False


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true",
                    help="also wait for a one-request batch to actually complete")
    ap.add_argument("--wait-minutes", type=int, default=10)
    args = ap.parse_args()

    slug = config.PORTKEY_PROVIDER
    if not slug:
        print("MW_MODEL_PREFIX is not set, so there is no slug to try.")
        return 1

    bare = config.EXTRACT_MODEL                  # claude-sonnet-5
    prefixed = config.resolve_model(bare)        # @slug/claude-sonnet-5

    print(f"slug     : {slug}")
    print(f"models   : {prefixed}  |  {bare}\n")

    # Ordered by how likely each is to be the intended form.
    combos = [
        ("@slug in header, bare model",      "x-portkey-provider", f"@{slug}",  bare),
        ("@slug in header, prefixed model",  "x-portkey-provider", f"@{slug}",  prefixed),
        ("literal 'anthropic', prefixed",    "x-portkey-provider", "anthropic", prefixed),
        ("literal 'anthropic', bare model",  "x-portkey-provider", "anthropic", bare),
        ("virtual-key header, bare model",   "x-portkey-virtual-key", slug,     bare),
        ("slug in header, bare model",       "x-portkey-provider", slug,        bare),
    ]

    for label, header, value, model in combos:
        ok, detail = attempt(header, value, model)
        print(f"  {'PASS' if ok else 'fail'}  {label}")
        if ok:
            print(f"\nBatches work with {header}: {value}")
            print("Keep this by setting:\n")
            if header == "x-portkey-provider":
                print(f'    $env:MW_PORTKEY_PROVIDER = "{value}"')
            else:
                print(f'    $env:MW_EXTRA_HEADERS = \'{{"{header}": "{value}"}}\'')
            if model == bare:
                print("\nLeave MW_MODEL_PREFIX exactly as it is. The sync path needs the")
                print("prefix and the batch path needs it gone; batch_pipeline.py now")
                print("strips it automatically, so one configuration drives both.")
            if args.full:
                print("\nNow checking a batch actually completes, not just that it "
                      "can be created:")
                if not round_trip(value, model, args.wait_minutes):
                    return 1
            else:
                print("\nThis proves a batch can be CREATED, not that it completes.")
                print("Before relying on it for a long run, confirm end to end:")
                print(f"    python check_batches.py --full")
            print("\nThen use batch_pipeline.py for the full run.")
            return 0
        print(f"        {detail}")

    print("\nNone worked. Two options, in order of what I would do:")
    print("  1. Run the synchronous pipeline. It produces identical output and")
    print("     costs roughly $190 more across the full 3,000 frames. Stop here.")
    print("  2. Ask Portkey support what x-portkey-provider value their batch")
    print("     endpoint expects for a model-catalog slug. It is a one-line answer,")
    print("     and MW_PORTKEY_PROVIDER will accept whatever they tell you.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
