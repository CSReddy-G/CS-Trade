"""
WhatsApp and Telegram alerts when a bot opens a trade, through CallMeBot's free APIs.

Telegram (no key): open https://api2.callmebot.com/txt/login.php once, log in with Telegram, then put your
@username in Settings → Alerts.

Setup (once per phone): from that WhatsApp number, send "I allow callmebot to send me messages" to the
CallMeBot number listed at https://www.callmebot.com/blog/free-api-whatsapp-messages/ — it replies with an
API key. Put the number (with country code, e.g. 919030953685) and that key in Settings → Alerts.

Sending runs on a background thread with a short timeout, so a slow or failing alert never delays an order.
"""
import html
import re
import threading
import urllib.parse
import urllib.request

URL = "https://api.callmebot.com/whatsapp.php"
TG_URL = "https://api.callmebot.com/text.php"
TIMEOUT = 15


def _digits(number):
    return "".join(c for c in str(number or "") if c.isdigit())


def enabled(config):
    return bool(getattr(config, "WHATSAPP_ALERTS", False) and _digits(getattr(config, "WHATSAPP_NUMBER", ""))
                and getattr(config, "WHATSAPP_APIKEY", ""))


def tg_users(config):
    """All Telegram usernames from TELEGRAM_USER: one or more, separated by commas, semicolons, spaces or new lines.
    Each person must authorise CallMeBot once (api2.callmebot.com/txt/login.php) to receive messages."""
    raw = str(getattr(config, "TELEGRAM_USER", "") or "")
    out = []
    for u in re.split(r"[\s,;]+", raw):
        u = u.strip().lstrip("@")
        if u and "@" + u.lower() not in (x.lower() for x in out):
            out.append("@" + u)
    return out


def tg_user(config):
    """The first username (kept for older callers)."""
    users = tg_users(config)
    return users[0] if users else ""


def tg_enabled(config):
    return bool(getattr(config, "TELEGRAM_ALERTS", False) and tg_users(config))


def _tg_send(config, text, log, html_mode=False):
    """Send text to every Telegram username, each on its own background thread (one slow or refused user never
    holds up the others)."""
    for user in tg_users(config):
        params = dict(user=user, text=text)
        if html_mode:
            params["html"] = "yes"
        threading.Thread(target=_get, args=(f"Telegram {user}", TG_URL, params, log), daemon=True).start()


def _send(url, params):
    """Send now. Returns (ok, CallMeBot's reply as plain text)."""
    try:
        with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=TIMEOUT) as r:
            body = r.read(4000).decode("utf-8", "replace")
            status = r.status
    except Exception as e:
        return False, str(e)[:200]
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", body)        # drop tracking scripts before the status text
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split())[:300]
    low = body.lower()
    bad = status != 200 or any(w in low for w in ("error", "denied", "not found", "not authori", "invalid", "blocked"))
    return not bad, text or f"HTTP {status}"


def _get(name, url, params, log):
    ok, reply = _send(url, params)
    if not ok:
        log(f"{name} alert not sent: {reply[-200:]}")


def test(config):
    """Send a test message on every channel that's on, right now. Returns [{channel, to, ok, reply}]."""
    out = []
    text = "✅ Kite Bot test alert. If you can read this, trade alerts will reach you here."
    if enabled(config):
        ok, reply = _send(URL, dict(phone="+" + _digits(config.WHATSAPP_NUMBER), text=text, apikey=config.WHATSAPP_APIKEY))
        out.append(dict(channel="WhatsApp", to="+" + _digits(config.WHATSAPP_NUMBER), ok=ok, reply=reply))
    if tg_enabled(config):
        for user in tg_users(config):
            ok, reply = _send(TG_URL, dict(user=user, text=text))
            out.append(dict(channel="Telegram", to=user, ok=ok, reply=reply))
    return out


def whatsapp(config, text, log=print):
    """Send text to the configured WhatsApp number and/or Telegram user in the background.
    Does nothing for a channel that is off."""
    if enabled(config):
        threading.Thread(target=_get, args=("WhatsApp", URL, dict(phone="+" + _digits(config.WHATSAPP_NUMBER), text=text,
                                                                   apikey=config.WHATSAPP_APIKEY), log), daemon=True).start()
    if tg_enabled(config):
        _tg_send(config, text, log)


# ------------------------------------------------------------------ trade card
def inr(v, d=2):
    return "—" if v is None else f"₹{v:,.{d}f}"


def num(v, d=1):
    return "—" if v is None else f"{v:,.{d}f}"


def _card(head, sub, table, notes, fmt):
    """fmt "tg": Telegram HTML (bold + <pre> table); "wa": WhatsApp (*bold* + ``` table)."""
    w = max(len(k) for k, _ in table if k) if table else 0
    rows = "\n".join("─" * 28 if k is None else f"{k:<{w}}  {v}" for k, v in table)
    if fmt == "tg":
        e = html.escape
        return (f"<b>{e(head)}</b>\n{e(sub)}\n<pre>{e(rows)}</pre>\n"
                + "\n".join(f"{icon} <b>{e(k)}:</b> {e(v)}" for icon, k, v in notes))
    return f"*{head}*\n{sub}\n```\n{rows}\n```\n" + "\n".join(f"{icon} *{k}:* {v}" for icon, k, v in notes)


def entry(config, log, *, bot, mode, side, symbol, qty, price, lot=None, index=None, spot=None,
          sl=None, target=None, prem_sl=None, prem_target=None, rr=None, strategy=None, why=None,
          extra=None, at=None, trades_today=None, max_trades=None):
    """A clear, aligned 'trade entered' alert to WhatsApp and/or Telegram."""
    if not (enabled(config) or tg_enabled(config)):
        return
    call = side == "CE"
    head = f"{'🟢' if call else '🔴'} NEW TRADE · BUY {'CALL' if call else 'PUT'} ({side})"
    sub = f"{bot} · {'🔴 LIVE' if str(mode).lower() == 'live' else '📝 PAPER'}"
    lots = f" ({qty // lot} lot{'s' if qty // lot != 1 else ''})" if lot else ""
    t = [("Option", symbol), ("Quantity", f"{qty}{lots}"), ("Entry", inr(price)), ("Capital", inr(price * qty, 0))]
    if index and spot is not None:
        t += [(None, None), (index, num(spot))]
        d = 1 if call else -1
        if sl is not None:
            t.append(("Stop-loss", f"{num(sl)}  ({(sl - spot) * d:+.1f} pts)"))
        if target is not None:
            t.append(("Target", f"{num(target)}  ({(target - spot) * d:+.1f} pts)"))
    elif sl is not None or target is not None:
        t += [(None, None), ("Stop-loss", str(sl)), ("Target", str(target))]
    if prem_sl or prem_target:
        t.append((None, None))
        if prem_sl:
            t.append(("Option SL", f"{inr(prem_sl)}  (≈ {inr((prem_sl - price) * qty, 0)})"))
        if prem_target:
            t.append(("Option TGT", f"{inr(prem_target)}  (≈ +{inr((prem_target - price) * qty, 0)})"))
    if rr:
        t.append(("Risk:Reward", f"1 : {rr:.1f}"))
    notes = []
    if strategy:
        notes.append(("📊", "Strategy", strategy))
    if why:
        notes.append(("💡", "Why", why))
    for k, v in (extra or []):
        notes.append(("🤖", k, v))
    when = at or ""
    if trades_today is not None:
        when += f" · trade {trades_today}" + (f" of {max_trades} today" if max_trades else " today")
    if when:
        notes.append(("🕙", "Time", when.strip(" ·")))
    tg = _card(head, sub, t, notes, "tg")
    wa = _card(head, sub, t, notes, "wa")
    if enabled(config):
        threading.Thread(target=_get, args=("WhatsApp", URL, dict(phone="+" + _digits(config.WHATSAPP_NUMBER), text=wa,
                                                                   apikey=config.WHATSAPP_APIKEY), log), daemon=True).start()
    if tg_enabled(config):
        _tg_send(config, tg, log, html_mode=True)


def skipped(config, log, *, bot="Main bot", mode="paper", index, spot=None, side, strategy=None, entry=None,
            sl=None, target=None, reason, why=None, symbol=None, at=""):
    """'Signal skipped' alert: the rules fired but the bot did not trade it, and why. Needs SKIP_ALERTS on."""
    if not getattr(config, "SKIP_ALERTS", False) or not (enabled(config) or tg_enabled(config)):
        return
    call = side == "CE"
    head = f"⏭️ SKIPPED · {'CALL' if call else 'PUT'} ({side}) signal"
    sub = f"{bot} · {'🔴 LIVE' if str(mode).lower() == 'live' else '📝 PAPER'} · no order placed"
    t = [("Index", index)]
    if spot is not None:
        t.append(("Now", num(spot)))
    if symbol:
        t.append(("Option", symbol))
    d = 1 if call else -1
    if entry is not None:
        t += [(None, None), ("Signal entry", num(entry))]
        if sl is not None:
            t.append(("Stop-loss", f"{num(sl)}  ({(sl - entry) * d:+.1f} pts)"))
        if target is not None:
            t.append(("Target", f"{num(target)}  ({(target - entry) * d:+.1f} pts)"))
    notes = [("🚫", "Skipped because", reason)]
    if strategy:
        notes.append(("📊", "Strategy", strategy))
    if why:
        notes.append(("💡", "Signal", why))
    if at:
        notes.append(("🕙", "Time", at))
    if enabled(config):
        threading.Thread(target=_get, args=("WhatsApp", URL, dict(phone="+" + _digits(config.WHATSAPP_NUMBER),
                                                                   text=_card(head, sub, t, notes, "wa"),
                                                                   apikey=config.WHATSAPP_APIKEY), log), daemon=True).start()
    if tg_enabled(config):
        _tg_send(config, _card(head, sub, t, notes, "tg"), log, html_mode=True)


def heads_up(config, log, *, index, spot, side, stage, text, level=None, tf=5, at=""):
    """'Get ready' alert: a signal may fire soon. Not a trade."""
    if not (enabled(config) or tg_enabled(config)):
        return
    word = "CALL (CE)" if side == "CE" else "PUT (PE)"
    head = f"⚠️ GET READY · possible {word}"
    sub = f"{index} · {stage} · not a trade yet"
    t = [("Index", index), ("Now", num(spot))]
    if level is not None:
        t.append(("Watch level", num(level)))
    t += [("Candles", f"{tf}-min"), ("Time", at)]
    notes = [("👀", "Setup", text), ("ℹ️", "Note", "The bot enters only if the rules confirm; you'll get an ENTRY alert if it does.")]
    if enabled(config):
        threading.Thread(target=_get, args=("WhatsApp", URL, dict(phone="+" + _digits(config.WHATSAPP_NUMBER),
                                                                   text=_card(head, sub, t, notes, "wa"),
                                                                   apikey=config.WHATSAPP_APIKEY), log), daemon=True).start()
    if tg_enabled(config):
        _tg_send(config, _card(head, sub, t, notes, "tg"), log, html_mode=True)
