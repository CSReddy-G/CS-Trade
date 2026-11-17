"""
Fair Value Gap (FVG) strategy — shared by bot.py (live/paper) and backtest.py, so both use identical rules.
Chosen in Settings → Strategies, alone or alongside others (strategies.py); the EMA-cross strategy v2 is untouched.

A three-candle imbalance, measured on the INDEX (signal) — the bot buys an ITM option (execution):
  Bullish FVG : candle1.high < candle3.low   zone = [c1.high, c3.low]   → wait for a retrace → BUY CE
  Bearish FVG : candle1.low  > candle3.high  zone = [c3.high, c1.low]   → wait for a retrace → BUY PE
  midpoint = 50% of the zone. Candle 2 is the displacement candle.

QUALITY (every gap is scored; a gap that fails a hard filter is logged as rejected, never traded)
  displacement  c2 body >= FVG_DISP_BODY_ATR x ATR(14) and >= FVG_DISP_BODY_PCT of its range, in the gap's direction
  size          FVG_MIN_SIZE_ATR <= gap / ATR <= FVG_MAX_SIZE_ATR
  volume        c2 relative volume >= FVG_VOLUME_MULT (NIFTYBEES proxy; skipped if unavailable)  [FVG_VOLUME_FILTER]
  trend         price vs EMA50 and EMA9 vs EMA21: "require" | "prefer" (score only) | "off"    [FVG_TREND_MODE]
  structure     BOS / CHoCH through the last confirmed swing, liquidity sweep of the last swing   [FVG_REQUIRE_BOS/SWEEP]
  score 0-100   displacement 25, volume 15, trend 25, BOS 20, sweep 15 — must be >= FVG_MIN_SCORE; the
                highest score wins when several zones qualify on one candle (BOS/sweep zones get priority)

ENTRY (only on a CLOSED candle after the one that formed the gap — never a future candle)
  aggressive    the candle trades into the zone and closes inside/above it (not through the far side)
  balanced      the candle reaches the midpoint and rejects it: closes back beyond the midpoint as a
                bullish (bearish) candle or with a wick >= FVG_REJECTION_WICK_PCT of its range  [default]
  conservative  after a touch, a candle closes in the trade's direction beyond the previous candle's
                high (low), at most FVG_MAX_CHASE_ATR x ATR beyond the zone
  One signal per FVG, ever. A zone dies when price closes through its far side (invalidated), after
  FVG_MAX_TOUCHES separate visits, or after FVG_MAX_AGE candles.

RISK   stop: "fvg" (beyond the far boundary) | "swing" (setup's extreme since candle 1) | "atr" (FVG_SL_ATR_MULT x ATR),
       plus FVG_SL_BUFFER_ATR x ATR; widened to at least FVG_MIN_RISK_ATR x ATR; skipped if wider than FVG_MAX_RISK_ATR.
REWARD target = entry ± FVG_RR x risk (1, 1.5, 2 or 3).
MANAGE strategy.manage (stop to cost at FVG_BREAKEVEN_AT_R, trailing FVG_TRAIL_START_R / FVG_TRAIL_R) plus
       partial booking (FVG_PARTIAL_AT_R / FVG_PARTIAL_PCT), max hold time (FVG_MAX_HOLD_MIN) and, in the caller,
       exit on an opposite FVG signal (FVG_EXIT_ON_OPPOSITE).
"""
import strategy

CFG_DEFAULTS = dict(BE_LOCK_PCT=25, TRAIL_STEP_POINTS=3, 
    FVG_ENTRY_MODE="balanced", FVG_DISP_BODY_ATR=1.0, FVG_DISP_BODY_PCT=0.6, FVG_MIN_SIZE_ATR=0.2,
    FVG_MAX_SIZE_ATR=2.0, FVG_VOLUME_FILTER=True, FVG_VOLUME_MULT=1.5, FVG_VOLUME_AVG_N=20, FVG_TREND_MODE="prefer",
    FVG_EMA_FAST=9, FVG_EMA_SLOW=21, FVG_EMA_TREND=50, FVG_SWING_N=2, FVG_STRUCTURE_LOOKBACK=40,
    FVG_SWEEP_LOOKBACK=6, FVG_REQUIRE_BOS=False, FVG_REQUIRE_SWEEP=False, FVG_MIN_SCORE=50, FVG_MAX_AGE=24,
    FVG_MAX_TOUCHES=2, FVG_MAX_ZONES=10, FVG_REJECTION_WICK_PCT=0.3, FVG_CONFIRM_WINDOW=3, FVG_MAX_CHASE_ATR=0.5,
    FVG_SL_METHOD="fvg", FVG_SL_BUFFER_ATR=0.1, FVG_SL_ATR_MULT=1.0, FVG_MIN_RISK_ATR=0.3, FVG_MAX_RISK_ATR=2.0,
    FVG_RR=2.0, FVG_BREAKEVEN_AT_R=1.0, FVG_TRAIL_START_R=0, FVG_TRAIL_R=1.0, FVG_PARTIAL_AT_R=0,
    FVG_PARTIAL_PCT=50, FVG_EXIT_ON_OPPOSITE=True, FVG_MAX_HOLD_MIN=0, MIN_ATR_PCT=0.05,
)


def _getter(cfg):
    if isinstance(cfg, dict):
        return lambda k: cfg.get(k, CFG_DEFAULTS.get(k))
    return lambda k: getattr(cfg, k, CFG_DEFAULTS.get(k))


# ------------------------------------------------------------------ market structure
def swings(candles, n):
    """Confirmed fractal swings: [(index, level)] for highs and lows. A swing at j needs n candles on each
    side, so it is only known once candle j+n has closed — callers pass only candles that have closed."""
    hi, lo = [], []
    for j in range(n, len(candles) - n):
        h, l = candles[j]["h"], candles[j]["l"]
        side = candles[j - n:j] + candles[j + 1:j + n + 1]
        if all(h > c["h"] for c in side):
            hi.append((j, h))
        if all(l < c["l"] for c in side):
            lo.append((j, l))
    return hi, lo


def structure(candles, i, bull, g):
    """Market structure for a gap whose third candle is candles[i]. Uses only candles[:i+1].
    Returns dict(bos, choch, sweep, swing_high, swing_low)."""
    n, look = int(g("FVG_SWING_N")), int(g("FVG_STRUCTURE_LOOKBACK"))
    start = max(0, i - 2 - look)
    # swings must be confirmed by candle 1 (i-2): nothing the displacement itself created
    hi, lo = swings(candles[start:i - 1], n)
    hi, lo = [(j + start, v) for j, v in hi], [(j + start, v) for j, v in lo]
    out = dict(bos=False, choch=False, sweep=False, swing_high=hi[-1][1] if hi else None,
               swing_low=lo[-1][1] if lo else None)
    c2, c3 = candles[i - 1], candles[i]
    if bull and hi:
        level = hi[-1][1]
        out["bos"] = max(c2["c"], c3["c"]) > level
        # CHoCH: breaking up through a market that was making lower highs
        out["choch"] = out["bos"] and len(hi) >= 2 and hi[-1][1] < hi[-2][1]
    if not bull and lo:
        level = lo[-1][1]
        out["bos"] = min(c2["c"], c3["c"]) < level
        out["choch"] = out["bos"] and len(lo) >= 2 and lo[-1][1] > lo[-2][1]
    sw = int(g("FVG_SWEEP_LOOKBACK"))
    if bull and lo:
        j, level = lo[-1]
        window = candles[max(j + 1, i - 1 - sw):i]          # up to and including the displacement candle
        out["sweep"] = bool(window) and min(c["l"] for c in window) < level and c2["c"] > level
    if not bull and hi:
        j, level = hi[-1]
        window = candles[max(j + 1, i - 1 - sw):i]
        out["sweep"] = bool(window) and max(c["h"] for c in window) > level and c2["c"] < level
    return out


def rel_volume(candles, k, avg_n):
    """Relative volume of candles[k]: its "rvol" if the caller set one (live: NIFTYBEES proxy), else its "v"
    vs the average of the previous avg_n candles. None when volume isn't available."""
    c = candles[k]
    if c.get("rvol") is not None:
        return c["rvol"]
    prior = candles[max(0, k - avg_n):k]
    vols = [p.get("v") for p in prior]
    if not c.get("v") or len(prior) < avg_n or any(not v for v in vols):
        return None
    avg = sum(vols) / avg_n
    return c["v"] / avg if avg > 0 else None


# ------------------------------------------------------------------ detection
def _day(c):
    t = c.get("t")
    return str(t.date()) if hasattr(t, "date") else None


def detect(candles, cfg, instrument="", tf=5, bar=None):
    """Check the three most recent closed candles for an FVG. Returns (zone, None) for a gap that passes the
    quality filters, (zone, reason) for a gap that fails them, or (None, None) when there is no gap."""
    g = _getter(cfg)
    if len(candles) < max(int(g("FVG_EMA_TREND")), 20) + 3:
        return None, None
    i = len(candles) - 1
    c1, c2, c3 = candles[i - 2], candles[i - 1], candles[i]
    if c1["h"] < c3["l"]:
        bull, lower, upper = True, c1["h"], c3["l"]
    elif c1["l"] > c3["h"]:
        bull, lower, upper = False, c3["h"], c1["l"]
    else:
        return None, None
    if _day(c1) is not None and _day(c1) != _day(c3):
        return None, None                                 # an overnight gap is not an intraday imbalance
    a = strategy.atr(candles[-100:], 14)
    if not a:
        return None, None
    size = upper - lower
    rng = c2["h"] - c2["l"]
    body = c2["c"] - c2["o"]
    disp_atr = abs(body) / a
    body_pct = abs(body) / rng if rng > 0 else 0
    rvol = rel_volume(candles, i - 1, int(g("FVG_VOLUME_AVG_N")))
    closes = [c["c"] for c in candles[-250:]]
    ef, es, et = (strategy.ema_series(closes, int(g(k))) for k in ("FVG_EMA_FAST", "FVG_EMA_SLOW", "FVG_EMA_TREND"))
    price = c3["c"]
    if bull:
        above, stacked = price > et[-1], ef[-1] > es[-1]
    else:
        above, stacked = price < et[-1], ef[-1] < es[-1]
    trend = "aligned" if above and stacked else "partial" if above or stacked else "against"
    st = structure(candles, i, bull, g)

    score = min(25.0, 25 * disp_atr / (2 * max(g("FVG_DISP_BODY_ATR"), 0.1)))
    score += 15 if rvol is not None and rvol >= g("FVG_VOLUME_MULT") else 7 if rvol is None else 0
    score += 25 if trend == "aligned" else 10 if trend == "partial" else 0
    score += 20 if st["bos"] else 0
    score += 15 if st["sweep"] else 0

    t = c3.get("t")
    d = "bull" if bull else "bear"
    zone = dict(
        id=f"{instrument or 'IDX'}-{tf}m-{d}-" + (f"{t:%Y%m%d%H%M}" if hasattr(t, "strftime") else f"b{bar}"),
        instrument=instrument, timeframe=f"{tf}m", direction="bullish" if bull else "bearish",
        side="CE" if bull else "PE", upper=round(upper, 2), lower=round(lower, 2), mid=round((upper + lower) / 2, 2),
        size=round(size, 2), size_atr=round(size / a, 2), atr=round(a, 2), formed_at=str(t) if t is not None else None,
        formed_bar=bar, day=_day(c3), candles=[{k: round(c[k], 2) for k in ("o", "h", "l", "c")} for c in (c1, c2, c3)],
        disp_body_atr=round(disp_atr, 2), disp_body_pct=round(body_pct, 2),
        rvol=round(rvol, 2) if rvol is not None else None, trend=trend, bos=st["bos"], choch=st["choch"],
        sweep=st["sweep"], swing_high=st["swing_high"], swing_low=st["swing_low"], score=round(score),
        extreme=c1["l"] if bull else c1["h"],                 # setup's swing extreme, for the "swing" stop
        touches=0, last_touch_bar=None, mitigated=False, invalidated=False, traded=False, state="waiting",
        eligible=True, note="")

    reasons = []
    if (body > 0) != bull or disp_atr < g("FVG_DISP_BODY_ATR"):
        reasons.append(f"weak displacement ({disp_atr:.2f}x ATR, need {g('FVG_DISP_BODY_ATR'):g}x)")
    if body_pct < g("FVG_DISP_BODY_PCT"):
        reasons.append(f"displacement body {body_pct:.0%} of range (need {g('FVG_DISP_BODY_PCT'):.0%})")
    if size / a < g("FVG_MIN_SIZE_ATR"):
        reasons.append(f"gap too small ({size / a:.2f}x ATR)")
    if size / a > g("FVG_MAX_SIZE_ATR"):
        reasons.append(f"gap too large ({size / a:.2f}x ATR)")
    if g("FVG_VOLUME_FILTER") and rvol is not None and rvol < g("FVG_VOLUME_MULT"):
        reasons.append(f"volume {rvol:.2f}x (need {g('FVG_VOLUME_MULT'):g}x)")
    if g("FVG_TREND_MODE") == "require" and trend != "aligned":
        reasons.append(f"trend {trend}")
    if g("FVG_REQUIRE_BOS") and not st["bos"]:
        reasons.append("no break of structure")
    if g("FVG_REQUIRE_SWEEP") and not st["sweep"]:
        reasons.append("no liquidity sweep")
    if not reasons and score < g("FVG_MIN_SCORE"):
        reasons.append(f"score {score:.0f} < {g('FVG_MIN_SCORE'):g}")
    if reasons:
        zone.update(state="rejected", eligible=False, note="; ".join(reasons))
        return zone, zone["note"]
    return zone, None


# ------------------------------------------------------------------ entries
def _entry_ok(z, c, prev, mode, a, g):
    """Does the just-closed candle c trigger an entry on zone z under this entry mode?"""
    bull = z["side"] == "CE"
    rng = c["h"] - c["l"]
    if bull:
        if c["c"] <= z["lower"]:
            return False
        if mode == "aggressive":
            return c["l"] <= z["upper"]
        if mode == "balanced":
            wick = (min(c["o"], c["c"]) - c["l"]) / rng if rng > 0 else 0
            return c["l"] <= z["mid"] and c["c"] > z["mid"] and (c["c"] > c["o"] or wick >= g("FVG_REJECTION_WICK_PCT"))
        touched = z["last_touch_bar"] is not None
        return (touched and c["c"] > c["o"] and c["c"] > prev["h"]
                and c["c"] <= z["upper"] + g("FVG_MAX_CHASE_ATR") * a)
    if c["c"] >= z["upper"]:
        return False
    if mode == "aggressive":
        return c["h"] >= z["lower"]
    if mode == "balanced":
        wick = (c["h"] - max(c["o"], c["c"])) / rng if rng > 0 else 0
        return c["h"] >= z["mid"] and c["c"] < z["mid"] and (c["c"] < c["o"] or wick >= g("FVG_REJECTION_WICK_PCT"))
    touched = z["last_touch_bar"] is not None
    return (touched and c["c"] < c["o"] and c["c"] < prev["l"]
            and c["c"] >= z["lower"] - g("FVG_MAX_CHASE_ATR") * a)


def build_signal(z, candles, a, g):
    """Entry, stop and target on the index for zone z at the close of candles[-1]. Returns (sig, None) or
    (None, reason) when the stop would be too wide."""
    c = candles[-1]
    bull = z["side"] == "CE"
    d = 1 if bull else -1
    entry = c["c"]
    buf = g("FVG_SL_BUFFER_ATR") * a
    method = g("FVG_SL_METHOD")
    since = candles[-(z["age"] + 3):]                  # candle 1 of the gap through the entry candle
    if method == "atr":
        sl = entry - d * g("FVG_SL_ATR_MULT") * a
    elif method == "swing":
        sl = (min(x["l"] for x in since) - buf) if bull else (max(x["h"] for x in since) + buf)
    else:
        sl = (z["lower"] - buf) if bull else (z["upper"] + buf)
    risk = (entry - sl) * d
    if risk < g("FVG_MIN_RISK_ATR") * a:
        risk = g("FVG_MIN_RISK_ATR") * a
        sl = entry - d * risk
    if risk > g("FVG_MAX_RISK_ATR") * a:
        return None, f"stop too wide ({risk / a:.2f}x ATR > {g('FVG_MAX_RISK_ATR'):g}x)"
    rr = g("FVG_RR")
    tags = [t for t, on in (("CHoCH" if z["choch"] else "BOS", z["bos"]), ("sweep", z["sweep"])) if on]
    why = (f"{z['direction']} FVG {z['lower']:.1f}–{z['upper']:.1f} ({g('FVG_ENTRY_MODE')} entry, score {z['score']}"
           + (", " + "+".join(tags) if tags else "") + f", trend {z['trend']}, touch {z['touches']}, ATR {a:.1f})")
    return dict(side=z["side"], entry=entry, sl=sl, target=entry + d * rr * risk, risk=risk, why=why,
                strategy="fvg", fvg_id=z["id"], score=z["score"], fvg=summary(z)), None


def plan(z, cfg):
    """What the bot would do with zone z if price comes back to it: the expected entry (the level the entry rule
    needs; the real entry is that candle's close), the stop, the target and the rules behind them. Mirrors
    _entry_ok / build_signal."""
    g = _getter(cfg)
    bull = z["side"] == "CE"
    d = 1 if bull else -1
    a, mode, method = z["atr"], g("FVG_ENTRY_MODE"), g("FVG_SL_METHOD")
    near, far = (z["upper"], z["lower"]) if bull else (z["lower"], z["upper"])
    if mode == "aggressive":
        entry, rule = near, f"a candle trades into the zone ({near:.1f}) and closes back {'above' if bull else 'below'} {far:.1f}"
    elif mode == "balanced":
        entry, rule = z["mid"], (f"a candle reaches the midpoint {z['mid']:.1f} and rejects it: closes back "
                                 f"{'above' if bull else 'below'} it as a {'green' if bull else 'red'} candle or with a long wick")
    else:
        entry = near + d * g("FVG_MAX_CHASE_ATR") * a / 2
        rule = (f"after a touch, a {'green' if bull else 'red'} candle closes beyond the previous candle's "
                f"{'high' if bull else 'low'}, no further than {near + d * g('FVG_MAX_CHASE_ATR') * a:.1f}")
    buf = g("FVG_SL_BUFFER_ATR") * a
    if method == "atr":
        sl, sl_rule = entry - d * g("FVG_SL_ATR_MULT") * a, f"{g('FVG_SL_ATR_MULT'):g}× ATR from entry"
    elif method == "swing":
        sl, sl_rule = z["extreme"] - d * buf, "beyond the setup's swing extreme + buffer"
    else:
        sl, sl_rule = far - d * buf, f"beyond the zone's far edge {far:.1f} + {g('FVG_SL_BUFFER_ATR'):g}× ATR buffer"
    risk = (entry - sl) * d
    if risk < g("FVG_MIN_RISK_ATR") * a:
        risk = g("FVG_MIN_RISK_ATR") * a
        sl, sl_rule = entry - d * risk, sl_rule + f" (widened to the {g('FVG_MIN_RISK_ATR'):g}× ATR minimum)"
    too_wide = risk > g("FVG_MAX_RISK_ATR") * a
    rr = g("FVG_RR")
    return dict(entry=round(entry, 2), sl=round(sl, 2), target=round(entry + d * rr * risk, 2), risk=round(risk, 2),
                rr=rr, mode=mode, entry_rule=rule, sl_rule=sl_rule,
                target_rule=f"{rr:g}× the risk from entry", skip=("stop would be too wide, the bot would skip it"
                                                                  if too_wide else None))


def summary(z):
    keys = ("id", "direction", "side", "upper", "lower", "mid", "size", "size_atr", "atr", "formed_at", "score",
            "disp_body_atr", "rvol", "trend", "bos", "choch", "sweep", "touches", "mitigated", "invalidated",
            "traded", "state", "eligible", "note")
    return {k: z.get(k) for k in keys}


class Engine:
    """Tracks the FVG zones of one instrument. Call update() exactly once per closed candle, with all closed
    candles so far (oldest first). It never looks past candles[-1], so no future data can leak in."""

    def __init__(self, instrument, timeframe, cfg):
        self.instrument, self.tf, self.cfg, self.g = instrument, timeframe, cfg, _getter(cfg)
        self.zones, self.rejected, self.bar = [], [], 0
        self.signals = []                       # recent signals (for the dashboard / log)

    def update(self, candles, allow_entry=True):
        """Process the newly closed candle candles[-1]: update every live zone, maybe emit ONE entry signal
        (highest score first), then look for a new gap. Returns the signal dict or None."""
        self.bar += 1
        if len(candles) < 4:
            return None
        g = self.g
        c, prev = candles[-1], candles[-2]
        a = strategy.atr(candles[-100:], 14)
        mode = g("FVG_ENTRY_MODE")
        cands = []
        for z in self.zones:
            if not z["eligible"]:
                continue
            z["age"] = self.bar - z["formed_bar"]
            bull = z["side"] == "CE"
            inside = c["l"] <= z["upper"] if bull else c["h"] >= z["lower"]
            was_in = prev["l"] <= z["upper"] if bull else prev["h"] >= z["lower"]
            if inside and (not was_in or z["last_touch_bar"] is None):
                z["touches"] += 1                             # a new visit to the zone
            if inside:
                z["last_touch_bar"] = self.bar
            if (c["l"] <= z["mid"]) if bull else (c["h"] >= z["mid"]):
                z["mitigated"] = True
            if (c["c"] < z["lower"]) if bull else (c["c"] > z["upper"]):
                z.update(invalidated=True, eligible=False, state="invalidated", note="closed through the zone")
                continue
            if z["day"] is not None and _day(c) not in (None, z["day"]):
                z.update(eligible=False, state="expired", note="new session")
                continue
            if z["age"] > g("FVG_MAX_AGE"):
                z.update(eligible=False, state="expired", note=f"older than {g('FVG_MAX_AGE')} candles")
                continue
            if z["touches"] > g("FVG_MAX_TOUCHES"):
                z.update(eligible=False, state="exhausted", note=f"{z['touches']} touches")
                continue
            if inside:
                z["state"] = "in zone"
            recent = z["last_touch_bar"] is not None and self.bar - z["last_touch_bar"] <= g("FVG_CONFIRM_WINDOW")
            if mode == "conservative" and not recent:
                continue
            if allow_entry and a and a >= c["c"] * g("MIN_ATR_PCT") / 100 and _entry_ok(z, c, prev, mode, a, g):
                cands.append(z)
        sig = None
        for z in sorted(cands, key=lambda z: (-z["score"], -z["formed_bar"])):
            sig, why = build_signal(z, candles, a, g)
            if sig:
                z.update(traded=True, eligible=False, state="traded", note=f"signal at {c.get('t') or 'bar %d' % self.bar}")
                sig["fvg"] = summary(z)
                self.signals = (self.signals + [dict(sig, at=str(c.get("t")))])[-10:]
                break
            z["note"] = why
        zone, reason = detect(candles, self.cfg, self.instrument, self.tf, bar=self.bar)
        if zone and reason:
            self.rejected = (self.rejected + [summary(zone)])[-20:]
        elif zone:
            self.zones.append(zone)
        # keep every live zone plus a short history of finished ones
        live = [z for z in self.zones if z["eligible"]][-int(g("FVG_MAX_ZONES")):]
        done = [z for z in self.zones if not z["eligible"]][-10:]
        self.zones = sorted(live + done, key=lambda z: z["formed_bar"])
        return sig

    def replay(self, candles):
        """Rebuild zones from history (bot warm-up). Zones that would already have triggered are spent,
        so a restart never re-enters an old setup."""
        for i in range(4, len(candles) + 1):
            self.update(candles[:i], allow_entry=True)
        self.signals = []

    def snapshot(self):
        return dict(active=[summary(z) for z in self.zones if z["eligible"]],
                    recent=[summary(z) for z in self.zones if not z["eligible"]][-5:],
                    rejected=self.rejected[-5:], signals=[{k: s[k] for k in ("side", "entry", "sl", "target", "fvg_id", "at")}
                                                          for s in self.signals[-5:]])


# ------------------------------------------------------------------ exits
def exit_cfg(cfg):
    """strategy.manage reads RR / BREAKEVEN_AT_R / TRAIL_*; map the FVG settings onto them."""
    g = _getter(cfg)
    return dict(RR=g("FVG_RR"), BREAKEVEN_AT_R=g("FVG_BREAKEVEN_AT_R"), TRAIL_START_R=g("FVG_TRAIL_START_R"),
                TRAIL_R=g("FVG_TRAIL_R"), TIME_STOP_MIN=0,
                BE_LOCK_PCT=g("BE_LOCK_PCT"), TRAIL_STEP_POINTS=g("TRAIL_STEP_POINTS"))


def manage(pos, hi, lo, cfg, now=None, close=None):
    """strategy.manage with the FVG exit settings, plus a hard maximum holding time."""
    lvl, why = strategy.manage(pos, hi, lo, exit_cfg(cfg), now=now, close=close)
    if lvl is not None:
        return lvl, why
    mx = _getter(cfg)("FVG_MAX_HOLD_MIN")
    if mx and now is not None and pos.get("t") is not None and (now - pos["t"]).total_seconds() >= mx * 60:
        return (close if close is not None else (hi + lo) / 2), f"max hold ({mx:g} min)"
    return None, None


def partial_due(pos, hi, lo, cfg):
    """Index level at which to book FVG_PARTIAL_PCT of the position, once, at +FVG_PARTIAL_AT_R x risk.
    Returns the level or None. Marks the position so it fires only once."""
    g = _getter(cfg)
    at = g("FVG_PARTIAL_AT_R")
    if not at or pos.get("partial_done"):
        return None
    d = 1 if pos["side"] == "CE" else -1
    level = pos.get("idx_entry", pos["entry"]) + d * at * pos["risk"]   # index levels (live: "entry" is the option fill)
    if (d == 1 and hi >= level) or (d == -1 and lo <= level):
        pos["partial_done"] = True
        return level
    return None


def partial_qty(qty, lot, cfg):
    """Units to sell for a partial exit: FVG_PARTIAL_PCT of qty, rounded down to whole lots, and never all of it."""
    part = int(qty * _getter(cfg)("FVG_PARTIAL_PCT") / 100) // max(int(lot), 1) * max(int(lot), 1)
    return part if 0 < part < qty else 0
