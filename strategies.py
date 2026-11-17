"""
The main bot's entry strategies, and how several run side by side. The bot, backtest, index chart and Settings page
all read this list, so the first signal wins everywhere: whichever selected strategy fires first takes the position.
If two fire on the same candle, the one listed first here goes first.

Strategies 4-7 (VWAP, ORB, Liquidity Sweep + MSS, Supertrend + ADX) are paper-only until their Allow-live switch
is on, and are off unless picked in Settings. Their Spec.invalidates() exits when a setup breaks.

To add a strategy: write its rules in a module with an Engine (update / replay / snapshot, see orderblock.Engine),
add a class below and list it in ALL, add its defaults to config.py, and add its fields to app.SCHEMA with a section
matching `section`. It then gets a checkbox and an expandable panel under Settings → Strategies.
"""
import fvg
import liquidity_mss
import orb_retest
import orderblock
import strategy
import supertrend_adx
import trend_continuation
import vwap_volume


class EmaRunner:
    """signal_v2 keeps no state between candles."""

    def __init__(self, spec, cfg):
        self.spec, self.cfg = spec, cfg

    def update(self, candles, allow_entry=True):
        return strategy.signal_v2(candles, self.cfg) if allow_entry else None

    def warm(self, candles):
        pass

    def replay(self, candles):
        pass

    def snapshot(self):
        return None


class EngineRunner:
    """A strategy with an Engine: it must see every closed candle, even when it may not enter."""

    def __init__(self, spec, engine):
        self.spec, self.engine = spec, engine

    def update(self, candles, allow_entry=True):
        return self.engine.update(candles, allow_entry=allow_entry)

    def warm(self, candles):
        """One candle of history before the tested period (backtest)."""
        self.engine.update(candles)

    def replay(self, candles):
        self.engine.replay(candles)

    def snapshot(self):
        return self.engine.snapshot()


class Spec:
    id = name = short = tag = blurb = section = signal_name = rr_key = None
    live_flag = None                # setting that must be on before this strategy may trade real money
    shared_filters = False          # True: the main VOLUME_FILTER and VIX-scaled stops apply to its signals
    module = None                   # where its exits live

    @classmethod
    def runner(cls, instrument, tf, cfg):
        raise NotImplementedError

    @classmethod
    def rr(cls, cfg):
        return _get(cfg, cls.rr_key)

    @classmethod
    def needs_rvol(cls, cfg):
        """Must the bot attach relative volume to each closed candle before this strategy sees it?"""
        return False

    @classmethod
    def exit_on_opposite(cls, cfg):
        return False

    @classmethod
    def manage(cls, pos, hi, lo, cfg, now=None, close=None):
        return cls.module.manage(pos, hi, lo, cfg, now=now, close=close)

    @classmethod
    def partial_due(cls, pos, hi, lo, cfg):
        return None

    @classmethod
    def partial_qty(cls, qty, lot, cfg):
        return 0

    @classmethod
    def premium_levels(cls, fill_price, sig, cfg):
        return None

    @classmethod
    def invalidates(cls, pos, candles, cfg):
        """On a closed candle while this strategy holds a position: a reason to exit because its setup broke, else None."""
        return None


class EmaCross(Spec):
    id, name, short, tag = "ema", "EMA cross", "EMA", "Tested"
    blurb = "Fast average crosses the slow one, with the trend, on momentum. The bot's original strategy (v2)."
    section, signal_name, rr_key = "entry", "an EMA cross with the trend", "RR"
    shared_filters, module = True, strategy

    @classmethod
    def runner(cls, instrument, tf, cfg):
        return EmaRunner(cls, cfg)


class FairValueGap(Spec):
    id, name, short, tag = "fvg", "Fair Value Gap", "FVG", "Paper first"
    blurb = "Waits for price to come back into a three-candle gap and reject it. Has its own stops and targets."
    section, signal_name, rr_key = "fvg", "a Fair Value Gap retest", "FVG_RR"
    live_flag, module = "FVG_ALLOW_LIVE", fvg

    @classmethod
    def runner(cls, instrument, tf, cfg):
        return EngineRunner(cls, fvg.Engine(instrument, tf, cfg))

    @classmethod
    def needs_rvol(cls, cfg):
        return bool(_get(cfg, "FVG_VOLUME_FILTER"))

    @classmethod
    def exit_on_opposite(cls, cfg):
        return bool(_get(cfg, "FVG_EXIT_ON_OPPOSITE"))

    @classmethod
    def partial_due(cls, pos, hi, lo, cfg):
        return fvg.partial_due(pos, hi, lo, cfg)

    @classmethod
    def partial_qty(cls, qty, lot, cfg):
        return fvg.partial_qty(qty, lot, cfg)


class OrderBlock(Spec):
    id, name, short, tag = "ob", "Order Block", "OB", "Paper first"
    blurb = "Liquidity sweep, then a market structure shift, then a retest of the order block that rejects and confirms."
    section, signal_name, rr_key = "ob", "an order block retest", "OB_RR"
    live_flag, module = "OB_ALLOW_LIVE", orderblock

    @classmethod
    def runner(cls, instrument, tf, cfg):
        return EngineRunner(cls, orderblock.Engine(instrument, tf, cfg))

    @classmethod
    def needs_rvol(cls, cfg):
        return True                 # displacement volume always counts towards the score

    @classmethod
    def partial_due(cls, pos, hi, lo, cfg):
        return orderblock.partial_due(pos, hi, lo, cfg)

    @classmethod
    def partial_qty(cls, qty, lot, cfg):
        return orderblock.partial_qty(qty, lot, cfg)

    @classmethod
    def premium_levels(cls, fill_price, sig, cfg):
        return orderblock.premium_levels(fill_price, sig, cfg)


class _NewEngineSpec(Spec):
    """Strategies 4-7: an Engine, their own exits (module.manage) and a setup-invalidation exit."""
    tag = "Paper first"

    @classmethod
    def runner(cls, instrument, tf, cfg):
        return EngineRunner(cls, cls.module.Engine(instrument, tf, cfg))

    @classmethod
    def invalidates(cls, pos, candles, cfg):
        return cls.module.invalidation(pos, candles, cfg)


class VwapVolume(_NewEngineSpec):
    id, name, short = "vwap", "VWAP + Volume", "VWAP"
    blurb = "Trend continuation: a pullback to VWAP or EMA 20, then a strong candle on 1.5x volume, with the 15-min trend."
    section, signal_name, rr_key = "vwap", "a VWAP pullback with volume", "VWAP_RR"
    live_flag, module = "VWAP_ALLOW_LIVE", vwap_volume

    @classmethod
    def needs_rvol(cls, cfg):
        return True                 # the volume rule is part of the entry


class OrbRetest(_NewEngineSpec):
    id, name, short = "orb", "Opening Range Breakout + Retest", "ORB"
    blurb = "Breaks the 09:15-09:30 range on volume, retests the edge, holds, then confirms. One trade a day at most."
    section, signal_name, rr_key = "orb", "an opening range breakout retest", "ORB_RR"
    live_flag, module = "ORB_ALLOW_LIVE", orb_retest

    @classmethod
    def needs_rvol(cls, cfg):
        return True


class LiquidityMss(_NewEngineSpec):
    id, name, short = "lsmss", "Liquidity Sweep + MSS", "LS-MSS"
    blurb = "Sweep of (equal) highs/lows, a structure shift on a displacement candle, then a retrace into its FVG."
    section, signal_name, rr_key = "lsmss", "a liquidity sweep and structure shift", "LSM_RR"
    live_flag, module = "LSM_ALLOW_LIVE", liquidity_mss


class SupertrendAdx(_NewEngineSpec):
    id, name, short = "supertrend", "Supertrend + ADX", "ST-ADX"
    blurb = "Supertrend(10, 2) direction with a rising ADX >= 20 and +DI/-DI agreeing, entered on a pullback and a candle that breaks the last one."
    section, signal_name, rr_key = "supertrend", "a Supertrend pullback in a strong trend", "ST_RR"
    live_flag, module = "ST_ALLOW_LIVE", supertrend_adx


class TrendContinuation(_NewEngineSpec):
    id, name, short = "tc", "Trend Continuation", "TREND"
    blurb = "After the first hour, the first close beyond the 09:15-09:45 range on the VWAP side. Wide ATR stop, one trade a day."
    section, signal_name, rr_key = "tc", "an opening-range trend continuation", "TC_RR"
    live_flag, module = "TC_ALLOW_LIVE", trend_continuation


ALL = [EmaCross, FairValueGap, OrderBlock, VwapVolume, OrbRetest, LiquidityMss, SupertrendAdx,
       TrendContinuation]   # order breaks same-candle ties
BY_ID = {s.id: s for s in ALL}

# "Best mix" preset (Settings → Strategies): EMA cross + Supertrend + ADX + Trend Continuation with the exits re-tuned in
# research/combo_90d.py (chosen on 17 Aug – 30 Sep, checked unchanged on 1 Oct – 13 Nov 2026; reports/strategy_review/
# combo_90d/RESULTS.md). The main change everywhere is locking profit earlier (+0.8R instead of +1 / +1.2R). Every value
# the result depends on is pinned here, so the preset gives the tested setup whatever else was saved before.
# FVG is left out: no FVG setting made money on the choosing half.
BEST_MIX = dict(
    STRATEGIES=["ema", "supertrend", "tc"],
    # EMA cross
    EMA_FAST=9, EMA_SLOW=21, EMA_TREND=50, RSI_BUY_CE=53.0, RSI_BUY_PE=47.0, RSI_MAX=80.0, ADX_MIN=0.0,
    HTF_FILTER=True, VOLUME_FILTER=True, VOLUME_MIN_RVOL=1.2,
    RR=2.0, BREAKEVEN_AT_R=0.8, BE_LOCK_PCT=25.0, TRAIL_STEP_POINTS=3.0, TRAIL_START_R=0.0,
    # Supertrend + ADX (v3 entry) with the earlier profit lock
    ST_MULT=2.0, ST_ADX_MIN=20.0, ST_ADX_RISING_BARS=3, ST_CONFIRM="break", ST_RR=2.0,
    ST_BREAKEVEN_AT_R=0.8, ST_BE_LOCK_PCT=25.0, ST_TRAIL_START_R=0.0, ST_EXIT_ON_FLIP=False,
    # Trend Continuation: a firmer break, a wider stop and a 1:2.5 target
    TC_ENTRY_FROM="10:15", TC_ENTRY_UNTIL="12:00", TC_MIN_BREAK_ATR=0.5, TC_SL_ATR=2.5, TC_RR=2.5,
    TC_BREAKEVEN_AT_R=0.8, TC_BE_LOCK_PCT=25.0, TC_TRAIL_START_R=0.0, TC_EXIT_ON_FAIL=False,
)


def _get(cfg, key, default=None):
    return cfg.get(key, default) if isinstance(cfg, dict) else getattr(cfg, key, default)


def active(cfg):
    """The selected strategies as Spec classes, always in ALL's order. Unknown ids are ignored."""
    chosen = _get(cfg, "STRATEGIES") or []
    return [s for s in ALL if s.id in chosen]


def spec_of(pos_or_sig):
    """The strategy that owns a position or signal. Positions from before strategies were tagged are EMA's."""
    return BY_ID.get(pos_or_sig.get("strategy") or "ema", EmaCross)


def blocked_live(specs, cfg):
    """Names of the selected strategies that have not been allowed to trade real money."""
    return [s.name for s in specs if s.live_flag and not _get(cfg, s.live_flag)]


def runners(specs, instrument, tf, cfg):
    return [s.runner(instrument, tf, cfg) for s in specs]


def evaluate(runs, candles, can_enter, held, cfg, clean=True):
    """Show a newly closed candle to every strategy, each exactly once.
    Returns (entry candidates in tie-break order, an opposite signal from the strategy that owns `held`, or None).
    Only the owner of an open position may close it on an opposite signal: another strategy's view leaves it alone."""
    entries, exit_sig = [], None
    owner = spec_of(held) if held else None
    for r in runs:
        watching = r.spec is owner and r.spec.exit_on_opposite(cfg)
        sig = r.update(candles, allow_entry=clean and (can_enter or watching))
        if not sig:
            continue
        sig = dict(sig, strategy=r.spec.id)
        if held:
            if watching and sig["side"] != held["side"]:
                exit_sig = sig
        else:
            entries.append(sig)
    return entries, exit_sig
