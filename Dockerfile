# Kite Bot control panel in a container.
#   docker compose up -d --build
# Code lives in /opt/kitebot (read-only, from the image). Everything the app writes — settings,
# Kite token, logs, trades, pid files — lives in /data, a persistent volume. On every start the
# entrypoint copies the code into /data, because the app and bots read and write next to their code.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata \
    KITEBOT_PORT=8765

# procps: app.py uses `ps` to tell whether a bot is still running. tzdata: India time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 kitebot \
    && mkdir /data && chown kitebot:kitebot /data

WORKDIR /opt/kitebot
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=kitebot:kitebot . .
RUN chmod +x docker-entrypoint.sh

USER kitebot
VOLUME /data
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/' % os.environ['KITEBOT_PORT'], timeout=4)"

ENTRYPOINT ["/opt/kitebot/docker-entrypoint.sh"]
