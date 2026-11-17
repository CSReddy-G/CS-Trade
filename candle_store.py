"""
Local 5-minute candle store for backtests longer than Yahoo's ~60-day window.

Yahoo keeps only about 60 days of 5-minute candles. Every backtest download is merged into a file on disk
(data/candle_store.pkl, or KITEBOT_CANDLE_STORE), so the history keeps growing day by day instead of sliding away.
It is seeded once from the research dataset (research/cache/data_5m.pkl, from 17 Jul 2026) when that exists, and,
when a Zerodha session is passed in (KITEBOT_KITE_KEY / KITEBOT_KITE_TOKEN, set by the panel for a logged-in user),
older candles are fetched from Kite's historical API (5-minute data, up to 100 days per request).

Where two sources have the same candle, the newer download wins.
"""
import os
import pickle
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
BASE = os.path.dirname(os.path.abspath(__file__))
SEED = os.path.join(BASE, "research", "cache", "data_5m.pkl")
KITE_TOKENS = {"NIFTY": 256265, "SENSEX": 265}
COLS = ["Open", "High", "Low", "Close"]


def path():
    return os.environ.get("KITEBOT_CANDLE_STORE") or os.path.join(BASE, "data", "candle_store.pkl")


def _empty():
    return dict(idx={}, vol=pd.Series(dtype=float), vix={})


def load():
    p = path()
    if os.path.exists(p):
        try:
            with open(p, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass
    st = _empty()
    if os.path.exists(SEED):
        try:
            with open(SEED, "rb") as f:
                d = pickle.load(f)
            st = dict(idx={n: df[COLS].astype(float) for n, df in d["idx"].items()}, vol=d["vol"], vix=dict(d["vix"]))
        except Exception:
            st = _empty()
    return st


def save(st):
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        pickle.dump(st, f)
    os.replace(tmp, p)                  # atomic: two backtests at once never leave a half-written file


def _merge(old, new):
    if old is None or old.empty:
        return new.sort_index()
    if new is None or new.empty:
        return old
    return pd.concat([old[~old.index.isin(new.index)], new]).sort_index()


def add(st, name, df):
    """Merge 5-min OHLC (IST index, 09:15–15:25) for one index into the store."""
    if df is not None and not df.empty:
        st["idx"][name] = _merge(st["idx"].get(name), df[COLS].astype(float))


def add_volume(st, vol):
    if vol is not None and len(vol):
        st["vol"] = _merge(st["vol"], vol.astype(float))


def sessions(st, name):
    df = st["idx"].get(name)
    return [] if df is None or df.empty else sorted(set(df.index.date))


def from_kite(name, start, end, key, token):
    """5-min candles for [start, end] from Kite's historical API, as an IST DataFrame (empty on any failure)."""
    from kiteconnect import KiteConnect
    k = KiteConnect(api_key=key)
    k.set_access_token(token)
    rows, a = [], start
    while a < end:
        b = min(a + timedelta(days=95), end)                 # Kite: at most 100 days of 5-minute data per request
        rows += k.historical_data(KITE_TOKENS[name], a, b, "5minute")
        a = b + timedelta(minutes=1)
    if not rows:
        return pd.DataFrame(columns=COLS)
    df = pd.DataFrame([dict(t=r["date"], Open=r["open"], High=r["high"], Low=r["low"], Close=r["close"]) for r in rows])
    df["t"] = pd.to_datetime(df["t"])
    df["t"] = df["t"].dt.tz_localize(IST) if df["t"].dt.tz is None else df["t"].dt.tz_convert(IST)
    df = df.drop_duplicates("t").set_index("t").sort_index()
    return df.between_time("09:15", "15:25")


def fill_from_kite(st, names, days, log=print):
    """Fetch older candles from Kite when the store holds fewer than `days` + 10 sessions and a session is given."""
    key, token = os.environ.get("KITEBOT_KITE_KEY"), os.environ.get("KITEBOT_KITE_TOKEN")
    if not key or not token:
        return
    for n in names:
        have = sessions(st, n)
        if len(have) >= days + 10:
            continue
        end = datetime.now(IST)
        start = end - timedelta(days=int((days + 10) * 1.6) + 7)   # trading days → calendar days, with holidays
        try:
            df = from_kite(n, start, end, key, token)
            add(st, n, df)
            log(f"Note: {n} — {len(set(df.index.date))} sessions fetched from Zerodha's historical API.")
        except Exception as e:
            log(f"Note: {n} — Zerodha history unavailable ({str(e)[:90]}); using stored + Yahoo candles.")
