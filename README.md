# Vinted listing bot

A Telegram bot for a private Vinted seller. Two things only:

1. **Send photos of one item** (up to 8, album or one-by-one) → it replies with the title,
   a bilingual description (French paragraph, then English), the Vinted dropdown fields, and
   a suggested price range. Every copyable block is tap-to-copy on iOS and Android.
   Add a caption to the photos to pass context ("Zara, taille M, petit accroc à la manche").
2. **A buyer's message, as pasted text *or* a screenshot of the Vinted chat** → it detects
   the language, translates it to French, and drafts a reply in the buyer's language plus a
   French gloss of what she's about to send. Photos are routed automatically: garment photos
   become a listing, a screenshot of a conversation becomes a reply draft. She doesn't pick a
   mode or type a command.

   For a pasted message, Telegram's **Reply** gesture on that item's listing scopes the
   question to that item. A screenshot usually doesn't need it — Vinted shows the item at the
   top of the thread, so the context is already in the image.

## Commands

| Command | What it does |
|---|---|
| `/new` | New item. Send photos next. |
| `/followup` | Buyer question. Pick the item from a list, then paste the message or send a screenshot. |
| `/sold` | Pick an item to take off the list. |
| `/items` | Show what's still on sale. |
| `/cost` | Running spend tally, overall and per person. |

Commands are optional. Photos or text sent with no command still work — the bot decides
whether it's looking at a garment or a conversation. The commands exist for when she wants to
be explicit, and `/followup` is the only way to attach a *stored* description to a question.

## Deploying to a server

Docker isn't strictly required — a venv and a systemd unit work. It's used here because the
target is someone else's machine: no Python version to negotiate, nothing installed on their
box, and `restart: unless-stopped` covers crashes and reboots, which a bare `nohup` does not.

On the server, once (needs Docker and the Compose plugin):

```bash
git clone git@github.com:mrakhman/vinted_assistant.git && cd vinted_assistant
```

Then create `.env` there — **copy it by hand, never commit it**. `scp` it over, or
`cp .env.example .env` and fill it in. Leave `ITEMS_PATH` / `USAGE_PATH` as they are;
`compose.yaml` overrides them to point at the volume.

```bash
docker compose up -d
docker compose logs -f        # ctrl-C to stop watching; the bot keeps running
```

That's the whole deploy. It survives reboots.

| Task | Command |
|---|---|
| Update after a `git push` | `git pull && docker compose up -d --build` |
| Logs | `docker compose logs -f` (capped at 3 × 10 MB) |
| Stop / start | `docker compose down` / `docker compose up -d` |
| Read her item list | `docker compose exec bot cat /data/items.json` |
| Back it up | `docker compose cp bot:/data/items.json ./items-backup.json` |
| Restart after editing `.env` | `docker compose up -d` (env is read at startup) |

Data lives in a Docker named volume (`bot-data`), not in the project directory, so
`docker compose down` and rebuilds don't touch it. Only `docker compose down -v` deletes it.
To keep the JSON files on the host instead — easier to edit by hand — swap the volume for
`./data:/data` and run `mkdir -p data && sudo chown 10001:10001 data` first (10001 is the
container's user).

### One thing to know about someone else's server

`.env` holds the OpenAI key, and anyone with root on that machine can read it. If the server
owner is also the person using the bot, that's fine. It's another reason to use a dedicated
key in its own project with a spend cap: the worst case is a capped bill on a key that does
nothing else, revocable without touching anything you care about.

## Language rule

**The bot speaks English. Only the listing text is bilingual.** Commands, prompts, labels,
error messages and the item store are English. The description is French first, then English,
because that's what gets pasted into Vinted.

Two deliberate exceptions, both because they're copy-paste targets rather than bot output:
`condition` and `category` come back as Vinted's own French dropdown labels ("Très bon état",
"Femmes > Vestes"), so they match the form she's filling in. Change those to English only if
she runs Vinted in English.

## How items stay separate

Three mechanisms:

- **`/followup` picks from the stored list** by title ("Top noir Zara", "Blazer en laine beige"),
  so a follow-up is always attached to one item.
- **One album = one item.** Telegram stamps every photo in an album with a shared
  `media_group_id`, so two albums sent back to back become two listings. Photos sent loose
  group by time instead — that's someone adding more shots of one thing.
- **Telegram's Reply gesture** on a listing message also scopes a question to that item, for
  when she doesn't feel like running `/followup`.

### The item store

`items.json` — a plain file, one row per item: id, title, both descriptions, and
`active`/`sold`. Not a database: around thirty items, no concurrent writers, and she can open
it in a text editor to fix a title or delete a row. Writes are atomic (temp file + rename) and
a corrupt file is moved aside rather than silently dropped.

**She is the source of truth for what's still on sale**, because there's no Vinted API to tell
the bot when something sold — that's what `/sold` is for. Keeping the list short keeps the
`/followup` picker usable.

It never touches Vinted. No API, no scraping, no automation — she copies and pastes into the
Vinted app. That keeps her account out of reach of Vinted's anti-automation rules.

Runs on the OpenAI API (Responses API, structured outputs, vision).

## Setup

```bash
uv venv .venv && uv pip install -r requirements.txt --python .venv/bin/python
cp .env.example .env   # then fill it in
```

Run it:

```bash
set -a && source .env && set +a && .venv/bin/python bot.py
```

## Testing without an API key

`.env` ships with `DRY_RUN=1`. In that mode the bot needs only a Telegram token: photo
batching, captions, HTML rendering and tap-to-copy are all real, and the model call returns a
canned listing instead of going to OpenAI. Use it to get the Telegram side right, then set
`DRY_RUN=0` and paste the key when you want real descriptions.

## Secrets

- `TELEGRAM_BOT_TOKEN` — message [@BotFather](https://t.me/BotFather), send `/newbot`,
  follow the prompts, copy the token.
- `OPENAI_API_KEY` — from the OpenAI dashboard. Only needed once `DRY_RUN=0`.

`ALLOWED_USER_IDS` is an allowlist so strangers can't spend your API credit. Leave it empty,
message the bot once, and it replies with your Telegram user ID — then paste that in and
restart. Do the same for your friend.

## Cost

`/cost` keeps a local tally in `usage.json` — tokens and dollars, all-time, this month, and per
person. It's a meter, not an invoice: accuracy depends on `PRICE_IN_PER_MTOK` /
`PRICE_OUT_PER_MTOK` matching your model's real rates.

Use a **dedicated API key in its own provider project with a monthly spend cap**. That gives
clean attribution, a hard ceiling the bot can't run past, and a key you can revoke without
breaking anything else.

## Picking the model

`OPENAI_MODEL` defaults to `gpt-5.5`, which is a guess — confirm what your key actually has
before the first real run:

```bash
.venv/bin/python -c "from openai import OpenAI; print([m.id for m in OpenAI().models.list() if 'gpt' in m.id])"
```

It has to be a model that accepts images and supports structured outputs. A smaller/cheaper
model is worth trying first — the descriptions are short and the task isn't hard.

## Notes

- `PRICE_IN_PER_MTOK` / `PRICE_OUT_PER_MTOK` only feed the cost line in the logs. Set them to
  the real rates for your chosen model and every call logs its actual spend.
- Price suggestions come from the model's judgement, not from Vinted sold-price data. Treat
  them as a starting point.
- The bot refuses to draft replies that move a sale off Vinted — that's the standard scam
  pattern there, and it flags it instead.
- Still to do: the bot currently infers brand/size/material from the photos. That's being
  replaced by asking the seller for those fields instead — pending the final field list.
