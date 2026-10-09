"""Running tally of what the bot has spent.

**Tokens are the record; dollars are derived at read time.** The API reports
tokens and nothing else — it never tells you a price — so any dollar figure here
comes from the rates in `.env`. Storing computed dollars would freeze a guess
into the history: correct the rate later, or switch models, and every past entry
would still be wrong with no way to recompute it. So only token counts are
persisted, and `/cost` multiplies them by the current rate. Fix the rate in
`.env` and the whole history corrects itself.

The provider's dashboard remains the billing truth — it knows about tiers,
caching discounts and promotions that this file cannot.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USAGE_PATH = Path(os.environ.get("USAGE_PATH", "usage.json"))


def _rates() -> tuple[float, float]:
    """Read per-token rates on each call, so .env edits take effect live."""
    return (
        float(os.environ.get("PRICE_IN_PER_MTOK", "0")) / 1_000_000,
        float(os.environ.get("PRICE_OUT_PER_MTOK", "0")) / 1_000_000,
    )


def _bucket() -> dict[str, int]:
    return {"calls": 0, "input_tokens": 0, "output_tokens": 0}


def _load() -> dict[str, Any]:
    base: dict[str, Any] = {**_bucket(), "by_month": {}, "by_chat": {}}
    if not USAGE_PATH.exists():
        return base
    try:
        return {**base, **json.loads(USAGE_PATH.read_text())}
    except json.JSONDecodeError:
        return base


def _save(data: dict[str, Any]) -> None:
    tmp = USAGE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(USAGE_PATH)


def record(chat_id: int, input_tokens: int, output_tokens: int) -> None:
    data = _load()
    month = datetime.now(timezone.utc).strftime("%Y-%m")

    for bucket in (
        data,
        data["by_month"].setdefault(month, _bucket()),
        data["by_chat"].setdefault(str(chat_id), _bucket()),
    ):
        bucket["calls"] = bucket.get("calls", 0) + 1
        bucket["input_tokens"] = bucket.get("input_tokens", 0) + input_tokens
        bucket["output_tokens"] = bucket.get("output_tokens", 0) + output_tokens
    _save(data)


def cost_of(bucket: dict[str, Any]) -> float:
    in_rate, out_rate = _rates()
    return (
        bucket.get("input_tokens", 0) * in_rate
        + bucket.get("output_tokens", 0) * out_rate
    )


def summary(chat_id: int) -> str:
    """HTML, for the /cost command."""
    data = _load()
    if not data.get("calls"):
        return "Nothing spent yet."

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    this_month = data["by_month"].get(month, _bucket())
    mine = data["by_chat"].get(str(chat_id), _bucket())
    total = cost_of(data)
    in_rate, out_rate = _rates()

    lines = [
        "<b>Spend so far</b>",
        f"All time: ${total:.2f} over {data['calls']} call(s)",
        f"This month: ${cost_of(this_month):.2f} over {this_month['calls']} call(s)",
        f"Yours: ${cost_of(mine):.2f} over {mine['calls']} call(s)",
        f"Average per call: ${total / data['calls']:.3f}",
        "",
        f"Tokens: {data['input_tokens']:,} in / {data['output_tokens']:,} out",
        f"<i>Dollars computed at ${in_rate * 1_000_000:g}/"
        f"${out_rate * 1_000_000:g} per Mtok, from .env. Tokens are measured; "
        "the price is not — check the provider dashboard for the real bill.</i>",
    ]
    if len(data["by_chat"]) > 1:
        lines.insert(4, f"Across {len(data['by_chat'])} people using the bot")
    return "\n".join(lines)
