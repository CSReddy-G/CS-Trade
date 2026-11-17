"""
Order Block research runs: each index on its own, over every window Yahoo's 5-min data allows, with the variants that
matter. Writes reports/order_block/RESULTS.md and a trades CSV. Same engine and costs as backtest.py.

    python ob_backtest.py                     # all windows and variants

Not real option prices: premiums are modelled (backtest.py explains why). Bid/ask spreads have no history either, so
a higher slippage run stands in for them. Yahoo keeps about 60 days of 5-min candles, so 90-day runs can't be done here.
"""
import argparse
import os

import pandas as pd

import backtest
import config
import orderblock

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports", "order_block")
BASE = dict(STRATEGIES=["ob"], USE_CRUDE_AGENT=False, MAX_TRADES_PER_DAY=config.MAX_TRADES_PER_DAY)
VARIANTS = [
    ("default (open-to-wick, OB only)", {}),
    ("zone: full candle", dict(OB_ZONE_MODE="FULL_CANDLE")),
    ("zone: body only", dict(OB_ZONE_MODE="BODY")),
    ("strict OB + FVG", dict(OB_REQUIRE_FVG=True)),
    ("strict OB + FVG, full candle", dict(OB_REQUIRE_FVG=True, OB_ZONE_MODE="FULL_CANDLE")),
    ("target 1:3", dict(OB_RR=3.0)),
    ("stop at zone only", dict(OB_SL_METHOD="ob")),
    ("15-min filter off, score 60", dict(OB_REQUIRE_HTF=False, OB_MIN_SCORE=60)),
    ("VWAP required", dict(OB_REQUIRE_VWAP=True)),
    ("volume required", dict(OB_REQUIRE_VOLUME=True)),
    ("option-price exits off", dict(OB_PREMIUM_EXITS=False)),
    ("sweep not required (off-spec, for sample size)", dict(OB_REQUIRE_SWEEP=False, OB_REQUIRE_HTF=False, OB_MIN_SCORE=0)),
]


def metrics(tr):
    if tr.empty:
        return dict(trades=0, win=0.0, net=0.0, pf=None, exp=0.0, dd=0.0, avg_r=0.0, streak=0)
    won, lost = tr.net[tr.net > 0].sum(), -tr.net[tr.net <= 0].sum()
    eq = tr.net.cumsum()
    streak = best = 0
    for v in tr.net:
        streak = streak + 1 if v <= 0 else 0
        best = max(best, streak)
    return dict(trades=len(tr), win=100 * (tr.net > 0).mean(), net=tr.net.sum(), pf=won / lost if lost else None,
                exp=tr.net.mean(), dd=(eq - eq.cummax().clip(lower=0)).min(), avg_r=tr.r.mean(), streak=best)


HEAD = "| Run | Trades | Win % | Net ₹ | Profit factor | ₹ / trade | Max drawdown ₹ | Avg R | Losses in a row |\n|---|--:|--:|--:|--:|--:|--:|--:|--:|\n"


def row(label, m):
    pf = "n/a" if m["pf"] is None else f"{m['pf']:.2f}"
    return (f"| {label} | {m['trades']} | {m['win']:.0f} | {m['net']:,.0f} | {pf} | {m['exp']:,.0f} | {m['dd']:,.0f} | "
            f"{m['avg_r']:.2f} | {m['streak']} |\n")


def all_for_verdict(frames_):
    return pd.concat(frames_) if frames_ else pd.DataFrame()


def funnel(df, cfg):
    """How many setups survive each stage, with no position or risk limits in the way."""
    candles = [dict(o=float(r.Open), h=float(r.High), l=float(r.Low), c=float(r.Close), t=t) for t, r in df.iterrows()]
    e = orderblock.Engine("", config.CANDLE_MINUTES, cfg)
    n = dict(sweeps=0, confirmed=0, signals=0)
    zones, sweeps = {}, set()
    real = e._signal

    def counted(*a):
        n["confirmed"] += 1
        return real(*a)
    e._signal = counted
    for i in range(1, len(candles) + 1):
        n["signals"] += bool(e.update(candles[max(0, i - 400):i]))
        sweeps.update(v["t"] for v in e.sweeps.values() if v)
        zones.update((z["id"], z) for z in e.zones)
    return dict(n, sweeps=len(sweeps), zones=len(zones), retested=sum(z["touched"] for z in zones.values()))


def regimes(idx, vix, day):
    """After-the-fact labels for reporting only; the strategy never sees them."""
    d = idx[idx.index.date == day]
    if d.empty:
        return "?", "?"
    rng = d["High"].max() - d["Low"].min()
    trend = "trending" if rng and abs(d["Close"].iloc[-1] - d["Open"].iloc[0]) / rng >= 0.5 else "sideways"
    v = vix.get(day)
    med = pd.Series(list(vix.values())).median()
    return trend, ("high VIX" if v is not None and v >= med else "low VIX")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lots", type=int, default=1)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    idx, vix, brent = backtest.load(["NIFTY", "SENSEX"], 59)
    vol = backtest.load_volume(59)
    frames = {n: df for n, df in idx.items()}
    avail = {n: len(set(df.index.date)) for n, df in frames.items()}
    span = min(avail.values())
    windows = [w for w in (30, 60, 90) if w <= span] or [span]
    if span not in windows and span < 60:
        windows.append(span)
    md = ["# Order Block strategy: backtest results\n\n"]
    md.append(f"Run on {pd.Timestamp.now(tz='Asia/Kolkata'):%d %b %Y}. Data: Yahoo 5-min candles, {min(df.index.min() for df in frames.values()):%d %b %Y} to "
              f"{max(df.index.max() for df in frames.values()):%d %b %Y}, {span} sessions. {a.lots} lot, index by index.\n\n")
    md.append("**Read this first.** Option premiums are Black-Scholes estimates (IV = India VIX), not historical option prices. "
              "Spreads are a flat slippage assumption, and stops fill exactly at their level, so results lean optimistic. "
              "The news agent can't be replayed, and the crude filter is off here. The score is a checklist, not a chance of winning.\n\n")
    run = lambda names, days, **o: backtest.run({n: frames[n] for n in names}, vix, brent, names, days, a.lots, False, vol, **{**BASE, **o})[0]

    cfg = {k: getattr(config, k) for k in dir(config) if k.isupper()}
    md.append("## Where setups drop out (default rules)\n\n| Index | Sweeps | Zones (sweep + MSS) | Retested | Confirmed | Signals |\n|---|--:|--:|--:|--:|--:|\n")
    for n in ("NIFTY", "SENSEX"):
        f = funnel(frames[n], cfg)
        md.append(f"| {n} | {f['sweeps']} | {f['zones']} | {f['retested']} | {f['confirmed']} | {f['signals']} |\n")
    md.append("\n## Default settings by index and window\n\n" + HEAD)
    keep = []
    for n in ("NIFTY", "SENSEX"):
        for w in windows:
            tr = run([n], w)
            md.append(row(f"{n}, {w} sessions", metrics(tr)))
            if w == max(windows):
                keep.append(tr)
    if len(all_for_verdict(keep)) < 30:
        md.append("\n**Verdict: not enough trades to judge.** Under 30 trades says nothing about an edge, either way. Don't read these rows as profit or loss to expect.\n")
    md.append("\n" + ("90 sessions was not run: Yahoo refuses 5-min data older than 60 days.\n\n" if 90 not in windows else "\n"))
    all_tr = pd.concat(keep) if keep else pd.DataFrame()
    all_tr.to_csv(os.path.join(OUT, "trades_default.csv"), index=False)

    w = max(windows)
    md.append(f"## Variants over {w} sessions, each index separately\n\nOne change at a time from the default. With this many variants, the best row is partly luck.\n\n" + HEAD)
    for label, o in VARIANTS:
        for n in ("NIFTY", "SENSEX"):
            md.append(row(f"{label}, {n}", metrics(run([n], w, **o))))
    md.append("\n## Cost stress over the same period\n\n" + HEAD)
    saved = backtest.SLIPPAGE
    for s in (0.004, 0.01, 0.02):
        backtest.SLIPPAGE = s
        for n in ("NIFTY", "SENSEX"):
            md.append(row(f"slippage {s:.1%} a side, {n}", metrics(run([n], w))))
    backtest.SLIPPAGE = saved

    if not all_tr.empty:
        t = all_tr.copy()
        t["hour"] = t.entry_time.str[:2] + ":00"
        labels = [regimes(frames[r["index"]], vix, r["date"]) for _, r in t.iterrows()]
        t["trend"], t["vol"] = [x[0] for x in labels], [x[1] for x in labels]
        for title, col in (("Time of entry", "hour"), ("Day type (labelled afterwards)", "trend"), ("Volatility (labelled afterwards)", "vol"), ("Direction", "side"), ("Exit reason", "reason")):
            md.append(f"\n## {title}, default settings, both indices\n\n" + HEAD)
            for k, g in t.groupby(col):
                md.append(row(str(k), metrics(g)))
    path = os.path.join(OUT, "RESULTS.md")
    with open(path, "w") as f:
        f.writelines(md)
    print("".join(md))


if __name__ == "__main__":
    main()
