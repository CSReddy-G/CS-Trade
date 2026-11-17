"""
Relative volume for the VWAP and ORB strategies. Indices have no volume: candles carry a proxy's volume "v"
(or a ready-made relative volume "rvol" from the live bot — that always wins).

Two bases, chosen per strategy (VWAP_VOLUME_BASIS / ORB_VOLUME_BASIS):
  "rolling"     candle volume / average of the previous avg_n candles (the spec's rule). Bars with no volume are
                left out of the average as long as at least min_frac of the window has volume — Yahoo reports the
                proxy's 09:15 bar as 0 on most days, which would otherwise blank out the next 20 candles.
                Volume is U-shaped through the day, so this rule favours the open and the close.
  "time_of_day" candle volume / average volume of the SAME time slot over the previous `days` sessions, which
                removes the U-shape: 1.5x means unusual for that time of day.
Returns None when there isn't enough volume to say.
"""


def _day(c):
    t = c.get("t")
    return t.date() if hasattr(t, "date") else None


def rolling(candles, k, avg_n=20, min_frac=0.75):
    c = candles[k]
    if c.get("rvol") is not None:
        return c["rvol"]
    if not c.get("v"):
        return None
    prior = [p.get("v") for p in candles[max(0, k - avg_n):k]]
    vols = [v for v in prior if v]
    if len(prior) < avg_n or len(vols) < min_frac * avg_n:
        return None
    avg = sum(vols) / len(vols)
    return c["v"] / avg if avg > 0 else None


def time_of_day(candles, k, days=5, min_days=3):
    c = candles[k]
    if c.get("rvol") is not None:
        return c["rvol"]
    t = c.get("t")
    if not c.get("v") or not hasattr(t, "hour"):
        return None
    slot, today, seen, vols = (t.hour, t.minute), _day(c), set(), []
    for p in reversed(candles[:k]):
        pt, d = p.get("t"), _day(p)
        if d is None or d == today or d in seen:
            continue
        if (pt.hour, pt.minute) == slot:
            seen.add(d)
            if p.get("v"):
                vols.append(p["v"])
            if len(seen) >= days:
                break
    if len(vols) < min_days:
        return None
    avg = sum(vols) / len(vols)
    return c["v"] / avg if avg > 0 else None


def relative(candles, k, basis="rolling", avg_n=20, days=5):
    return time_of_day(candles, k, days) if basis == "time_of_day" else rolling(candles, k, avg_n)
