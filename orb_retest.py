"""
Opening Range Breakout + Retest strategy. Shared by bot.py (live/paper) and the backtests. Index candles only;
candle "t" is the candle's OPEN time in Asia/Kolkata.

OPENING RANGE  highest high / lowest low of the candles that open in [ORB_RANGE_START, ORB_RANGE_END)
               (09:15, 09:20, 09:25 on 5-min candles). It is final only once the last of them has CLOSED (09:30);
               nothing trades before that. A day with a missing range candle, or a range that is too wide or too
               narrow against ATR, is skipped.
STAGES per direction and day (CE shown, PE is the mirror image):
  waiting    → a candle closes above the range high + ORB_BREAK_BUFFER_ATR x ATR, having closed at or below it
               before, with a bullish body >= ORB_MIN_BODY_PCT of its range and volume >= ORB_VOLUME_MULT x the
               previous 20 candles                                                   → broken out
  broken out → within ORB_RETEST_BARS a candle dips to the range high (low <= high + ORB_RETEST_TOL_ATR x ATR)
               and closes above it                                                   → retesting
  retesting  → within ORB_CONFIRM_BARS a bullish candle closes above the retest candle's high → SIGNAL
               (a deeper retest candle that still holds becomes the new retest candle)
  any close back below the range high - ORB_FAIL_BUFFER_ATR x ATR = false breakout → failed (no CE today).
  With ORB_REQUIRE_RETEST off, the breakout candle itself is the signal.
FILTERS  15-min trend (completed 15-min candles), no entry after ORB_NO_ENTRY_AFTER, no entry more than
         ORB_MAX_CHASE_ATR x ATR beyond the range edge (late entry), stop width within ATR limits.
EXITS    stop below the retest swing low (- ORB_SL_BUFFER_ATR x ATR), widened to ORB_MIN_RISK_ATR before entry,
         skipped beyond ORB_MAX_RISK_ATR; target ORB_RR x risk; stop to cost at 1R; trailing from 1.5R
         (strategy.manage never lowers a stop). invalidation(): a close back inside the opening range.
"""
import fvg
import orderblock
import strategy
import volume_stats

CFG_DEFAULTS = dict(
    ORB_ALLOW_LIVE=False,
    ORB_RANGE_START="09:15", ORB_RANGE_END="09:30",
    ORB_VOLUME_MULT=1.5, ORB_VOLUME_AVG_N=20, ORB_REQUIRE_VOLUME=True,
    ORB_VOLUME_BASIS="rolling", ORB_VOLUME_SLOT_DAYS=5,
    ORB_REQUIRE_RETEST=True, ORB_REQUIRE_HTF=True, ORB_HTF_MINUTES=15, ORB_HTF_EMA=20,
    ORB_MIN_BODY_PCT=0.5, ORB_BREAK_BUFFER_ATR=0.05,
    ORB_MIN_RANGE_ATR=0.5, ORB_MAX_RANGE_ATR=4.0, ORB_MAX_RANGE_PCT=0.8,
    ORB_RETEST_BARS=6, ORB_RETEST_TOL_ATR=0.2, ORB_CONFIRM_BARS=3, ORB_FAIL_BUFFER_ATR=0.0,
    ORB_MAX_CHASE_ATR=1.5, ORB_NO_ENTRY_AFTER="12:00",
    ORB_SL_BUFFER_ATR=0.1, ORB_MIN_RISK_ATR=0.5, ORB_MAX_RISK_ATR=2.0,
    ORB_RR=2.0, ORB_MIN_RR=2.0,
    ORB_BREAKEVEN_AT_R=1.0, ORB_BE_LOCK_PCT=0, ORB_TRAIL_STEP_POINTS=0, ORB_TRAIL_START_R=1.5, ORB_TRAIL_R=1.0,
    ORB_EXIT_ON_STRUCTURE_FAIL=True, ORB_MAX_SIGNALS_PER_DAY=1,
    MIN_ATR_PCT=0.05,
)


def _getter(cfg):
    if isinstance(cfg, dict):
        return lambda k: cfg.get(k, CFG_DEFAULTS.get(k))
    return lambda k: getattr(cfg, k, CFG_DEFAULTS.get(k))


_day = fvg._day


def _mins(hhmm):
    h, m = map(int, str(hhmm).split(":"))
    return h * 60 + m


def _tmins(c):
    t = c.get("t")
    return t.hour * 60 + t.minute if hasattr(t, "hour") else None


def opening_range(candles, start, end, tf):
    """(high, low, complete) from today's candles that OPEN in [start, end). complete = every expected candle is
    there and the last one has closed (it is in `candles`, which holds closed candles only). None before any."""
    day = _day(candles[-1])
    s, e = _mins(start), _mins(end)
    rng = [c for c in candles if _day(c) == day and _tmins(c) is not None and s <= _tmins(c) < e]
    if not rng:
        return None
    expected = {s + k * tf for k in range((e - s) // tf)}
    complete = {_tmins(c) for c in rng} == expected
    return max(c["h"] for c in rng), min(c["l"] for c in rng), complete


def _fresh_side():
    return dict(state="waiting", note="", breakout=None, retest=None, bar=None)


class Engine:
    """ORB for one instrument. Call update() exactly once per closed candle with the closed candles so far."""

    def __init__(self, instrument, timeframe, cfg):
        self.instrument, self.tf, self.cfg, self.g = instrument, timeframe, cfg, _getter(cfg)
        self.bar, self.day = 0, None
        self._new_day(None)
        self.signals, self.rejected = [], []
        self.price = self.rvol = None

    def _new_day(self, day):
        self.day, self.day_signals = day, 0
        self.range = None                 # dict(high, low, atr, width_atr) once final
        self.range_state = "building"     # building / ready / skipped: reason
        self.sides = dict(CE=_fresh_side(), PE=_fresh_side())

    def _rvol(self, candles):
        g = self.g
        return volume_stats.relative(candles, len(candles) - 1, g("ORB_VOLUME_BASIS"), int(g("ORB_VOLUME_AVG_N")),
                                     int(g("ORB_VOLUME_SLOT_DAYS")))

    def _reject(self, c, side, stage, reason):
        self.rejected = (self.rejected + [dict(at=str(c.get("t") or ""), side=side, stage=stage, reason=reason)])[-20:]

    def _finalize_range(self, candles, a):
        g = self.g
        hi, lo, complete = opening_range(candles, g("ORB_RANGE_START"), g("ORB_RANGE_END"), self.tf)
        if not complete:
            self.range_state = "skipped: opening range incomplete (missing candles)"
            return
        width = hi - lo
        mid = (hi + lo) / 2
        if width > g("ORB_MAX_RANGE_ATR") * a or 100 * width / mid > g("ORB_MAX_RANGE_PCT"):
            self.range_state = f"skipped: range too wide ({width:.1f} pts, {width / a:.1f} ATR)"
        elif width < g("ORB_MIN_RANGE_ATR") * a:
            self.range_state = f"skipped: range too narrow ({width:.1f} pts, {width / a:.1f} ATR)"
        else:
            self.range_state = "ready"
        self.range = dict(high=hi, low=lo, atr=a, width=width, width_atr=round(width / a, 2))

    # ---- one direction, one closed candle
    def _step(self, side, candles, a, allow_entry):
        g, s, c, prev = self.g, self.sides[side], candles[-1], candles[-2]
        bull = side == "CE"
        d = 1 if bull else -1
        edge = self.range["high"] if bull else self.range["low"]
        if s["state"] in ("failed", "signal", "expired", "skipped"):
            return None
        beyond = (c["c"] - edge) * d                       # how far the close is past the range edge

        if s["state"] == "waiting":
            if beyond <= g("ORB_BREAK_BUFFER_ATR") * a or (prev["c"] - edge) * d > 0 or _day(prev) != _day(c):
                return None                                # need a fresh close through the edge
            rng = c["h"] - c["l"]
            body = (c["c"] - c["o"]) * d
            if rng <= 0 or body <= 0 or body / rng < g("ORB_MIN_BODY_PCT"):
                s["note"] = "weak breakout candle"
                return self._reject(c, side, "breakout", f"body {max(body, 0) / rng if rng else 0:.0%} of range, "
                                                         f"need {g('ORB_MIN_BODY_PCT'):.0%}")
            rv = self._rvol(candles)
            vol_ok = rv is not None and rv >= g("ORB_VOLUME_MULT")
            if not vol_ok and (g("ORB_REQUIRE_VOLUME") or rv is not None):
                s["note"] = "breakout without volume"
                return self._reject(c, side, "volume", "volume unavailable" if rv is None
                                    else f"breakout volume {rv:.2f}x, need {g('ORB_VOLUME_MULT'):g}x")
            s.update(state="broken out", bar=self.bar, note="waiting for a retest",
                     breakout=dict(t=str(c.get("t") or ""), close=c["c"], rvol=rv, vol_ok=vol_ok))
            if not g("ORB_REQUIRE_RETEST"):
                return self._signal(side, candles, a, allow_entry, swing=c["l"] if bull else c["h"])
            return None

        # broken out or retesting: a close back through the edge is a false breakout
        if beyond < -g("ORB_FAIL_BUFFER_ATR") * a:
            s.update(state="failed", note="false breakout: closed back inside the range")
            return self._reject(c, side, "structure", s["note"])

        if s["state"] == "broken out":
            touched = ((c["l"] - edge) if bull else (edge - c["h"])) <= g("ORB_RETEST_TOL_ATR") * a
            if touched and beyond >= 0:
                s.update(state="retesting", bar=self.bar, note="retest held, waiting for confirmation",
                         retest=dict(t=str(c.get("t") or ""), h=c["h"], l=c["l"]))
            elif self.bar - s["bar"] >= g("ORB_RETEST_BARS"):
                s.update(state="expired", note=f"no retest within {g('ORB_RETEST_BARS')} candles")
            return None

        # retesting
        rt = s["retest"]
        if (c["c"] - (rt["h"] if bull else rt["l"])) * d > 0 and (c["c"] - c["o"]) * d > 0:
            swing = min(rt["l"], c["l"]) if bull else max(rt["h"], c["h"])
            return self._signal(side, candles, a, allow_entry, swing)
        if ((c["l"] < rt["l"]) if bull else (c["h"] > rt["h"])):
            s.update(bar=self.bar, retest=dict(t=str(c.get("t") or ""), h=c["h"], l=c["l"]))   # deeper retest, still holding
        elif self.bar - s["bar"] >= g("ORB_CONFIRM_BARS"):
            s.update(state="expired", note=f"no confirmation within {g('ORB_CONFIRM_BARS')} candles")
        return None

    def _signal(self, side, candles, a, allow_entry, swing):
        g, s, c = self.g, self.sides[side], candles[-1]
        bull = side == "CE"
        d = 1 if bull else -1
        edge = self.range["high"] if bull else self.range["low"]

        def no(stage, reason):
            s.update(state="skipped", note=reason)
            self._reject(c, side, stage, reason)

        if not allow_entry:
            return no("entry", "confirmed while entries were not allowed")
        if self.day_signals >= g("ORB_MAX_SIGNALS_PER_DAY"):
            return no("limit", f"already {self.day_signals} ORB signal(s) today")
        tm = _tmins(c)
        if tm is not None and tm + self.tf > _mins(g("ORB_NO_ENTRY_AFTER")):
            return no("late", f"after {g('ORB_NO_ENTRY_AFTER')}")
        price = c["c"]
        if (price - edge) * d > g("ORB_MAX_CHASE_ATR") * a:
            return no("late", f"price {(price - edge) * d / a:.2f} ATR past the range, max {g('ORB_MAX_CHASE_ATR'):g}")
        trend = orderblock.htf_trend(candles, self.tf, int(g("ORB_HTF_MINUTES")), int(g("ORB_HTF_EMA")))
        htf_ok = trend == ("up" if bull else "down")
        if g("ORB_REQUIRE_HTF") and not htf_ok:
            return no("HTF trend", f"{g('ORB_HTF_MINUTES')}-min trend is {trend or 'unknown'}")
        risk = (price - swing) * d + g("ORB_SL_BUFFER_ATR") * a
        risk = max(risk, g("ORB_MIN_RISK_ATR") * a)
        if risk > g("ORB_MAX_RISK_ATR") * a:
            return no("risk", f"stop {risk / a:.2f} ATR away, max {g('ORB_MAX_RISK_ATR'):g}")
        rr = max(g("ORB_RR"), g("ORB_MIN_RR"))
        bo = s["breakout"] or {}
        score = self._confidence(side, bo, htf_ok, price, edge, a, risk)
        rv = f"{bo['rvol']:.1f}x" if bo.get("rvol") is not None else "n/a"
        why = (f"ORB {'breakout' if bull else 'breakdown'} {self.range['low']:.1f}–{self.range['high']:.1f}"
               f"{', retest held' if s['retest'] else ''}, breakout volume {rv}, "
               f"{g('ORB_HTF_MINUTES')}-min {trend or 'n/a'}, 1:{rr:g}, ATR {a:.1f}")
        ts = c.get("t")
        sig = dict(side=side, entry=price, sl=price - d * risk, target=price + d * rr * risk, risk=risk, rr=rr,
                   why=why, strategy="orb", score=score, confidence=score, volume_ok=bool(bo.get("vol_ok")),
                   htf_ok=htf_ok, rvol=bo.get("rvol"), orb_high=self.range["high"], orb_low=self.range["low"],
                   ts=ts.isoformat() if hasattr(ts, "isoformat") else None)
        s.update(state="signal", note=f"signal at {ts or 'bar %d' % self.bar}")
        self.day_signals += 1
        self.signals = (self.signals + [dict(sig, at=str(ts or ""))])[-10:]
        return sig

    def _confidence(self, side, bo, htf_ok, price, edge, a, risk):
        """0-100 checklist quality (not a chance of winning): 50 for passing, the rest for how clearly."""
        g = self.g
        pts = 50.0
        if bo.get("rvol") is not None:
            pts += 20 * min(1.0, max(0.0, (bo["rvol"] - g("ORB_VOLUME_MULT")) / g("ORB_VOLUME_MULT") + 0.3))
        pts += 10 if self.sides[side]["retest"] else 0
        pts += 10 if htf_ok else 0
        pts += 10 * max(0.0, 1 - abs(price - edge) / (g("ORB_MAX_CHASE_ATR") * a))
        return int(max(0, min(100, round(pts))))

    # ---- the one call per closed candle
    def update(self, candles, allow_entry=True):
        self.bar += 1
        g = self.g
        if len(candles) < 3:
            return None
        c = candles[-1]
        if _day(c) != self.day:
            self._new_day(_day(c))
        self.price = c["c"]
        self.rvol = self._rvol(candles)
        tm = _tmins(c)
        if tm is None:
            return None
        if self.range_state == "building":
            if tm + self.tf < _mins(g("ORB_RANGE_END")):
                return None                                    # the range is still forming: nothing trades
            a = strategy.atr(candles[-100:], 14)
            if not a:
                self.range_state = "skipped: not enough history for ATR"
                return None
            self._finalize_range(candles, a)
            return None                                        # the last range candle can't also be a breakout
        if self.range_state != "ready":
            return None
        a = strategy.atr(candles[-100:], 14)
        if not a:
            return None
        tradable = allow_entry and a >= c["c"] * g("MIN_ATR_PCT") / 100
        sig = None
        for side in ("CE", "PE"):
            s = self._step(side, candles, a, tradable and not sig)
            sig = sig or s
        return sig

    def replay(self, candles):
        """Rebuild today's state from history (bot warm-up): setups already triggered are spent."""
        for i in range(1, len(candles) + 1):
            self.update(candles[max(0, i - 400):i], allow_entry=True)
        self.signals, self.rejected = [], []

    def snapshot(self):
        r = self.range or {}
        last = self.signals[-1] if self.signals else None
        keep = ("side", "entry", "sl", "target", "rr", "confidence", "volume_ok", "htf_ok", "at", "why")
        live = [k for k, v in self.sides.items() if v["state"] in ("broken out", "retesting", "signal")]
        side = live[0] if live else None
        st = self.sides[side] if side else None
        return dict(range_state=self.range_state, high=r.get("high"), low=r.get("low"), width_atr=r.get("width_atr"),
                    price=self.price, rvol=None if self.rvol is None else round(self.rvol, 2),
                    breakout=("up" if side == "CE" else "down") if side else None,
                    breakout_confirmed=bool(st and st["breakout"]),
                    retest=st["state"] if st else None,
                    sides={k: dict(state=v["state"], note=v["note"]) for k, v in self.sides.items()},
                    signals_today=self.day_signals,
                    last_signal={k: last.get(k) for k in keep} if last else None, rejected=self.rejected[-8:])


# ------------------------------------------------------------------ exits
def exit_cfg(cfg):
    g = _getter(cfg)
    return dict(RR=max(g("ORB_RR"), g("ORB_MIN_RR")), BREAKEVEN_AT_R=g("ORB_BREAKEVEN_AT_R"),
                TRAIL_START_R=g("ORB_TRAIL_START_R"), TRAIL_R=g("ORB_TRAIL_R"), TIME_STOP_MIN=0,
                BE_LOCK_PCT=g("ORB_BE_LOCK_PCT"), TRAIL_STEP_POINTS=g("ORB_TRAIL_STEP_POINTS"))


def manage(pos, hi, lo, cfg, now=None, close=None):
    return strategy.manage(pos, hi, lo, exit_cfg(cfg), now=now, close=close)


def invalidation(pos, candles, cfg):
    """On a closed candle while holding: exit if the breakout structure failed (close back inside the range)."""
    g = _getter(cfg)
    if not g("ORB_EXIT_ON_STRUCTURE_FAIL") or not candles:
        return None
    c = candles[-1]["c"]
    if pos["side"] == "CE" and pos.get("orb_high") is not None and c < pos["orb_high"]:
        return f"breakout failed: closed back below the range high {pos['orb_high']:.1f}"
    if pos["side"] == "PE" and pos.get("orb_low") is not None and c > pos["orb_low"]:
        return f"breakdown failed: closed back above the range low {pos['orb_low']:.1f}"
    return None
