"""
Nifty / Sensex intraday chart with the main bot's strategy drawn over it (the "Index chart" page).

Candles come from Zerodha's historical API when a panel user is logged in today, else Yahoo (~1 min behind).
The strategies chosen in Settings (first signal wins, as in the bot) are replayed candle by candle on the chosen
timeframe, exactly as the bot evaluates closed candles, and each signal is followed forward to its stop,
target or the square-off time, moving the stop (breakeven / trailing) the way the bot does. Partial exits
are not modelled, and the bot itself trades on CANDLE_MINUTES (5-min) candles. Use Backtest for the full model.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import indicators
import strategies, strategy
from agents import market

IST = ZoneInfo("Asia/Kolkata")
SPECS = {"NIFTY": dict(token=256265, yahoo="^NSEI", name="Nifty 50"),
         "SENSEX": dict(token=265, yahoo="^BSESN", name="Sensex")}
KEEP = 420             # candles kept for the replay (15-min: ~16 sessions, 5-min: ~5 sessions)
WARM = 250             # candles each signal check sees (enough for EMA-50 / ATR / RSI warm-up)


def _mins(v):
    return int(str(v)[:2]) * 60 + int(str(v)[3:5])


def candles(index, tf, creds):
    """[dict(t=datetime IST, o, h, l, c, v)], source. Zerodha first (needs historical-data access), then Yahoo."""
    spec = SPECS[index]
    err = None
    if creds:
        try:
            from kiteconnect import KiteConnect
            k = KiteConnect(api_key=creds[1])
            k.set_access_token(creds[2])
            to = datetime.now(IST)
            rows = k.historical_data(spec["token"], to - timedelta(days=30 if tf == 15 else 10), to, f"{tf}minute")
            out = [dict(t=r["date"].astimezone(IST), o=r["open"], h=r["high"], l=r["low"], c=r["close"], v=r.get("volume") or 0)
                   for r in rows]
            if out:
                return out[-KEEP:], "Zerodha", None
        except Exception as e:
            err = f"Zerodha history unavailable ({str(e)[:90]}), using Yahoo"
    d = market.chart(spec["yahoo"], f"{tf}m", "1mo", max_age=60)
    out = [dict(t=datetime.fromtimestamp(t, IST), o=o, h=h, l=l, c=c, v=v)
           for t, o, h, l, c, v in zip(d["t"], d["o"], d["h"], d["l"], d["c"], d["v"])]
    return out[-KEEP:], "Yahoo (~1 min delay)", err


def _follow(cs, i, sig, cfg, tf):
    """Walk forward from the signal candle to the exit, managing the stop exactly as the bot does on each index
    update (strategy manage(): stop to cost at 1:1, trailing, time stops). The stop is checked first inside a
    candle (the cautious assumption when both are touched) and a stop move counts from the next candle.
    Returns the outcome plus sl_now (the stop now / at exit) and sl_moves [dict(i, sl)] for the chart."""
    bull, sq = sig["side"] == "CE", _mins(cfg["SQUARE_OFF_AT"])
    d = 1 if bull else -1
    owner = strategies.spec_of(sig)
    pos = dict(side=sig["side"], entry=sig["entry"], sl=sig["sl"], target=sig["target"],
               risk=sig.get("risk") or abs(sig["entry"] - sig["sl"]), be_done=False, cost_pts=0,
               best=sig["entry"], t=cs[i]["t"] + timedelta(minutes=tf))
    moves = []
    track = lambda: dict(sl_now=round(pos["sl"], 2), sl_moves=moves, be_done=bool(pos.get("be_done")),
                         trailed=bool(pos.get("trailed")))
    for j in range(i + 1, len(cs)):
        c = cs[j]
        if c["t"].date() != cs[i]["t"].date() or c["t"].hour * 60 + c["t"].minute + tf > sq:
            k = j - 1
            return dict(outcome="square-off", exit_i=k, exit=cs[k]["c"], pts=(cs[k]["c"] - sig["entry"]) * d, **track())
        before = pos["sl"]
        lvl, why = owner.manage(pos, c["h"], c["l"], cfg, now=c["t"] + timedelta(minutes=tf), close=c["c"])
        if lvl is not None:
            outcome = "target" if why.startswith("target") else "stop" if why == "stop-loss" else why
            return dict(outcome=outcome, exit_i=j, exit=lvl, pts=(lvl - sig["entry"]) * d, **track())
        if pos["sl"] != before:
            moves.append(dict(i=j, sl=round(pos["sl"], 2)))
    return dict(outcome="open", exit_i=None, exit=None, pts=(cs[-1]["c"] - sig["entry"]) * d, **track())


def _fvg_zone(z, cs, tf, cfg, start, end, trades):
    """Chart zone for an FVG with what the bot plans to do with it (entry / stop / target), how long it stays
    tradable and why it was drawn."""
    import fvg
    plan = fvg.plan(z, cfg)
    c3 = min(len(cs) - 1, start + 2)
    formed = cs[c3]["t"] + timedelta(minutes=tf)                     # the gap is known once candle 3 closes
    g = fvg._getter(cfg)
    cutoff = formed.replace(hour=_mins(cfg["NO_ENTRY_AFTER"]) // 60, minute=_mins(cfg["NO_ENTRY_AFTER"]) % 60)
    valid_until = max(formed, min(formed + timedelta(minutes=tf * int(g("FVG_MAX_AGE"))), cutoff))
    why = [f"{z['direction'].capitalize()} 3-candle imbalance: candle 1 {'high' if z['side'] == 'CE' else 'low'} "
           f"and candle 3 {'low' if z['side'] == 'CE' else 'high'} don't overlap",
           f"displacement candle body {z['disp_body_atr']}× ATR ({round(z['disp_body_pct'] * 100)}% of its range)",
           f"gap {z['size']} pts = {z['size_atr']}× ATR ({z['atr']})",
           f"trend {z['trend']}" + (", CHoCH" if z.get("choch") else ", break of structure" if z.get("bos") else "")
           + (", liquidity sweep" if z.get("sweep") else ""),
           f"volume {z['rvol']}× average" if z.get("rvol") is not None else "volume not available",
           f"quality score {z['score']}/100 (min {g('FVG_MIN_SCORE'):g})"]
    if formed >= cutoff:
        why.append(f"formed after the {cfg['NO_ENTRY_AFTER']} entry cut-off, so it can't be traded today")
    trade = next((t for t in trades if t.get("fvg_id") == z["id"]), None)
    return dict(kind="FVG", side=z["side"], lower=round(z["lower"], 2), upper=round(z["upper"], 2), mid=z["mid"],
                start=max(0, start), end=end, state=z.get("state"), score=z.get("score"), note=z.get("note") or "",
                formed_at=int(formed.timestamp()), valid_until=int(valid_until.timestamp()),
                touches=z.get("touches"), max_touches=int(g("FVG_MAX_TOUCHES")), plan=plan, why=why,
                trade=dict(entry=round(trade["entry"], 2), sl=round(trade["sl"], 2), target=round(trade["target"], 2),
                           outcome=trade["outcome"]) if trade else None)


def _blocked(blocks, close_t, side, tf, bot_tf):
    """The bot's log line if it saw this signal (same side, logged within one candle of its close) and did not
    take it: agent council HOLD, risk-manager veto, low volume, no contract. None otherwise. Only matched on the
    bot's own candle size: on other candles the bot never saw these signals."""
    if not blocks or tf != bot_tf:
        return None
    lo_t, hi_t = close_t - timedelta(seconds=30), close_t + timedelta(minutes=max(tf, bot_tf), seconds=90)
    return next((b["msg"] for b in blocks if b["side"] == side and lo_t <= b["t"] <= hi_t), None)


def build(index, tf, cfg, creds, blocks=None):
    now = datetime.now(IST)
    cs, source, note = candles(index, tf, creds)
    if len(cs) < 80:
        raise ValueError(f"only {len(cs)} candles")
    last_closed = now >= cs[-1]["t"] + timedelta(minutes=tf)
    done = cs if last_closed else cs[:-1]               # the bot only reads closed candles
    active = strategies.active(cfg)
    ids = [s.id for s in active]
    use_fvg = ids == ["fvg"] or (("fvg" in ids) and "ema" not in ids)    # whose EMA settings are drawn
    lo, hi = _mins(cfg["NO_ENTRY_BEFORE"]), _mins(cfg["NO_ENTRY_AFTER"])
    runs = strategies.runners(active, index, tf, cfg)
    bot_tf = cfg.get("CANDLE_MINUTES", 5)
    row = lambda i, sig, f, **kw: dict(i=i, side=sig["side"], entry=round(sig["entry"], 2), sl=round(sig["sl"], 2),
                                       target=round(sig["target"], 2),
                                       risk=round(sig.get("risk") or abs(sig["entry"] - sig["sl"]), 2),
                                       why=sig.get("why", ""), strategy=strategies.spec_of(sig).name,
                                       sid=strategies.spec_of(sig).id, fvg_id=sig.get("fvg_id"), **f, **kw)
    # 1) what the bot would hold: one position at a time, first signal wins. A signal the bot logged as not taken
    #    (agent council HOLD, risk-manager veto, low volume, no contract...) opens nothing, so the next one is free.
    signals, extra_cands, busy_until = [], [], -1
    for i in range(len(done)):
        c = done[i]
        close_m = c["t"].hour * 60 + c["t"].minute + tf
        ok_time = lo <= close_m <= hi
        fired = strategies.evaluate(runs, done[max(0, i + 1 - WARM):i + 1], ok_time and i > busy_until, None, cfg)[0]
        if not fired:
            continue
        close_t = c["t"] + timedelta(minutes=tf)
        taken = None
        for sig in fired:
            if taken is not None:                       # same candle, lost the tie-break: shown as an overlap
                extra_cands.append((i, sig))
                continue
            blk = _blocked(blocks, close_t, sig["side"], tf, bot_tf)
            f = _follow(done, i, sig, cfg, tf)
            signals.append(row(i, sig, f, bot_skip=blk))
            if not blk:
                taken = signals[-1]
                busy_until = f["exit_i"] if f["exit_i"] is not None else len(done)   # one position at a time

    # 2) signals that fired while that position was open (or lost the tie-break on its candle). The bot can't take
    #    them (MAX_OPEN_POSITIONS), but they are real setups, so they are replayed on fresh strategy state and drawn
    #    on request. Their own outcome is followed as if taken, for comparison only.
    held = [s for s in signals if not s.get("bot_skip")]
    def running(i):
        for n, s in enumerate(signals):
            if not s.get("bot_skip") and s["i"] <= i <= (s["exit_i"] if s["exit_i"] is not None else len(done)):
                return n
        return None
    seen = {(s["i"], s["sid"], s["side"]) for s in signals}
    overlaps = []
    free = strategies.runners(active, index, tf, cfg)
    for i in range(len(done)):
        c = done[i]
        close_m = c["t"].hour * 60 + c["t"].minute + tf
        fired = strategies.evaluate(free, done[max(0, i + 1 - WARM):i + 1], lo <= close_m <= hi, None, cfg)[0]
        for sig in fired:
            key = (i, strategies.spec_of(sig).id, sig["side"])
            n = running(i)
            if key in seen or n is None:
                continue
            seen.add(key)
            overlaps.append(row(i, sig, _follow(done, i, sig, cfg, tf), during=n, same_candle=signals[n]["i"] == i))
    for i, sig in extra_cands:
        key = (i, strategies.spec_of(sig).id, sig["side"])
        if key not in seen:
            seen.add(key)
            overlaps.append(row(i, sig, _follow(done, i, sig, cfg, tf), during=running(i), same_candle=True))
    overlaps.sort(key=lambda s: s["i"])

    closes = [c["c"] for c in cs]
    pre = lambda e, n: [None] * (len(closes) - len(e)) + [round(x, 2) for x in e]
    fast_n, slow_n, trend_n = ((cfg["FVG_EMA_FAST"], cfg["FVG_EMA_SLOW"], cfg["FVG_EMA_TREND"]) if use_fvg
                               else (cfg["EMA_FAST"], cfg["EMA_SLOW"], cfg["EMA_TREND"]))
    emas = [[f"EMA {n}", pre(strategy.ema_series(closes, n), n)] for n in (fast_n, slow_n, trend_n)]   # fast, slow, trend

    zones = []
    for r in runs:
        if r.spec.id == "fvg":
            for z in r.engine.zones:
                start = z["formed_bar"] - 1 - 2               # bar counter is 1-based; the gap starts two candles back
                end = len(cs) - 1 if z["eligible"] else min(len(cs) - 1, start + 2 + (z.get("age") or 3))
                zones.append(_fvg_zone(z, cs, tf, cfg, start, end, signals))
        elif r.spec.id == "ob":
            for z in r.engine.zones:
                start = z["ob_bar"] - 1
                end = len(cs) - 1 if z["eligible"] else min(len(cs) - 1, z["formed_bar"] + 6)
                zones.append(dict(kind="Order block", side=z["side"], lower=round(z["low"], 2), upper=round(z["high"], 2),
                                  start=max(0, start), end=end, state=z.get("state"), score=z.get("score")))
                if z["fvg"]:
                    zones.append(dict(kind="FVG", side=z["side"], lower=round(z["fvg"]["lower"], 2), upper=round(z["fvg"]["upper"], 2),
                                      start=max(0, z["fvg"]["bar"] - 3), end=end, state="confluence", score=None))

    extra, zones = overlays(cs, ids, cfg, tf, zones)
    if not ({"ema", "fvg"} & set(ids)):
        emas = []                                   # the EMA lines belong to EMA cross / FVG; don't clutter the others

    # where the strategy stands on the latest closed candle
    dc = [c["c"] for c in done]
    tr = strategy.ema_series(dc, trend_n)
    fs, ss = strategy.ema_series(dc, fast_n), strategy.ema_series(dc, slow_n)
    r = strategy.rsi(dc[-200:], cfg.get("RSI_PERIOD", 14))
    a = strategy.atr(done[-100:], 14)
    trend = "up" if dc[-1] > tr[-1] and tr[-1] > tr[-6] else "down" if dc[-1] < tr[-1] and tr[-1] < tr[-6] else "sideways"
    cross_ago, cross_side = None, None
    for k in range(1, min(len(fs), len(ss)) - 1):
        a1, b1 = fs[-k] - ss[-k], fs[-k - 1] - ss[-k - 1]
        if (a1 > 0) != (b1 > 0):
            cross_ago, cross_side = k - 1, "bull" if a1 > 0 else "bear"
            break
    closed = [s for s in signals if s["outcome"] != "open" and not s.get("bot_skip")]
    stats = dict(signals=sum(not s.get("bot_skip") for s in signals), wins=sum(s["pts"] > 0 for s in closed),
                 losses=sum(s["pts"] <= 0 for s in closed), pts=round(sum(s["pts"] for s in closed), 1),
                 bot_skipped=sum(bool(s.get("bot_skip")) for s in signals), overlaps=len(overlaps))
    fmt = lambda s: dict(s, pts=round(s["pts"], 1), exit=round(s["exit"], 2) if s["exit"] else None)
    return dict(index=index, name=SPECS[index]["name"], tf=tf, source=source, note=note, at=now.strftime("%H:%M:%S"),
                strategies=[s.name for s in active],
                strat_ids=[dict(id=s.id, short=s.short, name=s.name) for s in active], bot_tf=cfg.get("CANDLE_MINUTES", 5), last_closed=last_closed,
                be_at_r=cfg.get("BREAKEVEN_AT_R") or 0,
                candles=dict(t=[int(c["t"].timestamp()) for c in cs], o=[round(c["o"], 2) for c in cs],
                             h=[round(c["h"], 2) for c in cs], l=[round(c["l"], 2) for c in cs], c=[round(c["c"], 2) for c in cs]),
                emas=emas, extra=extra, signals=[fmt(s) for s in signals], overlaps=[fmt(s) for s in overlaps],
                zones=zones, stats=stats,
                now=dict(trend=trend, rsi=round(r, 1) if r else None, atr=round(a, 1) if a else None, price=dc[-1],
                         cross_side=cross_side, cross_ago=cross_ago, ema_fast=round(fs[-1], 2), ema_slow=round(ss[-1], 2),
                         ema_trend=round(tr[-1], 2), fast_n=fast_n, slow_n=slow_n, trend_n=trend_n,
                         rsi_ce=cfg.get("RSI_BUY_CE"), rsi_pe=cfg.get("RSI_BUY_PE"), rsi_max=cfg.get("RSI_MAX")))


def overlays(cs, ids, cfg, tf, zones):
    """What each newer strategy looks at, drawn on the chart: [name, values aligned to cs, colour, dashed] lines
    and opening-range boxes added to zones."""
    extra, ids = [], set(ids)
    days = [c["t"].date() for c in cs]
    if ids & {"vwap", "orb", "tc"}:
        vw, day, tot, n = [], None, 0.0, 0
        for c, d in zip(cs, days):
            if d != day:
                day, tot, n = d, 0.0, 0
            tot, n = tot + (c["h"] + c["l"] + c["c"]) / 3, n + 1
            vw.append(round(tot / n, 2))
        extra.append(["Session VWAP", vw, "info", False])
    if "supertrend" in ids:
        st = indicators.supertrend(cs, int(cfg.get("ST_ATR_PERIOD", 10)), float(cfg.get("ST_MULT", 3.0)))
        extra.append(["Supertrend (up)", [round(x[0], 2) if x and x[1] == 1 else None for x in st], "up", False])
        extra.append(["Supertrend (down)", [round(x[0], 2) if x and x[1] == -1 else None for x in st], "down", False])
    if "lsmss" in ids:                              # yesterday's high / low: the obvious pools of stops
        hi, lo, prev, cur = [], [], None, None
        for k, (c, d) in enumerate(zip(cs, days)):
            if d != (days[k - 1] if k else None):
                prev, cur = cur, [c["h"], c["l"]]
            else:
                cur = [max(cur[0], c["h"]), min(cur[1], c["l"])]
            hi.append(round(prev[0], 2) if prev else None)
            lo.append(round(prev[1], 2) if prev else None)
        extra += [["Previous-day high", hi, "muted", True], ["Previous-day low", lo, "muted", True]]
    for sid, a, b, until in (("tc", "TC_OR_START", "TC_OR_END", "TC_ENTRY_UNTIL"), ("orb", "ORB_RANGE_START", "ORB_RANGE_END", "NO_ENTRY_AFTER")):
        if sid not in ids or not cfg.get(a) or not cfg.get(b):
            continue
        for d in sorted(set(days)):
            ix = [k for k, c in enumerate(cs) if days[k] == d and _mins(cfg[a]) <= c["t"].hour * 60 + c["t"].minute < _mins(cfg[b])]
            if not ix:
                continue
            last = [k for k, c in enumerate(cs) if days[k] == d and c["t"].hour * 60 + c["t"].minute + tf <= _mins(cfg.get(until) or "15:10")]
            zones.append(dict(kind="Opening range", side=None, lower=round(min(cs[k]["l"] for k in ix), 2),
                              upper=round(max(cs[k]["h"] for k in ix), 2), start=ix[0], end=(last or ix)[-1],
                              state="range", score=None))
        break                                       # one opening range is enough when both are on
    return extra, zones


# ------------------------------------------------------------------ what the bot really did with each signal
import re

_LINE = re.compile(r"^(\d{4}-\d\d-\d\d) \[(\d\d):(\d\d)(?::(\d\d))?\] (.*)$")


def _today_log(lines, day):
    """[(minute of day, message)] for the bot.log lines dated `day` (YYYY-MM-DD)."""
    out = []
    for ln in lines:
        m = _LINE.match(ln.rstrip("\n"))
        if m and m.group(1) == day:
            out.append((int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4) or 0) / 60, m.group(5).strip()))
    return out


_BLOCK = re.compile(r"^(NIFTY|SENSEX): (?:(CE|PE)\b.*?(?:skipped —|trader HOLD|vetoed by risk manager)"
                    r"|no ITM option found for (CE|PE))")


def bot_blocks(log_lines, index):
    """[dict(t, side, msg)]: signals on `index` the bot saw but did not take (agent council / trader HOLD,
    risk-manager veto, low volume, no affordable contract). Being in a trade already isn't one of them: the
    replay models that itself."""
    out = []
    for ln in log_lines:
        m = _LINE.match(ln.rstrip("\n"))
        if not m:
            continue
        b = _BLOCK.match(m.group(5).strip())
        if b and b.group(1) == index:
            t = datetime.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4) or '00'}", "%Y-%m-%d %H:%M:%S")
            out.append(dict(t=t.replace(tzinfo=IST), side=b.group(2) or b.group(3),
                            msg=m.group(5).strip()[len(index) + 1:].strip()))
    return out


def bot_actions(chart, log_lines, cfg):
    """Adds `bot` (short status) and `bot_note` (why) to each of today's signals in a build() result, by reading
    the bot's own log: taken, held by the trader, vetoed, skipped, or never seen (bot off, already in a trade,
    risk-locked, different candle size). Earlier days' signals are dropped: the table shows today only."""
    now = datetime.now(IST)
    day = now.strftime("%Y-%m-%d")
    ts, tf, index = chart["candles"]["t"], chart["tf"], chart["index"]
    bot_tf, max_open = cfg.get("CANDLE_MINUTES", 5), cfg.get("MAX_OPEN_POSITIONS", 1)
    ev = _today_log(log_lines, day)
    today = []
    rows = sorted([*chart["signals"], *[dict(o, overlap=True) for o in chart.get("overlaps", [])]], key=lambda s: s["i"])
    for s in rows:
        t0 = datetime.fromtimestamp(ts[s["i"]], IST)
        if t0.strftime("%Y-%m-%d") != day:
            continue
        T = t0.hour * 60 + t0.minute + tf                    # the signal candle's close
        win = (T - 0.5, T + max(tf, bot_tf) + 1.5)
        bot, note = None, None
        side = s["side"]
        for m, msg in ev:
            if not win[0] <= m <= win[1]:
                continue
            if msg.startswith("ENTRY ") and f"| {index} " in msg and msg.split()[1] == side:
                bot, note = "taken", msg; break
            if msg.startswith(f"{index}:") and (side in msg or "signal" in msg) and not bot:
                bot, note = "skipped", msg[len(index) + 1:].strip()
        if not bot:
            before = [(m, msg) for m, msg in ev if m <= T]
            started = [msg for m, msg in before if msg.startswith("Started ")]
            stopped_after_start = False
            for m, msg in before:
                if msg.startswith("Started "):
                    stopped_after_start = False
                elif msg.startswith(("Day done", "Ctrl+C", "Zerodha refused")):
                    stopped_after_start = True
            open_pos = {}
            for m, msg in before:
                if msg.startswith("ENTRY "):
                    sym = msg.split()[2]
                    mi = re.search(r"\| (NIFTY|SENSEX) ", msg)
                    open_pos[sym] = mi.group(1) if mi else "?"
                elif msg.startswith("EXIT "):
                    open_pos.pop(msg.split()[1], None)
            lock = [msg for m, msg in before if msg.startswith("RISK LOCK")]
            if not started or stopped_after_start:
                bot, note = "not running", "The bot wasn't running when this candle closed."
            elif f" {index}" not in started[-1].split("|")[0]:
                bot, note = "not traded", f"The bot was trading {started[-1].split(' on ')[-1].split('|')[0].strip()} only."
            elif open_pos and len(open_pos) >= max_open:
                bot, note = "busy", (f"Already in a trade ({', '.join(f'{v} {k}' for k, v in open_pos.items())}); "
                                     f"Max open positions is {max_open}, so the bot doesn't look for new entries.")
            elif lock:
                bot, note = "locked", lock[-1]
            elif tf != bot_tf:
                bot, note = "other candles", (f"This replay uses {tf}-min candles; the bot trades {bot_tf}-min candles, "
                                              f"which gave no signal here. Switch the chart to {bot_tf}m to compare.")
            else:
                bot, note = "no signal", ("The bot logged nothing for this candle. Its live candles can differ slightly "
                                          "from the replay's history (feed gaps, warm-up after a restart).")
        today.append(dict(s, bot=bot, bot_note=note))
    return today
