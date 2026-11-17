"""
Liquidity Sweep + Market Structure Shift strategy. Shared by bot.py and the backtests; closed 5-min index candles,
15-min market structure from completed 15-min candles only.

Reuses (imports, never modifies): fvg.swings, fvg._entry_ok, orderblock.is_displacement, structure_break,
fvg_at, htf_candles. New here: equal highs/lows liquidity pools and 15-min swing structure.

  1 LIQUIDITY   confirmed swing lows (sell-side) / highs (buy-side) that no later candle has traded through.
                Swings within LSM_EQUAL_TOL_ATR x ATR of each other merge into one EQUAL lows/highs pool
                (the liquidity sits beyond the most extreme of them).
  2 SWEEP       a candle trades past a pool by LSM_SWEEP_MIN_PEN_ATR..LSM_SWEEP_MAX_PEN_ATR x ATR and CLOSES back on
                the other side with a rejection wick >= LSM_SWEEP_MIN_WICK_PCT of its range. (CE: sweep of lows.)
  3 MSS         within LSM_MSS_BARS, a displacement candle (body >= LSM_DISP_ATR x ATR and LSM_DISP_BODY_PCT of its
                range) closes through the last confirmed swing on the other side. A close back beyond the swept
                level before that turns the sweep into a breakdown: setup dropped.
  4 FVG         a three-candle gap in the trade direction with the displacement as its 3rd or middle candle,
                at least LSM_FVG_MIN_ATR x ATR tall. No FVG → no zone → no trade.
  5 ENTRY       a later closed candle retraces into the FVG (LSM_ENTRY_MODE: aggressive / balanced / conservative,
                the same rules as the FVG strategy). A close through the far side of the gap, or beyond the
                sweep extreme, kills the zone; it expires after LSM_ZONE_MAX_AGE candles. One entry per zone.
  6 15-MIN      swing structure of completed 15-min candles: higher high + higher low = bullish, lower high +
                lower low = bearish, else range. LSM_HTF_MODE "with" = must agree; "not_against" = not opposite.
EXITS  stop beyond the sweep extreme + LSM_SL_BUFFER_ATR x ATR (too wide → skipped), target LSM_RR x risk (>= 2),
       stop to cost at 1R, trailing from 1.5R (strategy.manage never lowers a stop).
       invalidation(): a close beyond the sweep extreme.
The score ranks the checklist; it is NOT a probability of winning.
"""
import fvg
import orderblock
import strategy

CFG_DEFAULTS = dict(
    LSM_ALLOW_LIVE=False,
    LSM_SWING_N=2, LSM_STRUCTURE_LOOKBACK=60, LSM_EQUAL_TOL_ATR=0.1, LSM_REQUIRE_EQUAL=False,
    LSM_SWEEP_MIN_PEN_ATR=0.05, LSM_SWEEP_MAX_PEN_ATR=1.5, LSM_SWEEP_MIN_WICK_PCT=0.4,
    LSM_MSS_BARS=6, LSM_DISP_ATR=1.0, LSM_DISP_BODY_PCT=0.55, LSM_FVG_MIN_ATR=0.1,
    LSM_ENTRY_MODE="balanced", LSM_REJECTION_WICK_PCT=0.3, LSM_MAX_CHASE_ATR=0.5, LSM_ZONE_MAX_AGE=24,
    LSM_REQUIRE_HTF=True, LSM_HTF_MODE="not_against", LSM_HTF_MINUTES=15, LSM_HTF_SWING_N=2,
    LSM_SL_BUFFER_ATR=0.1, LSM_MIN_RISK_ATR=0.3, LSM_MAX_RISK_ATR=3.0,
    LSM_RR=2.0, LSM_MIN_RR=2.0,
    LSM_BREAKEVEN_AT_R=1.0, LSM_BE_LOCK_PCT=0, LSM_TRAIL_STEP_POINTS=0, LSM_TRAIL_START_R=1.5, LSM_TRAIL_R=1.0,
    LSM_EXIT_ON_INVALIDATION=True, LSM_MAX_SIGNALS_PER_DAY=2,
    MIN_ATR_PCT=0.05,
)


def _getter(cfg):
    if isinstance(cfg, dict):
        return lambda k: cfg.get(k, CFG_DEFAULTS.get(k))
    return lambda k: getattr(cfg, k, CFG_DEFAULTS.get(k))


_day = fvg._day


# ------------------------------------------------------------------ liquidity
def liquidity_pools(candles, n, tol, side):
    """Unswept liquidity in `candles` (all closed). side "CE" = sell-side (below swing lows), "PE" = buy-side.
    Returns [dict(level, kind, count, idx)], most recent first. Equal swings (within tol points) merge."""
    hi, lo = fvg.swings(candles, n)
    bull = side == "CE"
    live = []
    for j, level in (lo if bull else hi):
        later = candles[j + 1:]
        if any(x["l"] < level for x in later) if bull else any(x["h"] > level for x in later):
            continue
        live.append((j, level))
    pools = []
    for j, level in sorted(live, key=lambda x: x[1], reverse=not bull):
        p = next((p for p in pools if abs(p["anchor"] - level) <= tol), None)
        if p:
            p["count"] += 1
            p["idx"] = max(p["idx"], j)
            p["level"] = min(p["level"], level) if bull else max(p["level"], level)
        else:
            pools.append(dict(level=level, anchor=level, count=1, idx=j))
    for p in pools:
        p["kind"] = ("equal lows" if bull else "equal highs") if p["count"] > 1 else ("swing low" if bull else "swing high")
        del p["anchor"]
    return sorted(pools, key=lambda p: -p["idx"])


def detect_sweep(candles, atr, side, g):
    """Did candles[-1] sweep a pool that existed before it? Returns (sweep or None, rejection reason or None)."""
    n = int(g("LSM_SWING_N"))
    if len(candles) < 2 * n + 3:
        return None, None
    c, past = candles[-1], candles[:-1]
    bull = side == "CE"
    pools = liquidity_pools(past, n, g("LSM_EQUAL_TOL_ATR") * atr, side)
    if g("LSM_REQUIRE_EQUAL"):
        pools = [p for p in pools if p["count"] > 1]
    hit = []
    for p in pools:
        pen = (p["level"] - c["l"]) if bull else (c["h"] - p["level"])
        back = c["c"] > p["level"] if bull else c["c"] < p["level"]
        if pen >= g("LSM_SWEEP_MIN_PEN_ATR") * atr and pen <= g("LSM_SWEEP_MAX_PEN_ATR") * atr and back:
            hit.append(p)
    if not hit:
        return None, None
    best = max(hit, key=lambda p: (p["count"], p["idx"]))
    rng = c["h"] - c["l"]
    wick = ((min(c["o"], c["c"]) - c["l"]) if bull else (c["h"] - max(c["o"], c["c"]))) / rng if rng > 0 else 0
    if wick < g("LSM_SWEEP_MIN_WICK_PCT"):
        return None, f"weak sweep of {best['kind']} {best['level']:.1f} (wick {wick:.0%})"
    return dict(side=side, level=best["level"], kind=best["kind"], count=best["count"],
                extreme=c["l"] if bull else c["h"], wick=round(wick, 2), t=str(c.get("t") or "")), None


def htf_structure(candles, tf, minutes, n):
    """'bullish' / 'bearish' / 'range' from the last two confirmed swings of completed higher-timeframe candles,
    or None without enough of them."""
    h = orderblock.htf_candles(candles, tf, minutes)
    hi, lo = fvg.swings(h, n)
    if len(hi) < 2 or len(lo) < 2:
        return None
    if hi[-1][1] > hi[-2][1] and lo[-1][1] > lo[-2][1]:
        return "bullish"
    if hi[-1][1] < hi[-2][1] and lo[-1][1] < lo[-2][1]:
        return "bearish"
    return "range"


def _fresh():
    return dict(stage="idle", sweep=None, bar=None, mss=None, zone=None, note="")


class Engine:
    """Liquidity sweep + MSS for one instrument. update() once per closed candle; never looks past candles[-1]."""

    def __init__(self, instrument, timeframe, cfg):
        self.instrument, self.tf, self.cfg, self.g = instrument, timeframe, cfg, _getter(cfg)
        self.bar, self.day, self.day_signals = 0, None, 0
        self.sides = dict(CE=_fresh(), PE=_fresh())
        self.pools, self.htf, self.signals, self.rejected, self.sweeps = dict(CE=[], PE=[]), None, [], [], []
        # fvg._entry_ok reads FVG_* names: map ours onto them
        self._entry_g = lambda k: self.g({"FVG_REJECTION_WICK_PCT": "LSM_REJECTION_WICK_PCT",
                                          "FVG_MAX_CHASE_ATR": "LSM_MAX_CHASE_ATR"}[k])

    def _reject(self, c, side, stage, reason):
        self.rejected = (self.rejected + [dict(at=str(c.get("t") or ""), side=side, stage=stage, reason=reason)])[-20:]

    def _drop(self, side, note, c=None, stage=None):
        self.sides[side] = dict(_fresh(), note=note)
        if c is not None and stage:
            self._reject(c, side, stage, note)

    def _gap(self, side, candles, a):
        """The FVG ending at candles[-1] in the trade direction, if tall enough: (lower, upper) or None."""
        gap = orderblock.fvg_at(candles, len(candles) - 1, side)
        return gap if gap and gap[1] - gap[0] >= self.g("LSM_FVG_MIN_ATR") * a else None

    def _open_zone(self, side, gap, a):
        lo_, hi_ = gap
        self.sides[side].update(stage="zone", note="waiting for a retrace into the FVG",
                                zone=dict(side=side, lower=lo_, upper=hi_, mid=(lo_ + hi_) / 2, last_touch_bar=None,
                                          formed_bar=self.bar, atr=a))

    def _step(self, side, candles, a, allow_entry):
        g, s, c, prev = self.g, self.sides[side], candles[-1], candles[-2]
        bull = side == "CE"
        d = 1 if bull else -1
        if s["stage"] == "idle":
            return None
        sw = s["sweep"]
        if (c["c"] - sw["extreme"]) * d < 0:
            return self._drop(side, f"closed beyond the sweep extreme {sw['extreme']:.1f}", c, "structure")

        if s["stage"] == "swept":
            if (c["c"] - sw["level"]) * d < 0:
                return self._drop(side, "closed back beyond the swept level: a breakdown, not a sweep", c, "sweep")
            if (c["l"] < sw["extreme"]) if bull else (c["h"] > sw["extreme"]):
                sw["extreme"] = c["l"] if bull else c["h"]     # deeper sweep that still closed back
            strong, size = orderblock.is_displacement(c, a, g("LSM_DISP_ATR"), g("LSM_DISP_BODY_PCT"), side)
            window = candles[-int(g("LSM_STRUCTURE_LOOKBACK")):]
            level = orderblock.structure_break(window, int(g("LSM_SWING_N")), side) if strong else None
            if level is not None:
                s.update(stage="mss", bar=self.bar, note="MSS, looking for the FVG",
                         mss=dict(level=level, disp_atr=round(size, 2), bar=self.bar, t=str(c.get("t") or ""),
                                  gap3=self._gap(side, candles, a)))   # displacement as the gap's 3rd candle
                return None
            if self.bar - s["bar"] >= g("LSM_MSS_BARS"):
                return self._drop(side, f"no MSS within {g('LSM_MSS_BARS')} candles of the sweep")
            return None

        if s["stage"] == "mss":            # prefer the textbook gap: displacement as the MIDDLE candle
            gap = self._gap(side, candles, a) or s["mss"]["gap3"]
            if not gap:
                return self._drop(side, "no FVG after the MSS", c, "FVG")
            self._open_zone(side, gap, a)
            return None                    # entries start with the candle after the one that completed the gap

        z = s["zone"]                                          # stage "zone"
        if self.bar == z["formed_bar"]:
            return None
        if (c["c"] < z["lower"]) if bull else (c["c"] > z["upper"]):
            return self._drop(side, "closed through the FVG", c, "FVG")
        if self.bar - z["formed_bar"] > g("LSM_ZONE_MAX_AGE"):
            return self._drop(side, f"FVG older than {g('LSM_ZONE_MAX_AGE')} candles")
        if c["l"] <= z["upper"] and c["h"] >= z["lower"]:
            z["last_touch_bar"] = self.bar
        if not fvg._entry_ok(z, c, prev, g("LSM_ENTRY_MODE"), a, self._entry_g):
            return None
        return self._signal(side, candles, a, allow_entry)

    def _signal(self, side, candles, a, allow_entry):
        g, s, c = self.g, self.sides[side], candles[-1]
        bull = side == "CE"
        d = 1 if bull else -1
        sw, z, mss = s["sweep"], s["zone"], s["mss"]
        if not allow_entry:
            return self._drop(side, "retrace came while entries were not allowed", c, "entry")
        if self.day_signals >= g("LSM_MAX_SIGNALS_PER_DAY"):
            return self._drop(side, f"already {self.day_signals} signals today", c, "limit")
        htf = self.htf
        htf_ok = htf == ("bullish" if bull else "bearish")
        against = htf == ("bearish" if bull else "bullish")
        if g("LSM_REQUIRE_HTF") and (not htf_ok if g("LSM_HTF_MODE") == "with" else (against or htf is None)):
            return self._drop(side, f"15-min structure is {htf or 'unknown'}", c, "HTF structure")
        price = c["c"]
        risk = (price - sw["extreme"]) * d + g("LSM_SL_BUFFER_ATR") * a
        risk = max(risk, g("LSM_MIN_RISK_ATR") * a)
        if risk > g("LSM_MAX_RISK_ATR") * a:
            return self._drop(side, f"stop {risk / a:.2f} ATR away, max {g('LSM_MAX_RISK_ATR'):g}", c, "risk")
        rr = max(g("LSM_RR"), g("LSM_MIN_RR"))
        score = 50 + (15 if sw["count"] > 1 else 0) + int(10 * min(1.0, mss["disp_atr"] / (2 * g("LSM_DISP_ATR")))) \
            + int(10 * min(1.0, (z["upper"] - z["lower"]) / a)) + (10 if htf_ok else 0) + (5 if sw["wick"] >= 0.6 else 0)
        score = int(min(100, score))
        why = (f"{'bullish' if bull else 'bearish'} sweep of {sw['kind']} {sw['level']:.1f} → MSS through "
               f"{mss['level']:.1f} → FVG {z['lower']:.1f}–{z['upper']:.1f} retrace, 15-min {htf or 'n/a'}, "
               f"1:{rr:g}, ATR {a:.1f}")
        ts = c.get("t")
        sig = dict(side=side, entry=price, sl=price - d * risk, target=price + d * rr * risk, risk=risk, rr=rr,
                   why=why, strategy="lsmss", score=score, confidence=score, volume_ok=None, htf_ok=htf_ok,
                   sweep_level=sw["level"], sweep_extreme=sw["extreme"], fvg_low=z["lower"], fvg_high=z["upper"],
                   ts=ts.isoformat() if hasattr(ts, "isoformat") else None)
        self.day_signals += 1
        self.sides[side] = dict(_fresh(), note=f"signal at {ts or 'bar %d' % self.bar}")
        self.signals = (self.signals + [dict(sig, at=str(ts or ""))])[-10:]
        return sig

    def update(self, candles, allow_entry=True):
        self.bar += 1
        g = self.g
        n = int(g("LSM_SWING_N"))
        if len(candles) < max(20, 2 * n + 3):
            return None
        c = candles[-1]
        a = strategy.atr(candles[-100:], 14)
        if not a:
            return None
        if _day(c) != self.day:
            self.day, self.day_signals = _day(c), 0
            self.sides = dict(CE=_fresh(), PE=_fresh())       # setups never carry over into a new session
        self.htf = htf_structure(candles, self.tf, int(g("LSM_HTF_MINUTES")), int(g("LSM_HTF_SWING_N")))
        tradable = allow_entry and a >= c["c"] * g("MIN_ATR_PCT") / 100
        sig = None
        for side in ("CE", "PE"):
            s = self._step(side, candles, a, tradable and not sig)
            sig = sig or s
        window = candles[-int(g("LSM_STRUCTURE_LOOKBACK")):]
        tol = g("LSM_EQUAL_TOL_ATR") * a
        for side in ("CE", "PE"):
            self.pools[side] = liquidity_pools(window[:-1], n, tol, side)[:4]
            found, weak = detect_sweep(window, a, side, g)
            if weak:
                self._reject(c, side, "sweep", weak)
            elif found and self.sides[side]["stage"] in ("idle", "swept"):   # a newer sweep replaces an unconfirmed one
                self.sides[side] = dict(_fresh(), stage="swept", sweep=found, bar=self.bar, note="swept, waiting for MSS")
                self.sweeps = (self.sweeps + [found])[-5:]
        return sig

    def replay(self, candles):
        for i in range(1, len(candles) + 1):
            self.update(candles[max(0, i - 400):i], allow_entry=True)
        self.signals, self.rejected = [], []

    def snapshot(self):
        last = self.signals[-1] if self.signals else None
        keep = ("side", "entry", "sl", "target", "rr", "confidence", "htf_ok", "at", "why")
        sides = {}
        for k, s in self.sides.items():
            z = s["zone"]
            sides[k] = dict(stage=s["stage"], note=s["note"],
                            sweep={x: s["sweep"][x] for x in ("level", "kind", "extreme", "t")} if s["sweep"] else None,
                            mss=s["mss"], zone=dict(lower=z["lower"], upper=z["upper"]) if z else None)
        r = lambda p: dict(level=round(p["level"], 2), kind=p["kind"], count=p["count"])
        return dict(buy_side=[r(p) for p in self.pools["PE"]], sell_side=[r(p) for p in self.pools["CE"]],
                    htf=self.htf, sides=sides, recent_sweeps=self.sweeps[-5:], signals_today=self.day_signals,
                    last_signal={k: last.get(k) for k in keep} if last else None, rejected=self.rejected[-8:])


# ------------------------------------------------------------------ exits
def exit_cfg(cfg):
    g = _getter(cfg)
    return dict(RR=max(g("LSM_RR"), g("LSM_MIN_RR")), BREAKEVEN_AT_R=g("LSM_BREAKEVEN_AT_R"),
                TRAIL_START_R=g("LSM_TRAIL_START_R"), TRAIL_R=g("LSM_TRAIL_R"), TIME_STOP_MIN=0,
                BE_LOCK_PCT=g("LSM_BE_LOCK_PCT"), TRAIL_STEP_POINTS=g("LSM_TRAIL_STEP_POINTS"))


def manage(pos, hi, lo, cfg, now=None, close=None):
    return strategy.manage(pos, hi, lo, exit_cfg(cfg), now=now, close=close)


def invalidation(pos, candles, cfg):
    """On a closed candle while holding: exit when price closes beyond the sweep extreme."""
    g = _getter(cfg)
    if not g("LSM_EXIT_ON_INVALIDATION") or not candles or pos.get("sweep_extreme") is None:
        return None
    c, x = candles[-1]["c"], pos["sweep_extreme"]
    if (pos["side"] == "CE" and c < x) or (pos["side"] == "PE" and c > x):
        return f"closed beyond the sweep extreme {x:.1f}"
    return None
