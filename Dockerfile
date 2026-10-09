FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, so code edits don't invalidate the pip layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py prompts.py store.py usage.py ./

# Run as a normal user, and give it a writable /data for the two JSON files.
RUN useradd --uid 10001 --create-home app \
    && mkdir -p /data \
    && chown app:app /data
USER app

# Defaults; compose overrides these so they can't be clobbered by .env.
ENV ITEMS_PATH=/data/items.json \
    USAGE_PATH=/data/usage.json

CMD ["python", "bot.py"]
