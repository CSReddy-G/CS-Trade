"""
Order Block strategy: liquidity sweep → market structure shift → order block → retest → rejection → confirmation.
Shared by bot.py (live/paper), backtest.py and index_chart.py, so all three use identical rules.

SETUP  on closed candles only, in this order:
  1 SWEEP         a candle trades through the last unswept confirmed swing and closes back on the other side, with
                  a meaningful rejection wick. A swing with n candles each side is only known once candle j+n closed.
  2 DISPLACEMENT  a later strong candle (body >= OB_DISPLACEMENT_ATR x ATR) closes through the last confirmed swing
                  on the other side. After a sweep that is an MSS; without one (sweep not required) it is a BOS.
  3 ORDER BLOCK   the last opposite-coloured candle before the displacement. Zone by OB_ZONE_MODE:
                  OPEN_TO_WICK (default), FULL_CANDLE or BODY. A close beyond it invalidates it; trading through its
                  far side fully mitigates it. Either way it is finished.
  4 FVG           a three-candle gap around the displacement that overlaps the zone: adds score, and with
                  OB_REQUIRE_FVG it is mandatory.
ENTRY  never on a touch:
  5 REJECTION     a closed candle enters the zone and rejects it
  6 CONFIRMATION  the next closed candle breaks the rejection candle in the trade direction → signal
FILTERS  15-min trend (completed 15-min candles only), VWAP, volume, stop width, distance from the zone, minimum
         reward:risk, score threshold, one signal per zone, OB_MAX_TRADES_PER_DAY.

The score ranks setup quality against the checklist. It is NOT a probability of winning.
"""
import fvg
import strategy

CFG_DEFAULTS = dict(BE_LOCK_PCT=25, TRAIL_STEP_POINTS=3,
    OB_HTF_MINUTES=15, OB_HTF_EMA=20, OB_REQUIRE_HTF=True,
    OB_SWING_LOOKBACK=3, OB_STRUCTURE_LOOKBACK=60,
    OB_REQUIRE_SWEEP=True, OB_SWEEP_MIN_WICK_PCT=0.4, OB_SWEEP_MAX_AGE=12,
    OB_REQUIRE_MSS=True, OB_DISPLACEMENT_ATR=1.0, OB_DISPLACEMENT_BODY_PCT=0.55,
    OB_ZONE_MODE="OPEN_TO_WICK", OB_SEARCH_BACK=8, OB_MAX_ZONE_ATR=1.5, OB_MAX_AGE=36, OB_MAX_ZONES=10,
    OB_REQUIRE_FVG=False, OB_FVG_MIN_OVERLAP_PCT=10.0,
    OB_REQUIRE_VOLUME=False, OB_VOLUME_MULT=1.2, OB_VOLUME_AVG_N=20, OB_REQUIRE_VWAP=False,
    OB_REJECTION_WICK_PCT=0.3, OB_MAX_DISTANCE_ATR=1.0,
    OB_SL_METHOD="wider", OB_SL_BUFFER_ATR=0.1, OB_MAX_RISK_ATR=2.5,
    OB_TARGET_MODE="rr", OB_RR=2.0, OB_MIN_RR=2.0,
    OB_BREAKEVEN_AT_R=1.0, OB_TRAIL_START_R=0, OB_TRAIL_R=1.0, OB_PARTIAL_AT_R=0, OB_PARTIAL_PCT=50,
    OB_PREMIUM_EXITS=True, OB_OPTION_DELTA=0.6,
    OB_MAX_TRADES_PER_DAY=2, OB_MIN_SCORE=75,
    OB_SCORE_SWEEP=20, OB_SCORE_MSS=20, OB_SCORE_DISPLACEMENT=15, OB_SCORE_OB=15, OB_SCORE_FVG=15, OB_SCORE_HTF=10,
    OB_SCORE_VOLUME=5, MIN_ATR_PCT=0.05,
)
ZONE_MODES = ("OPEN_TO_WICK", "FULL_CANDLE", "BODY")


def _getter(cfg):
    if isinstance(cfg, dict):
        return lambda k: cfg.get(k, CFG_DEFAULTS.get(k))
    return lambda k: getattr(cfg, k, CFG_DEFAULTS.get(k))


def _day(c):
    return fvg._day(c)


# ------------------------------------------------------------------ detection stages (each testable on its own)
def detect_sweep(candles, n, min_wick, side):
    """Did the last candle sweep liquidity for a `side` trade? The swing must have been confirmed BEFORE that candle,
    and must still be unswept. Returns (sweep dict or None, reason a sweep was rejected or None)."""
    if len(candles) < 2 * n + 2:
        return None, None
    c, past = candles[-1], candles[:-1]
    hi, lo = fvg.swings(past, n)
    bull = side == "CE"
    for j, level in reversed(lo if bull else hi):
        later = past[j + 1:]
        if any(x["l"] < level for x in later) if bull else any(x["h"] > level for x in later):
            continue                                       # already swept: no liquidity left at this level
        if (c["l"] >= level) if bull else (c["h"] <= level):
            return None, None
        if (c["c"] <= level) if bull else (c["c"] >= level):
            return None, None                              # closed beyond it: a break, not a sweep
        rng = c["h"] - c["l"]
        wick = ((min(c["o"], c["c"]) - c["l"]) if bull else (c["h"] - max(c["o"], c["c"]))) / rng if rng > 0 else 0
        if wick < min_wick:
            return None, f"weak sweep of {level:.1f} (wick {wick:.0%}, need {min_wick:.0%})"
        return dict(side=side, level=level, extreme=c["l"] if bull else c["h"], wick=round(wick, 2),
                    t=str(c.get("t") or "")), None
    return None, None


def is_displacement(c, atr, atr_mult, body_pct, side):
    """A strong candle in the trade direction: (ok, body in ATR)."""
    body = c["c"] - c["o"]
    rng = c["h"] - c["l"]
    if not atr or rng <= 0 or (body > 0) != (side == "CE") or body == 0:
        return False, 0.0
    size = abs(body) / atr
    return size >= atr_mult and abs(body) / rng >= body_pct, size


def structure_break(candles, n, side):
    """Does the last candle freshly close through the last swing that was confirmed before it? Returns the level or None."""
    if len(candles) < 2 * n + 2:
        return None
    c, past = candles[-1], candles[:-1]
    hi, lo = fvg.swings(past, n)
    swings_ = hi if side == "CE" else lo
    if not swings_:
        return None
    level = swings_[-1][1]
    prev = past[-1]["c"]
    if side == "CE":
        return level if c["c"] > level and prev <= level else None
    return level if c["c"] < level and prev >= level else None


def find_order_block(candles, disp_i, side, back):
    """Index of the last opposite-coloured candle before candles[disp_i], or None. Stays within the session."""
    day = _day(candles[disp_i])
    for k in range(disp_i - 1, max(-1, disp_i - 1 - back), -1):
        c = candles[k]
        if _day(c) != day:
            return None
        if (c["c"] < c["o"]) if side == "CE" else (c["c"] > c["o"]):
            return k
    return None


def zone_bounds(c, side, mode):
    """(low, high) of the order block candle. Default: open-to-low for a bullish block, open-to-high for a bearish one."""
    if mode == "FULL_CANDLE":
        return c["l"], c["h"]
    if mode == "BODY":
        return min(c["o"], c["c"]), max(c["o"], c["c"])
    return (c["l"], c["o"]) if side == "CE" else (c["o"], c["h"])


def fvg_at(candles, i, side):
    """The three-candle gap ending at candles[i], if it points the `side` way, as (lower, upper). Same session only."""
    if i < 2 or _day(candles[i - 2]) != _day(candles[i]):
        return None
    c1, c3 = candles[i - 2], candles[i]
    if side == "CE" and c3["l"] > c1["h"]:
        return c1["h"], c3["l"]
    if side == "PE" and c3["h"] < c1["l"]:
        return c3["h"], c1["l"]
    return None


def overlap_pct(a_low, a_high, b_low, b_high):
    """Overlap of two ranges as a % of the narrower one."""
    width = min(a_high - a_low, b_high - b_low)
    shared = min(a_high, b_high) - max(a_low, b_low)
    return 100.0 * shared / width if width > 0 and shared > 0 else 0.0


def htf_candles(candles, tf, minutes):
    """Completed higher-timeframe candles built from closed candles. A bucket counts only once its last slot has
    closed, so the 15-min trend can't see into a 15-min candle that is still forming. Buckets start at 09:15."""
    out, key, cur = [], None, None
    for c in candles:
        t = c.get("t")
        if not hasattr(t, "hour"):
            return []
        mins = t.hour * 60 + t.minute - 555
        k = (str(t.date()), mins // minutes)
        if k != key:
            if cur is not None and cur["end"]:
                out.append(cur)
            key, cur = k, dict(o=c["o"], h=c["h"], l=c["l"], c=c["c"], end=False)
        else:
            cur.update(h=max(cur["h"], c["h"]), l=min(cur["l"], c["l"]), c=c["c"])
        cur["end"] = (mins + tf) % minutes == 0
    if cur is not None and cur["end"]:
        out.append(cur)
    return out


def htf_trend(candles, tf, minutes, ema_n):
    """'up' / 'down' / 'flat' on completed higher-timeframe candles, or None without enough history."""
    closes = [c["c"] for c in htf_candles(candles, tf, minutes)]
    e = strategy.ema_series(closes, ema_n)
    if len(e) < 2:
        return None
    if closes[-1] > e[-1] and e[-1] > e[-2]:
        return "up"
    if closes[-1] < e[-1] and e[-1] < e[-2]:
        return "down"
    return "flat"


def session_vwap(candles):
    """Today's VWAP through the last candle. Indices have no volume of their own: uses the candle's proxy volume "v"
    when every candle has it, otherwise an equal-weight average of typical prices."""
    day = _day(candles[-1])
    today = [c for c in candles if _day(c) == day]
    tp = [(c["h"] + c["l"] + c["c"]) / 3 for c in today]
    if all(c.get("v") for c in today):
        return sum(p * c["v"] for p, c in zip(tp, today)) / sum(c["v"] for c in today)
    return sum(tp) / len(tp)


def score_setup(z, g, htf_ok, vol_ok):
    """Points against the checklist, as (score, parts). Ranks quality; not a probability of winning."""
    parts = dict(sweep=g("OB_SCORE_SWEEP") if z["sweep"] else 0, mss=g("OB_SCORE_MSS") if z["kind"] == "MSS" else 0,
                 displacement=g("OB_SCORE_DISPLACEMENT") if z["disp_atr"] >= g("OB_DISPLACEMENT_ATR") else 0,
                 order_block=g("OB_SCORE_OB"), fvg=g("OB_SCORE_FVG") if z["fvg"] else 0,
                 htf=g("OB_SCORE_HTF") if htf_ok else 0, volume=g("OB_SCORE_VOLUME") if vol_ok else 0)
    return min(100, sum(parts.values())), parts


def plan_exits(z, entry, atr, hi_swings, lo_swings, g):
    """Stop and target on the index. Returns (sl, target, risk, None) or (None, None, None, reason)."""
    bull = z["side"] == "CE"
    d = 1 if bull else -1
    edge = z["low"] if bull else z["high"]
    far = z["sweep"]["extreme"] if z["sweep"] else edge
    method = g("OB_SL_METHOD")
    base = far if method == "sweep" else edge if method == "ob" else (min(edge, far) if bull else max(edge, far))
    sl = base - d * g("OB_SL_BUFFER_ATR") * atr
    risk = (entry - sl) * d
    if risk <= 0:
        return None, None, None, "stop on the wrong side of the entry"
    if risk > g("OB_MAX_RISK_ATR") * atr:
        return None, None, None, f"stop too wide ({risk / atr:.2f}x ATR, max {g('OB_MAX_RISK_ATR'):g}x)"
    mode, min_rr = g("OB_TARGET_MODE"), g("OB_MIN_RR")
    if mode == "rr":
        target = entry + d * g("OB_RR") * risk
    else:
        pool = sorted((lvl for _, lvl in (hi_swings if bull else lo_swings) if (lvl - entry) * d > 0), key=lambda v: (v - entry) * d)
        if mode == "liquidity":                            # nearest opposing liquidity far enough to pay min_rr
            pool = [lvl for lvl in pool if (lvl - entry) * d / risk >= min_rr] or pool
        if not pool:
            return None, None, None, "no opposing swing to target"
        target = pool[0]
    rr = (target - entry) * d / risk
    if rr < min_rr - 1e-9:
        return None, None, None, f"reward:risk 1:{rr:.2f}, needs 1:{min_rr:g}"
    return sl, target, risk, None


def summary(z):
    keys = ("id", "side", "direction", "low", "high", "invalidation", "formed_at", "kind", "structure_level", "sweep", "fvg",
            "fvg_overlap", "disp_atr", "rvol", "state", "eligible", "touched", "score", "note")
    return {k: z.get(k) for k in keys}


class Engine:
    """Tracks the order blocks of one instrument. Call update() exactly once per closed candle, with the closed
    candles so far (oldest first). It never looks past candles[-1], so no future data can leak in."""

    def __init__(self, instrument, timeframe, cfg):
        self.instrument, self.tf, self.cfg, self.g = instrument, timeframe, cfg, _getter(cfg)
        self.zones, self.rejected, self.signals, self.bar = [], [], [], 0
        self.sweeps = dict(CE=None, PE=None)           # latest usable sweep per trade direction
        self.day, self.day_signals = None, 0

    def _reject(self, c, side, stage, reason, zone=None, score=None):
        self.rejected = (self.rejected + [dict(at=str(c.get("t") or ""), side=side, stage=stage, reason=reason,
                                               zone=zone, score=score)])[-20:]

    # ---- zones
    def _new_zone(self, candles, side, kind, level, sweep, atr, size):
        i, g = len(candles) - 1, self.g
        k = find_order_block(candles, i, side, int(g("OB_SEARCH_BACK")))
        c = candles[i]
        if k is None:
            self._reject(c, side, "order block", "no opposite candle before the displacement")
            return
        low, high = zone_bounds(candles[k], side, g("OB_ZONE_MODE"))
        if high <= low:
            self._reject(c, side, "order block", "zone has no width")
            return
        if high - low > g("OB_MAX_ZONE_ATR") * atr:
            self._reject(c, side, "order block", f"zone too wide ({(high - low) / atr:.2f}x ATR, max {g('OB_MAX_ZONE_ATR'):g}x)")
            return
        bull = side == "CE"
        z = dict(id=f"{self.instrument}-{self.tf}m-{'bull' if bull else 'bear'}-{candles[k].get('t') or self.bar}",
                 side=side, direction="bullish" if bull else "bearish", low=low, high=high,
                 invalidation=low if bull else high, formed_at=str(c.get("t") or ""), formed_bar=self.bar,
                 ob_bar=self.bar - (i - k), kind=kind, structure_level=level, sweep=sweep, day=_day(c),
                 disp_atr=round(size, 2), rvol=fvg.rel_volume(candles, i, int(g("OB_VOLUME_AVG_N"))),
                 fvg=None, fvg_overlap=0.0, state="waiting", eligible=True, touched=False, pending=None, score=None, note="")
        for j in range(max(2, k), i + 1):              # gaps already complete at the displacement
            self._attach_fvg(z, candles, j)
        self.zones.append(z)
        live = [x for x in self.zones if x["eligible"]][-int(g("OB_MAX_ZONES")):]
        done = [x for x in self.zones if not x["eligible"]][-10:]
        self.zones = sorted(live + done, key=lambda x: x["formed_bar"])

    def _attach_fvg(self, z, candles, i):
        gap = fvg_at(candles, i, z["side"])
        if not gap:
            return
        pct = overlap_pct(z["low"], z["high"], *gap)
        if pct > z["fvg_overlap"]:
            z["fvg_overlap"] = round(pct, 1)
        if pct >= self.g("OB_FVG_MIN_OVERLAP_PCT") and pct > 0:
            z["fvg"] = dict(lower=gap[0], upper=gap[1], overlap=round(pct, 1), bar=self.bar - (len(candles) - 1 - i))

    def _finish(self, z, state, note):
        z.update(state=state, eligible=False, note=note, pending=None)

    # ---- entries
    def _signal(self, z, candles, atr):
        """The confirmation candle just closed: run every filter. Returns a signal, or None with the reason recorded."""
        g, c, side = self.g, candles[-1], z["side"]
        bull, entry = side == "CE", c["c"]
        d = 1 if bull else -1

        def no(stage, reason, score=None):
            z["note"] = reason
            self._reject(c, side, stage, reason, z["id"], score)

        if self.day_signals >= g("OB_MAX_TRADES_PER_DAY"):
            return no("limit", f"already {self.day_signals} order-block signals today")
        trend = htf_trend(candles, self.tf, int(g("OB_HTF_MINUTES")), int(g("OB_HTF_EMA")))
        htf_ok = trend == ("up" if bull else "down")
        if g("OB_REQUIRE_HTF") and not htf_ok:
            return no("HTF trend", f"{g('OB_HTF_MINUTES')}-min trend is {trend or 'unknown'}")
        if g("OB_REQUIRE_FVG") and not z["fvg"]:
            return no("FVG", f"no FVG overlap (best {z['fvg_overlap']:.0f}%, need {g('OB_FVG_MIN_OVERLAP_PCT'):g}%)")
        vol_ok = z["rvol"] is not None and z["rvol"] >= g("OB_VOLUME_MULT")
        if g("OB_REQUIRE_VOLUME") and not vol_ok:
            return no("volume", "displacement volume unavailable" if z["rvol"] is None
                      else f"displacement volume {z['rvol']:.2f}x, need {g('OB_VOLUME_MULT'):g}x")
        vwap = session_vwap(candles)
        if g("OB_REQUIRE_VWAP") and (entry - vwap) * d <= 0:
            return no("VWAP", f"close {entry:.1f} on the wrong side of VWAP {vwap:.1f}")
        away = (entry - (z["high"] if bull else z["low"])) * d
        if away > g("OB_MAX_DISTANCE_ATR") * atr:
            return no("distance", f"price {away / atr:.2f}x ATR from the zone, max {g('OB_MAX_DISTANCE_ATR'):g}x")
        window = candles[-int(g("OB_STRUCTURE_LOOKBACK")):]
        hi, lo = fvg.swings(window, int(g("OB_SWING_LOOKBACK")))
        sl, target, risk, why = plan_exits(z, entry, atr, hi, lo, g)
        if why:
            return no("risk", why)
        score, parts = score_setup(z, g, htf_ok, vol_ok)
        z["score"] = score
        if score < g("OB_MIN_SCORE"):
            return no("score", f"score {score} below {g('OB_MIN_SCORE'):g}", score)
        rr = abs(target - entry) / risk
        tags = [t for t, on in (("sweep", z["sweep"]), (z["kind"], True), ("FVG", z["fvg"])) if on]
        why = (f"{z['direction']} OB {z['low']:.1f}–{z['high']:.1f} ({'+'.join(tags)}, score {score}, "
               f"{g('OB_HTF_MINUTES')}-min {trend or 'unknown'}, 1:{rr:.1f}, ATR {atr:.1f})")
        self._finish(z, "traded", f"signal at {c.get('t') or 'bar %d' % self.bar}")
        self.day_signals += 1
        sig = dict(side=side, entry=entry, sl=sl, target=target, risk=risk, rr=round(rr, 2), why=why, strategy="ob",
                   ob_id=z["id"], score=score, score_parts=parts, ob=summary(z))
        self.signals = (self.signals + [dict(sig, at=str(c.get("t")))])[-10:]
        return sig

    def _step_zone(self, z, candles, atr, allow_entry):
        """One closed candle against one zone. Returns a signal or None."""
        g, c, prev = self.g, candles[-1], candles[-2]
        bull = z["side"] == "CE"
        if z["day"] is not None and _day(c) not in (None, z["day"]):
            return self._finish(z, "expired", "new session")
        if self.bar - z["formed_bar"] > g("OB_MAX_AGE"):
            return self._finish(z, "expired", f"older than {g('OB_MAX_AGE')} candles")
        if (c["c"] < z["low"]) if bull else (c["c"] > z["high"]):
            return self._finish(z, "invalidated", "closed through the zone")
        if (c["l"] < z["low"]) if bull else (c["h"] > z["high"]):
            return self._finish(z, "mitigated", "traded through the far side")
        if self.bar <= z["formed_bar"] + 2:            # the third candle of a gap can close just after the displacement
            self._attach_fvg(z, candles, len(candles) - 1)

        pending, z["pending"] = z["pending"], None
        if pending:                                    # the candle after a rejection: confirm it or start over
            ok = (c["c"] > c["o"] and c["c"] > pending["h"]) if bull else (c["c"] < c["o"] and c["c"] < pending["l"])
            if ok and allow_entry:
                return self._signal(z, candles, atr)

        inside = c["l"] <= z["high"] and c["h"] >= z["low"]
        if inside:
            z["touched"] = True
            z["state"] = "in zone"
            rng = c["h"] - c["l"]
            if bull:
                wick = (min(c["o"], c["c"]) - c["l"]) / rng if rng > 0 else 0
                rejects = c["c"] > c["o"] and c["c"] >= (z["low"] + z["high"]) / 2 and (wick >= g("OB_REJECTION_WICK_PCT") or c["c"] > prev["h"])
            else:
                wick = (c["h"] - max(c["o"], c["c"])) / rng if rng > 0 else 0
                rejects = c["c"] < c["o"] and c["c"] <= (z["low"] + z["high"]) / 2 and (wick >= g("OB_REJECTION_WICK_PCT") or c["c"] < prev["l"])
            if rejects:
                z["pending"] = dict(h=c["h"], l=c["l"], bar=self.bar)
                z["state"] = "rejection"
        return None

    # ---- the one call per closed candle
    def update(self, candles, allow_entry=True):
        """Process candles[-1]: step the live zones (maybe ONE signal, best zone first), then look for a new setup."""
        self.bar += 1
        g = self.g
        n = int(g("OB_SWING_LOOKBACK"))
        if len(candles) < max(20, 2 * n + 3):
            return None
        c = candles[-1]
        a = strategy.atr(candles[-100:], 14)
        if not a:
            return None
        if _day(c) != self.day:
            self.day, self.day_signals = _day(c), 0
        tradable = allow_entry and a >= c["c"] * g("MIN_ATR_PCT") / 100

        sig = None
        for z in sorted((z for z in self.zones if z["eligible"]), key=lambda z: -z["formed_bar"]):
            s = self._step_zone(z, candles, a, tradable and not sig)
            sig = sig or s

        window = candles[-int(g("OB_STRUCTURE_LOOKBACK")):]
        for side in ("CE", "PE"):
            sw = self.sweeps[side]
            if sw and (self.bar - sw["bar"] > g("OB_SWEEP_MAX_AGE") or sw["day"] != _day(c)):
                sw = self.sweeps[side] = None
            strong, size = is_displacement(c, a, g("OB_DISPLACEMENT_ATR"), g("OB_DISPLACEMENT_BODY_PCT"), side)
            if strong:
                level = structure_break(window, n, side)
                if (sw or not g("OB_REQUIRE_SWEEP")) and (level is not None or not g("OB_REQUIRE_MSS")):
                    kind = "MSS" if sw and level is not None else "BOS" if level is not None else "displacement"
                    self._new_zone(candles, side, kind, level, dict(sw) if sw else None, a, size)
                    self.sweeps[side] = None           # one zone per sweep
            found, weak = detect_sweep(window, n, g("OB_SWEEP_MIN_WICK_PCT"), side)
            if weak:
                self._reject(c, side, "sweep", weak)
            elif found:
                self.sweeps[side] = dict(found, bar=self.bar, day=_day(c))
        return sig

    def replay(self, candles):
        """Rebuild zones from history (bot warm-up). Zones that would already have triggered are spent,
        so a restart never re-enters an old setup."""
        for i in range(1, len(candles) + 1):
            self.update(candles[max(0, i - 400):i], allow_entry=True)
        self.signals, self.rejected, self.day_signals = [], [], 0

    def snapshot(self):
        """Everything the dashboard shows."""
        live = [z for z in self.zones if z["eligible"]]
        keep = ("side", "entry", "sl", "target", "rr", "score", "score_parts", "ob_id", "at", "why")
        return dict(active=[summary(z) for z in live],
                    bullish=sum(z["side"] == "CE" for z in live), bearish=sum(z["side"] == "PE" for z in live),
                    recent=[summary(z) for z in self.zones if not z["eligible"]][-5:],
                    sweeps={k: {x: v[x] for x in ("level", "extreme", "wick", "t")} for k, v in self.sweeps.items() if v},
                    rejected=self.rejected[-8:], signals=[{k: s.get(k) for k in keep} for s in self.signals[-5:]])


# ------------------------------------------------------------------ exits
def exit_cfg(cfg):
    """strategy.manage reads RR / BREAKEVEN_AT_R / TRAIL_*; map the order block settings onto them."""
    g = _getter(cfg)
    return dict(RR=g("OB_RR"), BREAKEVEN_AT_R=g("OB_BREAKEVEN_AT_R"), TRAIL_START_R=g("OB_TRAIL_START_R"),
                TRAIL_R=g("OB_TRAIL_R"), TIME_STOP_MIN=0,
                BE_LOCK_PCT=g("BE_LOCK_PCT"), TRAIL_STEP_POINTS=g("TRAIL_STEP_POINTS"))


def manage(pos, hi, lo, cfg, now=None, close=None):
    return strategy.manage(pos, hi, lo, exit_cfg(cfg), now=now, close=close)


def _partial_cfg(cfg):
    g = _getter(cfg)
    return dict(FVG_PARTIAL_AT_R=g("OB_PARTIAL_AT_R"), FVG_PARTIAL_PCT=g("OB_PARTIAL_PCT"))


def partial_due(pos, hi, lo, cfg):
    return fvg.partial_due(pos, hi, lo, _partial_cfg(cfg))


def partial_qty(qty, lot, cfg):
    return fvg.partial_qty(qty, lot, _partial_cfg(cfg))


def premium_levels(fill_price, sig, cfg):
    """Option-price stop and target, set from the fill and an assumed delta rather than copied from the index levels.
    Decay or a spread can then stop the option even while the index setup still stands. None when switched off."""
    g = _getter(cfg)
    if not g("OB_PREMIUM_EXITS"):
        return None
    delta = g("OB_OPTION_DELTA")
    return (max(0.05, round(fill_price - delta * sig["risk"], 2)),
            round(fill_price + delta * abs(sig["target"] - sig["entry"]), 2))
