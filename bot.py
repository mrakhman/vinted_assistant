import asyncio
import functools
import time
import base64
import html
import logging
import os
from typing import Literal, Optional

from openai import OpenAI
from pydantic import BaseModel
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

import store
import usage
from prompts import LISTING_SYSTEM, PHOTO_SYSTEM, REPLY_SYSTEM

MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5")

# Photos arriving within this window are treated as one item.
ALBUM_WINDOW_SECONDS = 2.5
MAX_PHOTOS = 8

# USD per million tokens, only used for the cost line in the logs. Set these to
# whatever OpenAI actually charges for the model you pick.
IN_PRICE = float(os.environ.get("PRICE_IN_PER_MTOK", "5")) / 1_000_000
OUT_PRICE = float(os.environ.get("PRICE_OUT_PER_MTOK", "30")) / 1_000_000

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("vinted-bot")

# Set DRY_RUN=1 to exercise the whole Telegram flow with canned answers and no
# API key. Everything except the model call is real.
DRY_RUN = os.environ.get("DRY_RUN") == "1"


@functools.cache
def openai_client() -> OpenAI:
    """Built on first use, so the bot starts without an API key in DRY_RUN."""
    return OpenAI()


class Listing(BaseModel):
    """Only what she can't do herself.

    Brand, size, condition, material, colour and category are all dropdowns she
    taps in Vinted's form — and the first four she tells us. Echoing them back
    would be a loop, so they feed the description and are never output.
    """

    title: str
    description_fr: str
    description_en: str
    parcel_size: Literal["Petit", "Moyen", "Grand"]
    price_low_eur: int
    price_high_eur: int
    notes: Optional[str]


class Reply(BaseModel):
    buyer_language: str
    buyer_message_en: str
    suggested_reply: str
    suggested_reply_en: str
    needs_from_her: Optional[str]


class PhotoResult(BaseModel):
    """Photos are either a garment to list or a screenshot of a buyer chat."""

    kind: Literal["listing", "buyer_message"]
    listing: Optional[Listing]
    reply: Optional[Reply]


FIXTURES: dict[type[BaseModel], BaseModel] = {
    Listing: Listing(
        title="Blazer en laine beige, coupe droite",
        description_fr=(
            "Blazer en laine, coupe droite, épaules légèrement structurées. "
            "Porté quelques fois, aucun défaut. Taille M, correspond plutôt à un 38."
        ),
        description_en=(
            "Wool blazer, straight cut, lightly structured shoulders. "
            "Worn a few times, no flaws. Size M, runs closer to a UK 10."
        ),
        parcel_size="Moyen",
        price_low_eur=18,
        price_high_eur=25,
        notes=None,
    ),
    Reply: Reply(
        buyer_language="Italian",
        buyer_message_en="Hi, is this item still available?",
        suggested_reply="Ciao! Sì, è ancora disponibile.",
        suggested_reply_en="Hi! Yes, it's still available.",
        needs_from_her=None,
    ),
}


FIXTURES[PhotoResult] = PhotoResult(
    kind="listing", listing=FIXTURES[Listing], reply=None
)


def allowed_users() -> set[int]:
    raw = os.environ.get("ALLOWED_USER_IDS", "")
    return {int(x) for x in raw.replace(",", " ").split()}


def is_allowed(update: Update) -> bool:
    allow = allowed_users()
    user = update.effective_user
    if not allow:
        log.warning("ALLOWED_USER_IDS is empty — anyone can use this bot")
        return True
    if user and user.id in allow:
        return True
    log.warning(
        "DENIED user_id=%s (%s) — not in allowlist %s",
        user.id if user else "?",
        user.username if user else "?",
        sorted(allow),
    )
    return False


# A stranger who spams shouldn't get a reply per message.
DENY_THROTTLE_SECONDS = 30
_last_denied: dict[int, float] = {}


async def deny(update: Update) -> None:
    """Tell them politely, once, and show the ID the owner would need to add."""
    chat, user = update.effective_chat, update.effective_user
    now = time.monotonic()
    if chat:
        if now - _last_denied.get(chat.id, 0.0) < DENY_THROTTLE_SECONDS:
            return
        _last_denied[chat.id] = now

    uid = user.id if user else "unknown"
    if update.callback_query:
        # Callback answers are plain text and capped at ~200 characters.
        await update.callback_query.answer(
            f"This bot is private. Your ID: {uid}", show_alert=True
        )
        return
    if update.message:
        await update.message.reply_text(
            "🔒 <b>This bot is private.</b>\n\n"
            "It's someone's personal Vinted assistant, not a public service — "
            "so there's nothing here for you, sorry!\n\n"
            "If you're supposed to have access, send whoever runs it this ID and "
            "ask them to add you:\n"
            f"<code>{uid}</code>",
            parse_mode="HTML",
        )


def ask_model(
    system: str, content: list[dict], schema: type[BaseModel], chat_id: int
):
    if DRY_RUN:
        log.info("DRY_RUN: returning canned %s", schema.__name__)
        return FIXTURES[schema]

    response = openai_client().responses.parse(
        model=MODEL,
        instructions=system,
        input=[{"role": "user", "content": content}],
        text_format=schema,
    )
    u = response.usage
    cost = u.input_tokens * IN_PRICE + u.output_tokens * OUT_PRICE
    log.info("%s in / %s out tokens — $%.3f", u.input_tokens, u.output_tokens, cost)
    usage.record(chat_id, u.input_tokens, u.output_tokens, cost)
    return response.output_parsed


def image_block(jpeg_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(jpeg_bytes).decode()
    return {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"}


def text_block(text: str) -> dict:
    return {"type": "input_text", "text": text}


def code(text: str) -> str:
    return f"<code>{html.escape(text)}</code>"


# In DRY_RUN the answer is a fixture, not a reading of her photos. Say so loudly,
# or a canned blazer looks like a real (and wrong) description.
BANNER = (
    "🧪 <b>DRY RUN — canned answer, no model was called.</b>\n"
    "<i>Set OPENAI_API_KEY and DRY_RUN=0 for real output.</i>\n\n"
)


def with_banner(body: str) -> str:
    return BANNER + body if DRY_RUN else body


def render_listing(item: Listing) -> str:
    lines = [
        "<b>Title</b>",
        code(item.title),
        "",
        "<b>Description</b>",
        code(f"{item.description_fr}\n\n{item.description_en}"),
        "",
        f"<b>Parcel</b> {item.parcel_size}   ·   "
        f"<b>Price</b> {item.price_low_eur}–{item.price_high_eur} € "
        "<i>(estimate)</i>",
    ]
    if item.notes:
        lines += ["", f"⚠️ {html.escape(item.notes)}"]
    return with_banner("\n".join(lines))


def render_reply(r: Reply) -> str:
    lines = [
        f"<b>Buyer wrote</b> <i>({html.escape(r.buyer_language)})</i>",
        html.escape(r.buyer_message_en),
        "",
        "<b>Reply to send</b>",
        code(r.suggested_reply),
        "",
        f"<i>In English: {html.escape(r.suggested_reply_en)}</i>",
    ]
    if r.needs_from_her:
        lines += ["", f"⚠️ {html.escape(r.needs_from_her)}"]
    return with_banner("\n".join(lines))


# The "/" list and the Menu button beside the input field.
COMMANDS = [
    BotCommand("new", "New item — send photos next"),
    BotCommand("followup", "Answer a buyer about one of your items"),
    BotCommand("items", "See what's still on sale"),
    BotCommand("sold", "Take an item off the list"),
    BotCommand("cost", "What this bot has spent so far"),
    BotCommand("help", "What this bot does"),
]

# Buttons that sit where the phone keyboard would be, so she never types a slash.
BTN_NEW = "🆕 New item"
BTN_FOLLOWUP = "💬 Follow-up"
BTN_ITEMS = "📋 My items"
BTN_SOLD = "✅ Mark sold"

MENU = ReplyKeyboardMarkup(
    [[BTN_NEW, BTN_FOLLOWUP], [BTN_ITEMS, BTN_SOLD]],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Photos, a buyer's message, or a screenshot",
)


async def publish_commands(app: Application) -> None:
    await app.bot.set_my_commands(COMMANDS)
    log.info("command menu published: %s", ", ".join(c.command for c in COMMANDS))


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    await update.message.reply_text(
        "<b>/new</b> — new item. Send the photos next and I'll give you the title, "
        "the description in French then English, and a suggested price.\n\n"
        "<b>/followup</b> — buyer question. Pick the item from the list, then paste "
        "their message or send a screenshot of the conversation.\n\n"
        "<b>/sold</b> — take an item off the list.\n"
        "<b>/items</b> — see what's still on sale.\n\n"
        "You can also just send photos or a message with no command — I'll work out "
        "whether I'm looking at an item or a conversation.\n\n"
        f"<i>Your Telegram user ID is {update.effective_user.id}</i>",
        parse_mode="HTML",
        reply_markup=MENU,
    )


def item_keyboard(prefix: str, chat_id: int) -> Optional[InlineKeyboardMarkup]:
    items = store.active(chat_id)
    if not items:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    item["title"][:60], callback_data=f"{prefix}:{item['id']}"
                )
            ]
            for item in items[:30]
        ]
    )


async def on_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    context.chat_data["mode"] = "new_item"
    context.chat_data.pop("followup_item", None)
    await update.message.reply_text(
        "<b>New item.</b> Send the photos (up to 8) with a caption telling me what "
        "I can't see:\n\n"
        "• brand\n• size, and whether it runs true\n• condition\n"
        "• material\n• any flaw\n\n"
        "Free-form, any order, skip what you don't know:\n"
        "<code>Zara / M, runs small / très bon état / laine / petit trou sous le "
        "bras gauche</code>",
        parse_mode="HTML",
        reply_markup=MENU,
    )


async def on_followup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    keyboard = item_keyboard("fu", update.effective_chat.id)
    if keyboard is None:
        await update.message.reply_text("Nothing on sale yet. Run /new first.")
        return
    await update.message.reply_text(
        "Which item is this about?", reply_markup=keyboard
    )


async def on_sold_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    keyboard = item_keyboard("sold", update.effective_chat.id)
    if keyboard is None:
        await update.message.reply_text("Nothing on sale.")
        return
    await update.message.reply_text("Which item sold?", reply_markup=keyboard)


async def on_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    items = store.active(update.effective_chat.id)
    if not items:
        await update.message.reply_text("Nothing on sale.")
        return
    listed = "\n".join(f"• {html.escape(i['title'])}" for i in items)
    await update.message.reply_text(
        f"<b>{len(items)} item(s) on sale</b>\n{listed}", parse_mode="HTML"
    )


async def on_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    await update.message.reply_text(
        usage.summary(update.effective_chat.id), parse_mode="HTML"
    )


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """She tapped an item in a /followup or /sold list."""
    query = update.callback_query
    # Check access before answering: Telegram honours only the first
    # answerCallbackQuery, so an early no-op answer would swallow the refusal.
    if not is_allowed(update):
        await deny(update)
        return
    await query.answer()

    prefix, _, item_id = query.data.partition(":")
    chat_id = update.effective_chat.id
    item = store.get(chat_id, item_id)
    if item is None:
        await query.edit_message_text("That item is gone.")
        return

    if prefix == "sold":
        store.mark_sold(chat_id, item_id)
        await query.edit_message_text(f"Sold: {item['title']} ✅")
        return

    context.chat_data["followup_item"] = item_id
    context.chat_data.pop("mode", None)
    await query.edit_message_text(
        f"<b>{html.escape(item['title'])}</b>\nPaste the buyer's message, or send a "
        "screenshot of the conversation.",
        parse_mode="HTML",
    )


async def flush_photos(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    groups = context.application.chat_data[chat_id].get("photo_groups", {})
    buffer = groups.pop(context.job.data, None)
    if not buffer:
        return

    file_ids = buffer["file_ids"][:MAX_PHOTOS]
    note = " ".join(buffer["captions"]).strip()

    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

    content: list[dict] = []
    for file_id in file_ids:
        tg_file = await context.bot.get_file(file_id)
        content.append(image_block(bytes(await tg_file.download_as_bytearray())))
    content.append(
        text_block(
            f"Context from the seller: {note}"
            if note
            else "No context given — describe only what you can see."
        )
    )

    mode = context.application.chat_data[chat_id].pop("mode", None)
    followup_id = context.application.chat_data[chat_id].pop("followup_item", None)

    try:
        if followup_id and (item := store.get(chat_id, followup_id)):
            # She picked an item, then sent a screenshot of its conversation.
            content.insert(0, text_block(store.as_context(item)))
            body = render_reply(
                await asyncio.to_thread(
                    ask_model, REPLY_SYSTEM, content, Reply, chat_id
                )
            )
        elif mode == "new_item":
            # /new — no need to make the model guess what these photos are.
            listing = await asyncio.to_thread(
                ask_model, LISTING_SYSTEM, content, Listing, chat_id
            )
            body = save_and_render(listing, chat_id, note)
        else:
            body = render_photo_result(
                await asyncio.to_thread(ask_model, PHOTO_SYSTEM, content, PhotoResult, chat_id),
                chat_id,
                note,
            )
    except Exception:
        log.exception("photo handling failed for chat %s", chat_id)
        await context.bot.send_message(
            chat_id, "That failed on my end. Send the photos again."
        )
        return

    await context.bot.send_message(chat_id, body, parse_mode="HTML")


def save_and_render(listing: Listing, chat_id: int, facts: str = "") -> str:
    """Remember the item, scoped to this chat, so /followup can offer it later."""
    store.add(
        chat_id,
        listing.title,
        listing.description_fr,
        listing.description_en,
        facts,
    )
    return render_listing(listing)


def render_photo_result(result: PhotoResult, chat_id: int, facts: str = "") -> str:
    if result.kind == "buyer_message" and result.reply:
        return render_reply(result.reply)
    if result.kind == "listing" and result.listing:
        return save_and_render(result.listing, chat_id, facts)
    log.warning("empty %s result for chat %s", result.kind, chat_id)
    return "I couldn't read those images. Send them again, or paste the text."


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    chat_id = update.effective_chat.id

    # One group = one item. Telegram stamps every photo of an album with a shared
    # media_group_id, so two albums sent back to back stay two separate items.
    # Photos sent loose carry no id; those we group by time, since that's someone
    # adding more shots of the same thing.
    group = update.message.media_group_id or "loose"

    groups = context.chat_data.setdefault("photo_groups", {})
    buffer = groups.setdefault(group, {"file_ids": [], "captions": []})
    buffer["file_ids"].append(update.message.photo[-1].file_id)
    if update.message.caption:
        buffer["captions"].append(update.message.caption)

    # Debounce per group, re-armed by each new photo in that group.
    job_name = f"flush-{chat_id}-{group}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
    context.job_queue.run_once(
        flush_photos,
        ALBUM_WINDOW_SECONDS,
        chat_id=chat_id,
        name=job_name,
        data=group,
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    content = []
    # Item context, best source first: an item picked via /suivi, otherwise the
    # listing she used Telegram's reply gesture on.
    followup_id = context.chat_data.pop("followup_item", None)
    quoted = update.message.reply_to_message
    if followup_id and (item := store.get(update.effective_chat.id, followup_id)):
        content.append(text_block(store.as_context(item)))
    elif quoted and quoted.text:
        content.append(text_block(f"Listing in question:\n{quoted.text}"))
    content.append(text_block(f"Buyer's message:\n{update.message.text}"))

    try:
        r = await asyncio.to_thread(
            ask_model, REPLY_SYSTEM, content, Reply, update.effective_chat.id
        )
    except Exception:
        log.exception("reply failed")
        await update.message.reply_text("That failed on my end. Send the message again.")
        return

    await update.message.reply_text(render_reply(r), parse_mode="HTML")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the traceback and tell her something broke.

    Without this, python-telegram-bot swallows every unhandled exception and
    logs "No error handlers are registered" — so on a server she'd see a silent
    non-answer with no clue why.
    """
    log.error("unhandled error", exc_info=context.error)
    chat = getattr(update, "effective_chat", None)
    if chat and is_allowed(update):
        try:
            await context.bot.send_message(
                chat.id, "Something broke on my end. Try again."
            )
        except Exception:
            log.exception("could not report the error to the chat")


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).post_init(publish_commands).build()
    app.add_handler(CommandHandler(["start", "help"], on_start))
    app.add_handler(CommandHandler("new", on_new))
    app.add_handler(CommandHandler("followup", on_followup))
    app.add_handler(CommandHandler("sold", on_sold_command))
    app.add_handler(CommandHandler("items", on_items))
    app.add_handler(CommandHandler("cost", on_cost))
    app.add_handler(CallbackQueryHandler(on_pick))
    for label, handler in (
        (BTN_NEW, on_new),
        (BTN_FOLLOWUP, on_followup),
        (BTN_ITEMS, on_items),
        (BTN_SOLD, on_sold_command),
    ):
        app.add_handler(MessageHandler(filters.Text([label]), handler))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("running with model %s", MODEL)
    app.run_polling()


if __name__ == "__main__":
    main()
