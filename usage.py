"""Running tally of what the bot has spent.

The provider's dashboard is the billing truth, but it lags and — if the key is
shared with other projects — it mixes this bot's spend in with everything else.
This file answers "what has the bot cost me" immediately and in isolation.

Prices come from the env, so the numbers are only as right as PRICE_IN_PER_MTOK
and PRICE_OUT_PER_MTOK. Treat it as a meter, not an invoice.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USAGE_PATH = Path(os.environ.get("USAGE_PATH", "usage.json"))

EMPTY: dict[str, Any] = {
    "calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "usd": 0.0,
    "by_month": {},
    "by_chat": {},
}


def _load() -> dict[str, Any]:
    if not USAGE_PATH.exists():
        return json.loads(json.dumps(EMPTY))
    try:
        return {**json.loads(json.dumps(EMPTY)), **json.loads(USAGE_PATH.read_text())}
    except json.JSONDecodeError:
        return json.loads(json.dumps(EMPTY))


def _save(data: dict[str, Any]) -> None:
    tmp = USAGE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(USAGE_PATH)


def record(chat_id: int, input_tokens: int, output_tokens: int, usd: float) -> None:
    data = _load()
    month = datetime.now(timezone.utc).strftime("%Y-%m")

    for bucket in (
        data,
        data["by_month"].setdefault(month, {"calls": 0, "usd": 0.0}),
        data["by_chat"].setdefault(str(chat_id), {"calls": 0, "usd": 0.0}),
    ):
        bucket["calls"] += 1
        bucket["usd"] = round(bucket["usd"] + usd, 6)

    data["input_tokens"] += input_tokens
    data["output_tokens"] += output_tokens
    _save(data)


def summary(chat_id: int) -> str:
    """HTML, for the /cost command."""
    data = _load()
    if not data["calls"]:
        return "Nothing spent yet."

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    this_month = data["by_month"].get(month, {"calls": 0, "usd": 0.0})
    mine = data["by_chat"].get(str(chat_id), {"calls": 0, "usd": 0.0})
    per_call = data["usd"] / data["calls"]

    lines = [
        "<b>Spend so far</b>",
        f"All time: ${data['usd']:.2f} over {data['calls']} call(s)",
        f"This month: ${this_month['usd']:.2f} over {this_month['calls']} call(s)",
        f"Yours: ${mine['usd']:.2f} over {mine['calls']} call(s)",
        f"Average per call: ${per_call:.3f}",
        "",
        f"Tokens: {data['input_tokens']:,} in / {data['output_tokens']:,} out",
        "<i>Estimated from the prices in .env — check the provider dashboard "
        "for the real bill.</i>",
    ]
    if len(data["by_chat"]) > 1:
        lines.insert(
            4, f"Across {len(data['by_chat'])} people using the bot"
        )
    return "\n".join(lines)
