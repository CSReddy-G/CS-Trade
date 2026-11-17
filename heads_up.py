"""
Early warnings: "a trade signal may come on the next candle(s)". Checked on every closed candle by the main bot,
sent to Telegram / WhatsApp and the bot log, so you can be ready before the entry fires.

These are NOT signals and never place orders. They read the same indicators and settings as the real rules:
  EMA cross (strategy.signal_v2)
    "cross coming"   trend (and 15-min trend) agree, volatility is enough, and EMA fast is closing on EMA slow
    "confirm next"   the cross has happened with the trend; the signal needs one candle that closes beyond the
                     last candle's high/low (green for calls, red for puts) with RSI in range — the exact level is given
  FVG / Order block
    "near zone"      price is within half an ATR of a live zone that would give a retest entry
Each warning is sent once (per index, side, stage and setup), so they don't repeat every candle.
"""
import strategy

COOLDOWN_MIN = 30                 # a repeat "cross coming" for the same index and side waits this long


def _g(cfg, k, d=None):
    return cfg.get(k, d) if isinstance(cfg, dict) else getattr(cfg, k, d)


def ema_watch(candles, cfg):
    """[dict(side, stage, key, text)] for the EMA-cross rules on these closed candles."""
    slow_n = _g(cfg, "EMA_TREND")
    if len(candles) < slow_n + 10:
        return []
    closes = [c["c"] for c in candles]
    fast = strategy.ema_series(closes, _g(cfg, "EMA_FAST"))
    slow = strategy.ema_series(closes, _g(cfg, "EMA_SLOW"))
    trend = strategy.ema_series(closes, slow_n)
    look = _g(cfg, "CROSS_LOOKBACK", 3)
    diff = [f - s for f, s in zip(fast[-(look + 1):], slow[-(look + 1):])]
    r = strategy.rsi(closes[-200:], _g(cfg, "RSI_PERIOD", 14))
    a = strategy.atr(candles[-100:], 14)
    if r is None or not a:
        return []
    cur, price = candles[-1], closes[-1]
    if a < price * _g(cfg, "MIN_ATR_PCT", 0) / 100:
        return []
    adx_min = _g(cfg, "ADX_MIN", 0)
    if adx_min:
        x = strategy.adx(candles[-150:], 14)
        if x is None or x < adx_min:
            return []
    up = price > trend[-1] and trend[-1] > trend[-6]
    dn = price < trend[-1] and trend[-1] < trend[-6]
    if _g(cfg, "HTF_FILTER", False):
        h = strategy.resample(candles, 3)
        e = strategy.ema_series([c["c"] for c in h], 20)
        if len(e) < 3:
            return []
        up = up and h[-1]["c"] > e[-1] and e[-1] > e[-3]
        dn = dn and h[-1]["c"] < e[-1] and e[-1] < e[-3]
    ce_lo, rmax, pe_hi = _g(cfg, "RSI_BUY_CE"), _g(cfg, "RSI_MAX"), _g(cfg, "RSI_BUY_PE")
    fn, sn = _g(cfg, "EMA_FAST"), _g(cfg, "EMA_SLOW")
    out, gap = [], diff[-1]
    bar = cur.get("t")
    for side, ok, sign in (("CE", up, 1), ("PE", dn, -1)):
        if not ok:
            continue
        crossed = sign * diff[-1] > 0 and any(sign * d <= 0 for d in diff[:-1])
        word, need = ("CALL", "green and above") if side == "CE" else ("PUT", "red and below")
        lvl = cur["h"] if side == "CE" else cur["l"]
        rsi_ok = ce_lo <= r <= rmax if side == "CE" else 100 - rmax <= r <= pe_hi
        rsi_rule = f"{ce_lo:g}–{rmax:g}" if side == "CE" else f"{100 - rmax:g}–{pe_hi:g}"
        if crossed:
            text = (f"EMA {fn}/{sn} {'bull' if side == 'CE' else 'bear'} cross is in, trend agrees. "
                    f"{word} triggers if the next candle closes {need} {lvl:,.1f} with RSI {rsi_rule} "
                    f"(RSI now {r:.0f}{'' if rsi_ok else ' — outside the range'}).")
            # key on the last candle that was on the other side of the cross, so one cross = one warning
            k = max(i for i, d in enumerate(diff[:-1]) if sign * d <= 0)
            out.append(dict(side=side, stage="confirm next", level=lvl, text=text,
                            key=f"x{side}{str(candles[-(len(diff) - k)].get('t'))}"))
        elif sign * gap < 0 and abs(gap) <= 0.25 * a and abs(diff[-1]) < abs(diff[-2]) < abs(diff[-3]):
            text = (f"EMA {fn} is {abs(gap):.1f} pts {'below' if side == 'CE' else 'above'} EMA {sn} and closing fast; "
                    f"trend is {'up' if side == 'CE' else 'down'}. A {'bull' if side == 'CE' else 'bear'} cross may come "
                    f"in the next 1–2 candles, then a {word} needs a {need.split()[0]} candle with RSI {rsi_rule} "
                    f"(RSI now {r:.0f}).")
            out.append(dict(side=side, stage="cross coming", level=None, text=text, key=f"c{side}{bar}"))
    return out


def zone_watch(runs, candles):
    """Price within half an ATR of a live FVG / order-block zone."""
    a = strategy.atr(candles[-100:], 14)
    if not a or not candles:
        return []
    price, out = candles[-1]["c"], []
    for r in runs:
        eng = getattr(r, "engine", None)
        if not eng:
            continue
        for z in getattr(eng, "zones", []):
            if not z.get("eligible") or z.get("state") in ("traded", "invalidated", "expired", "rejected", "exhausted"):
                continue
            lo, hi = (z["lower"], z["upper"]) if "lower" in z else (z["low"], z["high"])
            dist = price - hi if z["side"] == "CE" else lo - price      # calls retest from above, puts from below
            if -0.1 * a <= dist <= 0.5 * a:
                kind = "Order block" if r.spec.id == "ob" else "FVG"
                word = "CALL" if z["side"] == "CE" else "PUT"
                text = (f"Price {price:,.1f} is {'in' if dist <= 0 else f'{dist:.1f} pts from'} a "
                        f"{'bullish' if z['side'] == 'CE' else 'bearish'} {kind} zone {lo:,.1f}–{hi:,.1f}. "
                        f"A {word} fires if it retests and {'holds above' if z['side'] == 'CE' else 'rejects below'} it.")
                out.append(dict(side=z["side"], stage="near zone", level=hi if z["side"] == "CE" else lo, text=text,
                                key=f"z{r.spec.id}{z.get('formed_bar')}{z['side']}"))
    return out


class Watcher:
    """Sends each warning once per day."""

    def __init__(self):
        self.sent, self.day, self.last = set(), None, {}

    def check(self, index, runs, candles, cfg, day):
        if day != self.day:
            self.sent, self.day, self.last = set(), day, {}
        ids = {r.spec.id for r in runs}
        found = (ema_watch(candles, cfg) if "ema" in ids else []) + zone_watch(runs, candles)
        new, t = [], candles[-1].get("t")
        for w in found:
            k = (index, w["stage"], w["key"])
            if w["stage"] == "cross coming":       # one warning per approach, not one per candle
                last = self.last.get((index, w["side"]))
                if last is not None and t is not None and (t - last).total_seconds() < COOLDOWN_MIN * 60:
                    continue
                self.last[(index, w["side"])] = t
            if k not in self.sent:
                self.sent.add(k)
                new.append(w)
        return new
