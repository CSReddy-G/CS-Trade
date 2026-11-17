"""
Backtest strategy v2 (same rules as the bot, from strategy.py) on real 5-min index data.

    python backtest.py                                  # settings from config / Settings page
    python backtest.py --days 30 --lots 2 --instrument BOTH

Data    : Nifty (^NSEI) / Sensex (^BSESN) 5-min candles, Brent (BZ=F) 5-min, India VIX — Yahoo (~60 days), merged
          into a local store (candle_store.py) that keeps older days, plus Zerodha history when the panel passes a
          session — up to 90 days. Brent (crude filter) stays Yahoo-only, so older days have no crude reading.
Options : ITM premiums are MODELLED with Black-Scholes (IV = India VIX) and weekly expiry
          (Nifty Tue, Sensex Thu) — free historical option prices don't exist. Treat as estimates.
Filters : the volume filter (NIFTYBEES 5-min volume) and VIX-scaled stops are tested, with a table
          comparing each switched off. VIX = the PREVIOUS day's close (no look-ahead).
Costs   : charges plus SLIPPAGE on both sides of every trade. Stops still fill exactly at their level, so results stay optimistic.
Agents  : the crude-oil filter IS tested (prints ON vs OFF). The news agent can't be backtested
          (no free archive of intraday headlines) — it only works live/paper.
Strategy: STRATEGIES from Settings, or --strategy ema, fvg, ob or a mix like ema,fvg (first signal wins, as in the bot).
          FVG alone (fvg.py) compares its entry modes and stop methods instead of the volume/VIX table; its volume
          filter uses the same NIFTYBEES 5-min proxy. Order Block research runs live in ob_backtest.py.
"""
import argparse, math
from datetime import datetime, timedelta, time as dtime, timezone

import pandas as pd
import yfinance as yf

import candle_store
import config
import fvg
import pairing
import strategies
import strategy

RATE = 0.065
SLIPPAGE = 0.004      # per side, on premium: the same figure strategy.cost_per_unit uses for a liquid ITM strike


# ---------------------------------------------------------------- option model
def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(spot, strike, t_years, iv, kind):
    t = max(t_years, 1 / (365 * 24 * 12))
    d1 = (math.log(spot / strike) + (RATE + iv * iv / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if kind == "CE":
        return spot * ncdf(d1) - strike * math.exp(-RATE * t) * ncdf(d2)
    return strike * math.exp(-RATE * t) * ncdf(-d2) - spot * ncdf(-d1)


def next_expiry(ts, weekday):
    d = ts.date()
    exp = datetime.combine(d + timedelta(days=(weekday - d.weekday()) % 7), dtime(15, 30), tzinfo=ts.tzinfo)
    return exp + timedelta(days=7) if exp <= ts else exp


def years_to(exp, ts):
    return (exp - ts).total_seconds() / (365 * 24 * 3600)


def charges(buy_val, sell_val):
    brokerage = 40
    stt = 0.0015 * sell_val           # options STT on sell premium, 0.15% from 1 Apr 2026
    txn = 0.0003553 * (buy_val + sell_val)  # NSE options transaction charge
    sebi = 10e-7 * (buy_val + sell_val)
    stamp = 0.00003 * buy_val
    gst = 0.18 * (brokerage + txn + sebi)
    return brokerage + stt + txn + sebi + stamp + gst


# ---------------------------------------------------------------- data
def _dl(sym, period, interval):
    df = yf.download(sym, period=period, interval=interval, progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna(subset=["Close"])


def load(names, days):
    """5-min index candles for the last `days` sessions (+10 of warm-up): fresh Yahoo merged into the local store
    (candle_store.py), which also keeps what older downloads and Zerodha history provided."""
    period = f"{min(days + 10, 59)}d"
    st = candle_store.load()
    for n in names:
        try:
            df = _dl(config.INSTRUMENT_SPECS[n]["yahoo"], period, "5m")
            df.index = df.index.tz_convert("Asia/Kolkata")
            candle_store.add(st, n, df.between_time("09:15", "15:25"))
        except Exception as e:
            print(f"Note: {n} Yahoo download failed ({str(e)[:80]}); using stored candles.")
    candle_store.fill_from_kite(st, names, days)
    idx = {}
    for n in names:
        df = st["idx"].get(n)
        if df is None or df.empty:
            raise SystemExit(f"No 5-minute data for {n}.")
        keep = candle_store.sessions(st, n)[-(days + 10):]
        idx[n] = df[df.index.date >= keep[0]]
        if len(keep) < days:
            print(f"Note: {n} — only {len(keep)} sessions of 5-min data available ({keep[0]} onwards), asked for "
                  f"{days}; the earliest days have less indicator warm-up. Log in to Zerodha to fetch older days.")
    try:
        vix = _dl("^INDIAVIX", "1y", "1d")["Close"]
        st["vix"].update({d.date(): float(v) / 100 for d, v in vix.items()})
    except Exception:
        pass
    vix_by_day = dict(st["vix"])
    try:
        candle_store.save(st)
    except OSError as e:
        print(f"Note: candle store not saved ({e}).")
    try:
        brent = _dl("BZ=F", period, "5m")["Close"]
        brent.index = brent.index.tz_convert("UTC")
    except Exception:
        brent = pd.Series(dtype=float)
    return idx, vix_by_day, brent


def load_volume(days):
    """Volume proxy (VOLUME_SYMBOL) 5-min bars as (epoch list, volume list), fresh Yahoo merged with the local
    store; empty if neither has any."""
    st = candle_store.load()
    try:
        df = _dl(config.VOLUME_SYMBOL, f"{min(days + 10, 59)}d", "5m")
        v = df["Volume"].astype(float)
        v.index = v.index.tz_convert("Asia/Kolkata")
        candle_store.add_volume(st, v)
        candle_store.save(st)
    except Exception:
        pass
    v = st["vol"]
    if v is None or not len(v):
        return [], []
    return [int(t.timestamp()) for t in v.index], [float(x or 0) for x in v.values]


def vix_before(day, vix_by_day):
    """India VIX (in points) at the previous close — what the bot could know at the open."""
    prior = [d for d in vix_by_day if d < day]
    return vix_by_day[max(prior)] * 100 if prior else None


def crude_change(brent, ts):
    """% change of Brent at time ts vs the previous UTC session's last price (same rule as the live agent)."""
    if brent.empty:
        return None
    t = ts.tz_convert("UTC")
    upto = brent[brent.index <= t]
    if upto.empty:
        return None
    day = upto.index[-1].date()
    prior = upto[[d.date() < day for d in upto.index]]
    if prior.empty:
        return None
    return (upto.iloc[-1] - prior.iloc[-1]) / prior.iloc[-1] * 100


def iv_for(day, vix_by_day):
    prior = [d for d in vix_by_day if d <= day]
    return vix_by_day[max(prior)] if prior else 0.13


# ---------------------------------------------------------------- simulation
def run(idx, vix, brent, names, days, lots, use_crude, volume=None, **overrides):
    cfg = {k: getattr(config, k) for k in dir(config) if k.isupper()}
    cfg.update(overrides)
    cfg["USE_CRUDE_AGENT"] = use_crude
    vol_t, vol_v = volume or ([], [])
    cfg["USE_NEWS_AGENT"] = False
    active = strategies.active(cfg)
    needs_time = any(s.id != "ema" for s in active)       # the engines read candle times and volume
    runs = {n: strategies.runners(active, n, config.CANDLE_MINUTES, cfg) for n in names}
    vol_at = dict(zip(vol_t, vol_v))
    specs = {n: config.INSTRUMENT_SPECS[n] for n in names}
    all_days = sorted(set().union(*[set(idx[n].index.date) for n in names]))
    test_days = set(all_days[-days:])
    tf = timedelta(minutes=config.CANDLE_MINUTES)
    scale = lots / max(config.LOTS, 1)
    max_loss = config.MAX_DAILY_LOSS * scale
    max_prem = config.MAX_PREMIUM_PER_TRADE * scale

    def at(ts, s):
        h, m = map(int, s.split(":")); return ts.replace(hour=h, minute=m, second=0)

    hist = {n: [] for n in names}
    pos, trades, day_state, vetoed = {}, [], {}, 0
    timeline = sorted(set().union(*[set(idx[n].index) for n in names]))
    rows = {n: idx[n] for n in names}

    def opt(p, ts, spot):
        return bs_price(spot, p["strike"], years_to(p["exp"], ts), p["iv"], p["side"])

    def premium_exit(p, ts, c):
        """An option-price stop or target hit inside candle c: (index level where the modelled premium reaches it, reason).
        The stop is checked first, as with index stops."""
        if not p.get("prem_sl"):
            return None
        up = p["side"] == "CE"
        worst, best = (c["l"], c["h"]) if up else (c["h"], c["l"])
        for level, hit, name in ((p["prem_sl"], opt(p, ts, worst) <= p["prem_sl"], "option stop"),
                                 (p["prem_target"], opt(p, ts, best) >= p["prem_target"], "option target")):
            if hit:
                lo_s, hi_s = worst, best
                if (opt(p, ts, lo_s) - level) * (opt(p, ts, hi_s) - level) > 0:
                    return c["o"], name                       # the whole candle is beyond it: out at the open
                for _ in range(25):
                    mid = (lo_s + hi_s) / 2
                    if (opt(p, ts, mid) - level) * (opt(p, ts, lo_s) - level) <= 0:
                        hi_s = mid
                    else:
                        lo_s = mid
                return (lo_s + hi_s) / 2, name
        return None

    def exit_trade(n, ts, level, reason):
        p = pos.pop(n)
        px = max(0.05, opt(p, ts, level))
        part, part_val = p.get("part_qty", 0), p.get("part_value", 0.0)   # units booked earlier (FVG partial)
        gross = (px - p["prem"]) * p["qty"] + part_val - p["prem"] * part
        gross -= SLIPPAGE * (p["prem"] * (p["qty"] + part) + px * p["qty"] + part_val)
        ch = charges(p["prem"] * (p["qty"] + part), px * p["qty"] + part_val) + (23.6 if part else 0)  # +1 order
        net = gross - ch
        st = day_state[ts.date()]
        st["pnl"] += net; st["trades"] += 1
        st["consec"] = st["consec"] + 1 if net < 0 else 0
        if (st["pnl"] <= -max_loss or st["consec"] >= config.MAX_CONSECUTIVE_LOSSES
                or st["trades"] >= config.MAX_TRADES_PER_DAY):
            st["locked"] = True
        trades.append(dict(date=ts.date(), index=n, side=p["side"], strike=p["strike"], qty=p["qty"] + part,
                           entry_time=p["t"].strftime("%H:%M"), entry=round(p["prem"], 2),
                           exit_time=ts.strftime("%H:%M"), exit=round(px, 2),
                           idx_entry=round(p["entry"], 1), idx_exit=round(level, 1),
                           gross=round(gross), charges=round(ch), net=round(net), reason=reason,
                           strategy=p.get("strategy", "ema"), why=p.get("why", ""), score=p.get("score"),
                           r=round((level - p["entry"]) * (1 if p["side"] == "CE" else -1) / p["risk"], 2)))

    gate = pairing.make(cfg.get("BOTH_SAME_SIGNAL"), names)

    def enter(n, entries, ts, day, close_ts, half):
        """Open a position on the first signal that clears the filters."""
        nonlocal vetoed
        if len(pos) - (1 if half else 0) >= config.MAX_OPEN_POSITIONS:
            return
        for sig in entries:                                # first signal wins, as in the bot
            shared = strategies.spec_of(sig).shared_filters
            if cfg.get("VOLUME_FILTER") and vol_t and shared:
                # 5-min proxy bars: the bar starting at ts is the signal candle (minutes=1 picks just that bar)
                rv, _ = strategy.relative_volume(vol_t, vol_v, int(ts.timestamp()), 1, cfg["VOLUME_AVG_N"])
                if rv is not None and rv < cfg["VOLUME_MIN_RVOL"]:
                    continue
            if cfg.get("VIX_ADJUST_EXITS") and shared:     # FVG and OB stops sit on structure, not on volatility
                sig = strategy.vix_scale(sig, vix_before(day, vix), cfg)
            ch = crude_change(brent, close_ts) if use_crude else None
            if strategy.agent_veto(sig["side"], {"change_pct": ch} if ch is not None else None, None, cfg):
                vetoed += 1; continue
            spec = specs[n]
            n_lots = pairing.half_lots(n, lots) if half else lots
            if not n_lots:
                continue
            p = dict(side=sig["side"], entry=sig["entry"], sl=sig["sl"], target=sig["target"], risk=sig["risk"],
                     be_done=False, strike=strategy.itm_strike(sig["entry"], sig["side"], spec["step"], config.ITM_STRIKES),
                     exp=next_expiry(close_ts, spec["expiry_weekday"]), iv=iv_for(day, vix), t=close_ts,
                     qty=spec["lot"] * n_lots, strategy=sig["strategy"], why=sig.get("why", ""), score=sig.get("score"),
                     orb_high=sig.get("orb_high"), orb_low=sig.get("orb_low"), sweep_extreme=sig.get("sweep_extreme"))
            p["prem"] = opt(p, close_ts, sig["entry"])
            if p["prem"] * p["qty"] > max_prem:
                continue
            p["cost_pts"] = strategy.cost_points(p["prem"], p["qty"])
            p["cost_unit"] = strategy.cost_per_unit(p["prem"], p["qty"])
            prem = strategies.spec_of(sig).premium_levels(p["prem"], sig, cfg)
            if prem:
                p["prem_sl"], p["prem_target"] = prem
            pos[n] = p
            break

    for ts in timeline:
        day = ts.date()
        for n in names:
            if ts not in rows[n].index:
                continue
            r = rows[n].loc[ts]
            c = dict(o=float(r["Open"]), h=float(r["High"]), l=float(r["Low"]), c=float(r["Close"]))
            if needs_time:
                c.update(t=ts, v=vol_at.get(int(ts.timestamp())))
            if day not in test_days:
                hist[n].append(c)
                hist[n] = hist[n][-400:]
                for r_ in runs[n]:
                    r_.warm(hist[n])
                continue
            st = day_state.setdefault(day, dict(pnl=0.0, trades=0, consec=0, locked=False))
            close_ts = ts + tf
            if n in pos:
                if ts >= at(ts, config.SQUARE_OFF_AT):
                    exit_trade(n, ts, c["o"], "square-off time")
                else:
                    p = pos[n]
                    owner = strategies.spec_of(p)
                    lvl, why = owner.manage(p, c["h"], c["l"], cfg, now=close_ts, close=c["c"])
                    prem_hit = premium_exit(p, close_ts, c) if lvl is None else None
                    if lvl is not None:
                        exit_trade(n, close_ts, lvl, why)
                    elif prem_hit:
                        exit_trade(n, close_ts, *prem_hit)
                    elif (plvl := owner.partial_due(p, c["h"], c["l"], cfg)) is not None:
                        q = owner.partial_qty(p["qty"], specs[n]["lot"], cfg)
                        if q:
                            p["part_qty"] = q
                            p["part_value"] = max(0.05, opt(p, close_ts, plvl)) * q
                            p["qty"] -= q
                    elif p["be_done"] and cfg.get("COST_STOP_ON_PREMIUM"):
                        worst = c["l"] if p["side"] == "CE" else c["h"]
                        if opt(p, close_ts, worst) <= p["prem"] + p["cost_unit"]:
                            exit_trade(n, close_ts, worst, "stop at cost")
            hist[n].append(c)
            hist[n] = hist[n][-400:]
            blocked = (n in pos or st["locked"] or len(pos) >= config.MAX_OPEN_POSITIONS
                       or not (at(ts, config.NO_ENTRY_BEFORE) <= close_ts <= at(ts, config.NO_ENTRY_AFTER)))
            # every strategy sees every candle; an engine may only spend a zone when the bot could act on it
            entries, opposite = strategies.evaluate(runs[n], hist[n], not blocked, pos.get(n), cfg)
            if n in pos:
                if opposite:
                    exit_trade(n, close_ts, c["c"], f"opposite {strategies.spec_of(opposite).short} signal")
                elif (broke := strategies.spec_of(pos[n]).invalidates(pos[n], hist[n], cfg)):
                    exit_trade(n, close_ts, c["c"], f"{strategies.spec_of(pos[n]).short} setup broke: {broke}")
                continue
            if blocked:
                continue
            if gate:
                if entries:
                    gate.offer(n, ts, entries, ts)
            else:
                enter(n, entries, ts, day, close_ts, False)
        if gate:                                               # both indices have closed this candle
            closed = {x: ts for x in names}
            for n in names:
                sigs, half, _ = gate.decide(n, closed, ts)
                if sigs and n not in pos:
                    enter(n, sigs, ts, ts.date(), ts + tf, half)
    for n in list(pos):
        last = rows[n].index[-1]
        exit_trade(n, last, float(rows[n]["Close"].iloc[-1]), "end of data")
    return pd.DataFrame(trades), sorted(test_days), vetoed


# ---------------------------------------------------------------- report
def summary(tr):
    if tr.empty:
        return dict(trades=0, win=0, net=0, gross=0, charges=0, avg_win=0, avg_loss=0, dd=0, rr=0)
    w, l = tr[tr.net > 0], tr[tr.net <= 0]
    eq = tr.net.cumsum()
    aw, al = (w.net.mean() if len(w) else 0), (l.net.mean() if len(l) else 0)
    return dict(trades=len(tr), win=len(w) / len(tr) * 100, net=tr.net.sum(), gross=tr.gross.sum(),
                charges=tr.charges.sum(), avg_win=aw, avg_loss=al, dd=(eq - eq.cummax()).min(),
                rr=(aw / abs(al)) if al else 0)


def report(tr, days, names, lots, vetoed, label):
    ids = [s.id for s in strategies.active(config)]
    if ids == ["fvg"]:
        title = f"FVG strategy backtest ({config.FVG_ENTRY_MODE} entry, {config.FVG_SL_METHOD} stop, 1:{config.FVG_RR:g})"
    elif ids == ["ema"]:
        title = f"Strategy v2 backtest (1:{config.RR:g})"
    else:
        title = " + ".join(s.name for s in strategies.active(config)) + " backtest (first signal wins)"
    print(f"\n=== {title} [{label}] — {', '.join(names)}, {len(days)} days "
          f"({days[0]} to {days[-1]}), {lots} lot(s), ITM {config.ITM_STRIKES} ===")
    if tr.empty:
        print("No trades — filters found no qualifying setups."); return
    s = summary(tr)
    daily = tr.groupby("date")["net"].sum()
    if tr["strategy"].nunique() > 1:
        for sid, g in tr.groupby("strategy"):
            print(f"  {strategies.BY_ID[sid].name:16}: {len(g)} trades, {(g.net > 0).mean():.0%} wins, net ₹{g.net.sum():,.0f}")
    print(f"Trades           : {s['trades']}  (wins {int((tr.net > 0).sum())}, losses {int((tr.net <= 0).sum())}, "
          f"win rate {s['win']:.0f}%)")
    print(f"Gross P&L        : ₹{s['gross']:,.0f}")
    print(f"Charges          : ₹{s['charges']:,.0f}")
    print(f"NET P&L          : ₹{s['net']:,.0f}")
    print(f"Avg win / loss   : ₹{s['avg_win']:,.0f} / ₹{s['avg_loss']:,.0f}   (realised reward:risk 1:{s['rr']:.2f})")
    print(f"Best / worst trade: ₹{tr.net.max():,.0f} / ₹{tr.net.min():,.0f}")
    print(f"Best / worst day : ₹{daily.max():,.0f} ({daily.idxmax()}) / ₹{daily.min():,.0f} ({daily.idxmin()})")
    print(f"Max drawdown     : ₹{s['dd']:,.0f}")
    print(f"Days traded      : {len(daily)}  (green {int((daily > 0).sum())}, red {int((daily <= 0).sum())})")
    if vetoed:
        print(f"Crude agent vetoed {vetoed} signal(s)")
    if len(names) > 1:
        print("\nBy index:\n" + tr.groupby("index")["net"].agg(["count", "sum"]).rename(
            columns={"count": "trades", "sum": "net ₹"}).round(0).to_string())
    print("\nExit reasons:\n" + tr.reason.value_counts().to_string())
    print("\nDay-wise net P&L:")
    for d, v in daily.items():
        print(f"  {d}  ₹{v:>9,.0f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--lots", type=int, default=config.LOTS)
    ap.add_argument("--instrument", default=config.TRADE_INSTRUMENTS, choices=["NIFTY", "SENSEX", "BOTH"])
    ap.add_argument("--no-compare", action="store_true")
    ap.add_argument("--strategy", default=",".join(config.STRATEGIES), help="ema, fvg, ob or a mix like ema,fvg")
    a = ap.parse_args()
    config.STRATEGIES = a.strategy.split(",")
    if not strategies.active(config) or len(strategies.active(config)) != len(config.STRATEGIES):
        ap.error("--strategy takes ema, fvg, ob or a comma-separated mix")
    ids = [s.id for s in strategies.active(config)]
    names = ["NIFTY", "SENSEX"] if a.instrument == "BOTH" else [a.instrument]
    idx, vix, brent = load(names, a.days)
    vol = load_volume(a.days)
    if brent.empty:
        print("Note: Brent data unavailable — crude filter can't be tested.")
    if config.VOLUME_FILTER and not vol[0]:
        print(f"Note: {config.VOLUME_SYMBOL} volume unavailable — volume filter not applied.")
    main_crude = bool(config.USE_CRUDE_AGENT) and not brent.empty
    tr, days, vetoed = run(idx, vix, brent, names, a.days, a.lots, main_crude, vol)
    report(tr, days, names, a.lots, vetoed, "crude agent ON" if main_crude else "crude agent OFF")
    tr.to_csv("backtest_trades.csv", index=False)
    if not a.no_compare and ids == ["fvg"]:
        rows = [("as set", {})] + [(f"entry {m}", dict(FVG_ENTRY_MODE=m)) for m in ("aggressive", "balanced", "conservative")] \
            + [(f"stop {m}", dict(FVG_SL_METHOD=m)) for m in ("fvg", "swing", "atr")] \
            + [(f"target 1:{r:g}", dict(FVG_RR=r)) for r in (1.0, 1.5, 2.0, 3.0)] \
            + [("trend required", dict(FVG_TREND_MODE="require")), ("BOS required", dict(FVG_REQUIRE_BOS=True)),
               ("sweep required", dict(FVG_REQUIRE_SWEEP=True)), ("volume off", dict(FVG_VOLUME_FILTER=False))]
        print("\n=== FVG variants (one setting changed at a time) ===")
        print(f"{'':18}{'Trades':>8}{'Win %':>8}{'Net P&L ₹':>12}{'Max DD ₹':>11}{'R:R 1:':>8}")
        for lbl, o in rows:
            s = summary(run(idx, vix, brent, names, a.days, a.lots, main_crude, vol, **o)[0])
            print(f"{lbl:18}{s['trades']:>8.0f}{s['win']:>8.0f}{s['net']:>12,.0f}{s['dd']:>11,.0f}{s['rr']:>8.2f}")
    elif not a.no_compare and "ema" in ids:
        cols = [("as set", {}), ("volume off", dict(VOLUME_FILTER=False)), ("VIX exits off", dict(VIX_ADJUST_EXITS=False)),
                ("both off", dict(VOLUME_FILTER=False, VIX_ADJUST_EXITS=False))]
        on = dict(VOLUME_FILTER=True, VIX_ADJUST_EXITS=True)
        res = [(lbl, summary(run(idx, vix, brent, names, a.days, a.lots, main_crude, vol, **{**on, **o})[0])) for lbl, o in cols]
        print("\n=== Volume filter & VIX-scaled exits ===")
        print(f"{'':18}" + "".join(f"{lbl:>15}" for lbl, _ in res))
        for k, lbl, f in [("trades", "Trades", "{:.0f}"), ("win", "Win rate %", "{:.0f}"), ("net", "Net P&L ₹", "{:,.0f}"),
                          ("dd", "Max drawdown ₹", "{:,.0f}"), ("rr", "Reward:risk 1:", "{:.2f}")]:
            print(f"{lbl:18}" + "".join(f"{f.format(s[k]):>15}" for _, s in res))
    if not a.no_compare and not brent.empty:
        alt, _, _ = run(idx, vix, brent, names, a.days, a.lots, not main_crude, vol)
        s1, s2 = summary(tr), summary(alt)
        lab1, lab2 = ("ON", "OFF") if main_crude else ("OFF", "ON")
        print(f"\n=== Crude agent {lab1} vs {lab2} ===")
        print(f"{'':18}{'agent ' + lab1:>14}{'agent ' + lab2:>14}")
        for k, lbl, f in [("trades", "Trades", "{:.0f}"), ("win", "Win rate %", "{:.0f}"), ("net", "Net P&L ₹", "{:,.0f}"),
                          ("dd", "Max drawdown ₹", "{:,.0f}"), ("rr", "Reward:risk 1:", "{:.2f}")]:
            print(f"{lbl:18}{f.format(s1[k]):>14}{f.format(s2[k]):>14}")
    print("\nAll trades saved to backtest_trades.csv")
    print("Reminder: option prices are modelled; news agent not included (live/paper only).")
