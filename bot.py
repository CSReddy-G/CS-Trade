"""
Nifty / Sensex options bot for Zerodha Kite Connect — strategy v2.

Signal : any mix of the strategies in strategies.py on 5-min index candles: strategy.signal_v2 (trend + momentum +
         confirmation), the Fair Value Gap retest (fvg.py) and the Order Block retest (orderblock.py).
         The first to fire takes the position, which then keeps that strategy's own exits.
Option : buys an IN-THE-MONEY option (ITM_STRIKES deep), nearest weekly expiry
Exits  : stop / 1:RR target / breakeven trail measured on the INDEX, or square-off
Filters: volume confirmation (NIFTYBEES proxy) and India-VIX-scaled stop/target (VOLUME_FILTER, VIX_ADJUST_EXITS)
Agents : analysts (fundamental, sentiment, news, technical, crude, global markets) → bull/bear debate →
         trader (BUY CE / BUY PE / HOLD) → risk manager (final approval). See agents/pipeline.py.
         Agents can only stop a trade; only the strategy signal can start one.
Safety : daily loss cap, max trades, losses-in-a-row lock, max open positions,
         exchange-side disaster stop in live mode, STOP-file kill switch, limit orders only.

Run via the control panel (app.py), or:  python login.py  then  python bot.py
"""
import csv, json, os, sys, threading, time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import config
import manual_exit
import heads_up
import manual_levels
import notify
import pairing
import strategies
import strategy
from agents import debate, load_cache, market, pipeline, risk_manager, technical, trader

IST = ZoneInfo("Asia/Kolkata")
STATE_FILE = "state.json"
TRADES_FILE = "trades.csv"


def now():
    return datetime.now(IST)


def hm(s):
    h, m = map(int, s.split(":"))
    return now().replace(hour=h, minute=m, second=0, microsecond=0)


def log(*a):
    msg = f"[{now():%H:%M:%S}] " + " ".join(str(x) for x in a)
    print(msg, flush=True)
    with open("bot.log", "a") as f:
        f.write(f"{now():%Y-%m-%d} {msg}\n")


def tick_round(p):
    return round(round(p / 0.05) * 0.05, 2)


# ------------------------------------------------------------------ risk state
class Risk:
    """Persists for the whole day, so restarting the bot does NOT reset limits."""

    def __init__(self):
        self.s = {"date": str(date.today()), "pnl": 0.0, "trades": 0,
                  "consec_losses": 0, "locked": False, "reason": ""}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                saved = json.load(f)
            if saved.get("date") == self.s["date"]:
                self.s = saved

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.s, f, indent=2)

    def record(self, pnl):
        self.s["pnl"] += pnl
        self.s["trades"] += 1
        self.s["consec_losses"] = self.s["consec_losses"] + 1 if pnl < 0 else 0
        if self.s["pnl"] <= -config.MAX_DAILY_LOSS:
            self.lock(f"daily loss limit hit (₹{self.s['pnl']:.0f})")
        elif self.s["consec_losses"] >= config.MAX_CONSECUTIVE_LOSSES:
            self.lock(f"{self.s['consec_losses']} losses in a row")
        elif self.s["trades"] >= config.MAX_TRADES_PER_DAY:
            self.lock("max trades for the day reached")
        self.save()

    def lock(self, reason):
        self.s["locked"], self.s["reason"] = True, reason
        log("RISK LOCK:", reason, "- no more trades today.")
        self.save()

    def can_trade(self):
        return not self.s["locked"]


# ------------------------------------------------------------------ broker
TERMINAL = ("COMPLETE", "REJECTED", "CANCELLED")
STOP_OPEN = ("TRIGGER PENDING", "OPEN")


class Fill:
    """What actually happened to an order: average price and FILLED quantity (not requested quantity).
    status is the order's last Kite status, or STOP_STILL_OPEN when sell() found the exchange stop
    could not be cancelled and therefore sent nothing (a second sell could leave you short)."""

    def __init__(self, price, qty, status="COMPLETE", oid=None):
        self.price, self.qty, self.status, self.oid = price, int(qty or 0), status, oid

    def __bool__(self):
        return self.qty > 0

    def __repr__(self):
        return f"Fill({self.price}, {self.qty}, {self.status}, {self.oid})"


class Broker:
    def __init__(self, kite, live):
        self.kite, self.live = kite, live
        self._instruments = {}
        self._seq = 0
        self._stop_trigger = {}  # sl order id -> current trigger; stops may only move up (we hold long options)

    def ltp(self, key):
        return self.kite.ltp([key])[key]["last_price"]

    def lots_to_buy(self, premium, lot, max_lots, in_use=0):
        """(lots, None), or (0, why) when the funds are too low or unknown. Live: Zerodha's Available to trade, which
        already excludes open positions. Paper: PAPER_FUNDS less what open positions (in_use) cost."""
        try:
            funds = self.kite.margins("equity")["net"] if self.live else config.PAPER_FUNDS - in_use
        except Exception as e:
            return 0, f"couldn't read available funds ({str(e)[:80]})"
        lots = strategy.lots_for_funds(funds, premium, lot, max_lots, config.FUNDS_USE_PCT, config.MAX_PREMIUM_PER_TRADE)
        if lots:
            return lots, None
        return 0, (f"not enough funds: 1 lot costs ₹{premium * lot:,.0f}, {config.FUNDS_USE_PCT:g}% of ₹{funds:,.0f} "
                   f"available (max premium ₹{config.MAX_PREMIUM_PER_TRADE:,.0f})")

    def option_for(self, spec, side, spot):
        """Nearest-expiry ITM option for this index."""
        ex = spec["exchange"]
        if ex not in self._instruments:
            self._instruments[ex] = [i for i in self.kite.instruments(ex)
                                     if i["name"] == spec["name"] and i["instrument_type"] in ("CE", "PE")]
        strike = strategy.itm_strike(spot, side, spec["step"], config.ITM_STRIKES)
        today = date.today()
        cands = [i for i in self._instruments[ex]
                 if i["instrument_type"] == side and float(i["strike"]) == strike and i["expiry"] >= today]
        if not cands:
            return None
        return min(cands, key=lambda i: i["expiry"])

    # ---- order plumbing (live only)
    def _new_tag(self):
        """Unique per order (Kite tags: alphanumeric, max 20), so a request that errored can be looked up."""
        self._seq += 1
        return f"{getattr(self, 'tag', 'kitebot')[:9]}{int(time.time()) % 10 ** 8:08d}{self._seq % 100:02d}"

    def _find_by_tag(self, tag):
        for _ in range(3):
            try:
                for o in self.kite.orders():
                    if o.get("tag") == tag or tag in (o.get("tags") or []):
                        return o["order_id"]
            except Exception as e:
                log("Order lookup failed:", e)
            time.sleep(1)
        return None

    def _order(self, ex, sym, side, qty, **kw):
        if not self.live:
            raise RuntimeError("paper mode: refusing to send a broker order")
        tag = self._new_tag()
        try:
            return self.kite.place_order(variety="regular", exchange=ex, tradingsymbol=sym, transaction_type=side,
                                         quantity=qty, product="MIS", tag=tag, **kw)
        except Exception as e:
            # a timeout can still have reached Zerodha — never send the same order twice
            oid = self._find_by_tag(tag)
            if oid:
                log(f"Order call errored ({e}) but Zerodha has it as {oid} — using that one.")
                return oid
            raise

    def order_state(self, oid):
        h = self.kite.order_history(oid)[-1]
        return dict(status=h["status"], filled=int(h.get("filled_quantity") or 0),
                    avg=h.get("average_price") or 0.0, msg=h.get("status_message"))

    def _settle(self, oid, secs=10):
        """Poll until the order reaches a final status (or give up after secs)."""
        st = self.order_state(oid)
        t0 = time.time()
        while st["status"] not in TERMINAL and time.time() - t0 < secs:
            time.sleep(1)
            st = self.order_state(oid)
        return st

    def _cancel(self, oid):
        try:
            self.kite.cancel_order("regular", oid)
        except Exception as e:
            log("Cancel of", oid, "failed (may already be filled):", e)
        return self._settle(oid)

    def _wait_fill(self, order_id, timeout=30):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.order_state(order_id)
            if st["status"] == "COMPLETE":
                return Fill(st["avg"], st["filled"], "COMPLETE", order_id)
            if st["status"] in ("REJECTED", "CANCELLED"):
                log("Order", order_id, st["status"], st["msg"], f"(filled {st['filled']})")
                return Fill(st["avg"] or None, st["filled"], st["status"], order_id)
            time.sleep(1)
        st = self._cancel(order_id)
        if st["status"] not in TERMINAL:
            log("!!! Order", order_id, "could not be cancelled (status", st["status"], ") — CHECK KITE !!!")
        elif st["filled"]:
            log(f"Order {order_id} only partly filled ({st['filled']}) before cancel.")
        else:
            log("Order not filled in time, cancelled.")
        return Fill(st["avg"] or None, st["filled"], st["status"], order_id)

    # ---- trading calls
    def buy(self, ex, sym, qty, ref):
        if not self.live:
            return Fill(ref, qty)
        oid = self._order(ex, sym, "BUY", qty, order_type="LIMIT", price=tick_round(ref * 1.01))
        return self._wait_fill(oid)

    def move_stop(self, sl_id, trigger):
        """Raise the exchange-side stop (e.g. to cost at 1:1) so it protects the gain if the bot dies.
        Refuses to lower it: the bot only holds long options, so a lower sell-stop means more risk."""
        if not self.live or not sl_id:
            return
        trigger = tick_round(trigger)
        if trigger <= self._stop_trigger.get(sl_id, 0):
            log(f"Not moving the exchange stop down ({self._stop_trigger[sl_id]} → {trigger}).")
            return
        try:
            self.kite.modify_order("regular", sl_id, trigger_price=trigger, price=tick_round(trigger * 0.97))
            self._stop_trigger[sl_id] = trigger
        except Exception as e:
            log("Could not move the exchange stop:", e)

    def set_stop(self, sl_id, trigger):
        """Move the exchange-side stop to exactly this trigger, up or down. Only for a stop the user edited by hand:
        a wider stop needs the exchange stop moved down too, or the old one would fill before the bot's new stop.
        Returns True when the exchange stop sits at the trigger afterwards (or there is none to move)."""
        if not self.live or not sl_id:
            return True
        trigger = tick_round(trigger)
        if trigger <= 0.5:
            return False
        if trigger == self._stop_trigger.get(sl_id):
            return True
        try:
            self.kite.modify_order("regular", sl_id, trigger_price=trigger, price=tick_round(trigger * 0.97))
            self._stop_trigger[sl_id] = trigger
            return True
        except Exception as e:
            log("Could not move the exchange stop:", e)
            return False

    def disaster_stop(self, ex, sym, qty, trigger):
        """Exchange-side stop far below the bot's own stop — protects you if the bot/PC dies."""
        if not self.live or trigger <= 0.5:
            return None
        oid = self._order(ex, sym, "SELL", qty, order_type="SL",
                          trigger_price=tick_round(trigger), price=tick_round(trigger * 0.95))
        self._stop_trigger[oid] = tick_round(trigger)
        return oid

    def sl_filled(self, sl_id):
        if not self.live or not sl_id:
            return None
        st = self.order_state(sl_id)
        return Fill(st["avg"], st["filled"], st["status"], sl_id) if st["status"] == "COMPLETE" else None

    def sell(self, ex, sym, qty, ref, sl_id=None):
        """Exit qty units. Returns a Fill for what was actually sold (exchange stop + exit order combined)."""
        if not self.live:
            return Fill(ref, qty)
        done, value, status, oid = 0, 0.0, "COMPLETE", None
        if sl_id:
            st = self._cancel(sl_id)
            done, value = st["filled"], st["filled"] * st["avg"]
            if st["status"] not in TERMINAL:
                log("!!! Exchange stop", sl_id, "is still live and could not be cancelled — not sending another sell")
                return Fill(st["avg"] or None, done, "STOP_STILL_OPEN", sl_id)
        rest = qty - done
        if rest > 0:
            try:
                oid = self._order(ex, sym, "SELL", rest, order_type="LIMIT", price=tick_round(ref * 0.98))
                f = self._wait_fill(oid, timeout=20)
                done, value, status = done + f.qty, value + f.qty * (f.price or 0), f.status
            except Exception as e:
                log("Exit order error:", e)
                status = "ERROR"
        if done < qty:
            log(f"!!! EXIT INCOMPLETE for {sym}: sold {done} of {qty} — the bot keeps trying; check Kite !!!")
        return Fill(value / done if done else None, done, "COMPLETE" if done >= qty else status, oid)


# ------------------------------------------------------------------ volume & VIX
def candle_volume(c):
    """Relative volume of a just-closed index candle, from VOLUME_SYMBOL's 1-min bars (indices have no
    volume). Returns (rvol or None, note)."""
    if c.get("t") is None:
        return None, "candle has no time"
    try:
        d = market.chart(config.VOLUME_SYMBOL, "1m", "5d", max_age=15)
    except Exception as e:
        return None, f"volume data unavailable ({str(e)[:60]})"
    rv, n = strategy.relative_volume(d["t"], d["v"], c["t"].timestamp(), config.CANDLE_MINUTES, config.VOLUME_AVG_N)
    return rv, f"{n} of {config.CANDLE_MINUTES} min of volume published"


def india_vix(broker):
    """India VIX now: Kite quote when the feed has it, else Yahoo, else the sentiment agent's last reading."""
    for fn in (lambda: broker.ltp("NSE:INDIA VIX"), lambda: market.chart("^INDIAVIX", "1m", "1d", max_age=60)["c"][-1],
               lambda: (load_cache().get("sentiment") or {})["vix"]):
        try:
            v = float(fn())
            if 5 < v < 100:
                return v
        except Exception:
            pass
    return None


# ------------------------------------------------------------------ candles
class Candles:
    """Builds OHLC candles from polled prices."""

    def __init__(self, minutes):
        self.m, self.done, self.cur, self.cur_start = minutes, [], None, None

    def bucket(self, t):
        mins = (t.hour * 60 + t.minute) // self.m * self.m
        return t.replace(hour=mins // 60, minute=mins % 60, second=0, microsecond=0)

    def update(self, t, p):
        """Returns True when a candle has just closed."""
        b = self.bucket(t)
        closed = False
        if self.cur_start is not None and b != self.cur_start:
            self.done.append(self.cur)
            self.done = self.done[-400:]
            closed = True
        if self.cur_start != b:
            self.cur_start, self.cur = b, dict(o=p, h=p, l=p, c=p, t=b)
        else:
            self.cur.update(h=max(self.cur["h"], p), l=min(self.cur["l"], p), c=p)
        return closed

    def sync(self, bars):
        """Rebuild today's candles from 1-minute bars (dict t, o, h, l, c; oldest first, the last one forming).
        A candle is closed once a bar from a later bucket exists. Keeps older history (warm-up) untouched.
        Returns True when a new closed candle appeared."""
        if not bars:
            return False
        before = self.done[-1]["t"] if self.done else None
        groups = {}
        for b in bars:
            k = self.bucket(b["t"])
            g = groups.get(k)
            if g is None:
                groups[k] = dict(o=b["o"], h=b["h"], l=b["l"], c=b["c"], t=k)
            else:
                g.update(h=max(g["h"], b["h"]), l=min(g["l"], b["l"]), c=b["c"])
        keys = sorted(groups)
        first, live_k = keys[0], keys[-1]
        for c in self.done:                                   # keep what was attached to a candle (e.g. rvol)
            g = groups.get(c.get("t"))
            if g is not None and c.get("t") != live_k:
                g.update({k: v for k, v in c.items() if k not in g})
        self.done = ([c for c in self.done if c.get("t") is None or c["t"] < first] + [groups[k] for k in keys[:-1]])[-400:]
        self.cur_start, self.cur = live_k, groups[live_k]
        after = self.done[-1]["t"] if self.done else None
        return before is not None and after is not None and after > before


class TickFeed:
    """Zerodha WebSocket ticks for the bot's indices (live mode, or paper on Zerodha prices). Ticks queue up
    between loop passes; the main loop drains them, so every tick reaches the candles (true highs and lows)
    and exits see the full price range since the last pass, not one 5-second sample."""

    def __init__(self, api_key, access_token, specs):
        import live
        self.q, self._lock = {n: [] for n in specs}, threading.Lock()
        self.last = {}
        self.stream = live.Stream(lambda: ("bot", api_key, access_token),
                                  tokens={s["token"]: n for n, s in specs.items()}, on_tick=self._tick)
        self.stream.ensure()

    def _tick(self, name, price, at):
        with self._lock:
            self.q[name].append((at, price))
            del self.q[name][:-2000]
            self.last[name] = at

    def drain(self, name):
        """Ticks received since the last call, [(epoch, price)], or [] when the stream isn't live."""
        self.stream.ensure()
        with self._lock:
            out, self.q[name] = self.q[name], []
        return out if self.stream.connected else []


def warmup(kite, spec, candles):
    try:
        data = kite.historical_data(spec["token"], now() - timedelta(days=6), now(),
                                    f"{config.CANDLE_MINUTES}minute")
        cur = candles.bucket(now())
        candles.done = [dict(o=c["open"], h=c["high"], l=c["low"], c=c["close"], t=c["date"].astimezone(IST))
                        for c in data if c["date"].astimezone(IST) < cur][-400:]
        log(f"{spec['name']}: warm-up loaded {len(candles.done)} candles.")
    except Exception as e:
        need = (config.EMA_TREND + 10) * config.CANDLE_MINUTES
        log(f"{spec['name']}: no historical data ({e}). Building live; first signal in ~{need} min.")




def option_levels(p):
    """Option price at the position's stop and target. Exact for premium exits; for index exits an estimate from the
    option's delta, since the option's price when the index gets there isn't known in advance."""
    d = 1 if p["side"] == "CE" else -1
    at = lambda level: round(max(0.05, p["entry"] + d * risk_manager.ITM_DELTA * (level - p["idx_entry"])), 2)
    stop = at(p["sl"])
    if p["be_done"] and not p.get("sl_edited"):   # never below cost after 1:1, but follow a trailing stop above it
        stop = max(stop, p["entry"] + p["cost_unit"])
    return round(p.get("prem_sl") or stop, 2), round(p.get("prem_target") or at(p["target"]), 2)


def write_status(**kw):
    kw["updated"] = now().strftime("%Y-%m-%d %H:%M:%S")
    kw["mode"] = config.MODE
    tmp = "status.json.tmp"
    with open(tmp, "w") as f:
        json.dump(kw, f, default=str)
    os.replace(tmp, "status.json")


TRADE_COLS = ["date", "mode", "symbol", "qty", "entry_time", "entry", "exit_time", "exit", "pnl", "reason",
              "index", "side", "idx_entry", "idx_sl", "idx_target", "idx_exit", "why", "charges", "net_pnl", "strategy"]


def migrate_trade_header(path):
    """Rewrite a trades file with an older header to the current columns."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        header = f.readline().strip().split(",")
    if header != TRADE_COLS:
        with open(path) as f:
            old = list(csv.DictReader(f))
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, TRADE_COLS); w.writeheader()
            for r in old:
                w.writerow({k: r.get(k, "") for k in TRADE_COLS})


def write_trade(row):
    new = not os.path.exists(TRADES_FILE)
    migrate_trade_header(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, TRADE_COLS)
        if new:
            w.writeheader()
        w.writerow(row)


# ------------------------------------------------------------------ restart safety
def pid_alive(pid, script):
    """True if pid is a running python process for script (guards against recycled pids)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    import subprocess
    out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    return script in out


def acquire_lock(path, script):
    """One copy per bot — a second copy would double every order. False if another copy is running."""
    try:
        with open(path) as f:
            pid = int(f.read().strip() or 0)
    except (OSError, ValueError):
        pid = 0
    if pid and pid != os.getpid() and pid_alive(pid, script):
        return False
    with open(path, "w") as f:
        f.write(str(os.getpid()))
    import atexit

    def release():
        try:
            with open(path) as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(path)
        except OSError:
            pass
    atexit.register(release)
    return True


def keep_awake():
    """Stop macOS idle-sleeping while this bot runs. A sleeping Mac gets no prices, so candles are
    built across the gap and skipped, and signals the backtest takes are silently missed.
    caffeinate -w exits by itself when this process ends. Closing the lid still sleeps the Mac."""
    if sys.platform != "darwin":
        return None
    import subprocess
    try:
        return subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log(f"Could not stop the Mac sleeping ({e}) — keep it awake yourself or signals will be missed.")
        return None


POSITIONS_FILE = "positions.json"


def save_positions(positions, path=None):
    data = dict(date=str(date.today()), mode=config.MODE,
                positions={n: dict(p, t=p["t"].isoformat()) for n, p in positions.items()})
    path = path or POSITIONS_FILE
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(path + ".tmp", path)


def load_positions(path=None):
    """Today's open positions for the current mode (paper positions never resume in live, or vice versa)."""
    try:
        with open(path or POSITIONS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if data.get("date") != str(date.today()) or data.get("mode") != config.MODE:
        return {}
    out = {}
    for n, p in (data.get("positions") or {}).items():
        p["t"] = datetime.fromisoformat(p["t"])
        out[n] = p
    return out


def reconcile(broker, saved, exchanges):
    """Live restart: compare the saved positions with Zerodha's MIS positions.
    Returns (positions to keep managing, [(name, pos, row)] closed while offline, [problems])."""
    rows = {r["tradingsymbol"]: r for r in broker.kite.positions().get("net", [])
            if r.get("product") == "MIS" and r.get("exchange") in exchanges}
    keep, closed, problems = {}, [], []
    for n, p in saved.items():
        r = rows.pop(p["sym"], None)
        q = int(r.get("quantity") or 0) if r else 0
        if r is None:
            problems.append(f"{p['sym']} was open but Zerodha shows no position — check Kite")
        elif q == 0:
            closed.append((n, p, r))
        elif q < 0:
            problems.append(f"{p['sym']}: Zerodha shows a SHORT position ({q}) — exit it manually")
        else:
            if q != p["qty"]:
                problems.append(f"{p['sym']}: Zerodha holds {q}, the bot had {p['qty']} — now tracking {q}")
                p["qty"] = q
            if p.get("sl_id"):
                try:
                    status = broker.order_state(p["sl_id"])["status"]
                except Exception:
                    status = "unknown"
                if status in STOP_OPEN:
                    broker._stop_trigger[p["sl_id"]] = p.get("sl_trigger", 0)
                else:
                    p["sl_id"] = None  # re-placed by the main loop
            keep[n] = p
    for sym, r in rows.items():
        if int(r.get("quantity") or 0):
            problems.append(f"{sym}: {r['quantity']} open at Zerodha but not opened by this bot — not managed")
    return keep, closed, problems


def is_fatal(e):
    """Errors that retrying can't fix (Kite plan lacks market data, login expired)."""
    return type(e).__name__ in ("PermissionException", "TokenException")


# ------------------------------------------------------------------ main loop
def main():
    live = config.MODE == "live"
    if not acquire_lock("bot.pid", "bot.py"):
        log("Another copy of the bot is already running — not starting a second one (it would double every order).")
        sys.exit(1)
    if keep_awake():
        log("Keeping the Mac awake while the bot runs (closing the lid will still sleep it).")
    for w in config.TIME_WARNINGS:
        log(w)
    active = strategies.active(config)
    if not active:
        log("No strategy selected — choose at least one in Settings → Strategies. Not starting.")
        sys.exit(1)
    paper_only = strategies.blocked_live(active, config) if live else []
    if paper_only:
        log(f"{' and '.join(paper_only)} is paper-only until its 'Allow live' switch is on in Settings → Strategies — not starting.")
        sys.exit(1)
    if live:
        ok = os.environ.get("BOT_CONFIRM_LIVE") or input("LIVE MODE - real money. Type YES to continue: ")
        if ok.strip() != "YES":
            sys.exit("Aborted.")

    if not live and config.PAPER_FEED == "free":
        from feed import FreeFeed
        kite = FreeFeed()
        log("Paper feed: FREE (Yahoo index prices, modelled option premiums — no Zerodha keys needed)")
    else:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=config.API_KEY)
        with open("access_token.txt") as f:
            kite.set_access_token(f.read().strip())
    broker, risk = Broker(kite, live), Risk()
    stock_auto = None
    if config.USE_STOCK_OPTIONS or os.path.exists("stock_positions.json"):
        import stock_options
        stock_auto = stock_options.StockOptionsBot(broker, risk, live, log, write_trade)
        if config.USE_STOCK_OPTIONS:
            log("Stock-option scanner enabled (long options only; same risk limits and square-off).")

    def stock_held():
        """The open stock option, if any: it shares MAX_OPEN_POSITIONS and the funds with the index positions."""
        return [stock_auto.position] if stock_auto and stock_auto.position else []
    names = config.instruments()
    specs = {n: config.INSTRUMENT_SPECS[n] for n in names}
    candles = {n: Candles(config.CANDLE_MINUTES) for n in names}
    bar_feed = hasattr(kite, "bars")              # free feed: candles from Yahoo's own 1-min bars
    ticks = None
    if getattr(config, "TICK_STREAM", True) and getattr(kite, "api_key", None) and getattr(kite, "access_token", None):
        try:
            ticks = TickFeed(kite.api_key, kite.access_token, specs)
            log("Price stream: Zerodha WebSocket ticks (REST quotes as fallback)")
        except Exception as e:
            log(f"Zerodha tick stream unavailable ({e}) — polling quotes every {config.POLL_SECONDS} s")
    runs = {n: strategies.runners(active, n, config.CANDLE_MINUTES, config) for n in names}
    wants_rvol = any(s.needs_rvol(config) for s in active)
    gate = pairing.make(config.BOTH_SAME_SIGNAL, names)
    strat = " + ".join(s.name for s in active) + (" (first signal wins)" if len(active) > 1 else "")
    log(f"Started {strat} in {config.MODE.upper()} mode on {', '.join(names)} | ITM {config.ITM_STRIKES} | "
        + " ".join(f"{s.id} 1:{s.rr(config):g}" for s in active) + " | agents on: "
        + (", ".join(k for k, _, flag, _ in pipeline.ANALYSTS if getattr(config, flag)) or "none")
        + f" | trader {'on' if config.USE_TRADER_AGENT else 'off'}")

    watcher = heads_up.Watcher()
    positions = load_positions()  # name -> dict; saved on every change so a restart resumes them
    spots, last_ok, gap_bucket = {}, {}, {}
    plist, errors = [], 0

    def book_exit(name, exit_px, qty, reason, idx_exit, t=None):
        """Record a fully closed position: trade row, net P&L into the risk limits, forget it."""
        p = positions.pop(name)
        gross = (exit_px - p["entry"]) * qty
        ch = strategy.charges(p["entry"], exit_px, qty, p["exchange"])
        log(f"EXIT {p['sym']} @ {exit_px:.2f} ({reason}; {name} {idx_exit:.1f}) "
            f"P&L ₹{gross:.0f}, net ₹{gross - ch:.0f} after ≈₹{ch:.0f} charges")
        write_trade(dict(date=date.today(), mode=config.MODE, symbol=p["sym"], qty=qty,
                         entry_time=f"{p['t']:%H:%M}", entry=p["entry"], exit_time=f"{(t or now()):%H:%M}",
                         exit=round(exit_px, 2), pnl=round(gross, 2), reason=reason, index=name, side=p["side"],
                         idx_entry=round(p["idx_entry"], 2), idx_sl=round(p["sl0"], 2),
                         idx_target=round(p["target"], 2), idx_exit=round(idx_exit, 2), why=p["why"],
                         charges=ch, net_pnl=round(gross - ch, 2), strategy=p.get("strategy") or "ema"))
        risk.record(gross - ch)
        save_positions(positions)

    def close(name, reason, idx_exit):
        """Exit a position. A partial exit keeps the rest open (and locks new entries) until it is sold."""
        p = positions[name]
        px = broker.ltp(p["key"])
        f = broker.sell(p["exchange"], p["sym"], p["qty"], px, p.get("sl_id"))
        if f.status != "STOP_STILL_OPEN":
            p["sl_id"] = None  # sell() cancelled (or found filled) the exchange stop
        if f.qty:
            p["sold"] = p.get("sold", 0) + f.qty
            p["sold_value"] = p.get("sold_value", 0.0) + f.qty * f.price
            p["qty"] -= f.qty
        if p["qty"] > 0:
            risk.lock(f"exit of {p['sym']} incomplete ({p['qty']} left) - bot keeps trying; check Kite")
            save_positions(positions)
            return
        book_exit(name, p["sold_value"] / p["sold"], p["sold"], reason, idx_exit)

    def book_partial(name, idx_level):
        """Partial profit: sell the strategy's share (whole lots) and keep managing the rest. The exchange stop is
        cancelled by sell() and re-placed for the remaining quantity by ensure_stop."""
        p = positions[name]
        part = strategies.spec_of(p).partial_qty(p["qty"], p.get("lot", p["qty"]), config)
        if not part:
            log(f"{name}: partial target reached, but {p['qty']} units can't be split into whole lots — holding all")
            save_positions(positions)
            return
        f = broker.sell(p["exchange"], p["sym"], part, broker.ltp(p["key"]), p.get("sl_id"))
        if f.status != "STOP_STILL_OPEN":
            p["sl_id"] = None
        if f.qty:
            p["sold"] = p.get("sold", 0) + f.qty
            p["sold_value"] = p.get("sold_value", 0.0) + f.qty * f.price
            p["qty"] -= f.qty
            log(f"PARTIAL EXIT {p['sym']} {f.qty} @ {f.price:.2f} ({name} {idx_level:.1f}); {p['qty']} left")
        else:
            log(f"{name}: partial exit of {part} {p['sym']} not filled ({f.status}) — holding all; not retried")
        if p["be_done"]:  # the re-placed exchange stop must not sit below cost
            p["dstop"] = max(p["dstop"], (p["entry"] + p["cost_unit"]) * 0.98)
        if p["qty"] <= 0:
            book_exit(name, p["sold_value"] / p["sold"], p["sold"], "partial exit filled the stop", idx_level)
            return
        save_positions(positions)
        ensure_stop(p)

    def ensure_stop(p):
        """Live: keep an exchange-side stop on every open position; retry every 30 s if placing it failed."""
        if not live or p.get("sl_id") or time.time() - p.get("sl_try", 0) < 30:
            return
        p["sl_try"] = time.time()
        try:
            p["sl_id"] = broker.disaster_stop(p["exchange"], p["sym"], p["qty"], p["dstop"])
            p["sl_trigger"] = broker._stop_trigger.get(p["sl_id"], p["dstop"])
            if p["sl_id"]:
                log(f"Exchange stop placed for {p['sym']} (trigger {p['sl_trigger']})")
        except Exception as e:
            log(f"!!! Exchange stop for {p['sym']} not placed ({e}) — the bot's own stop is active; retrying !!!")
        save_positions(positions)

    def apply_levels(n, p, cmd, spot):
        """The user's new stop/target from the panel: checked against the live price, then used from this pass on."""
        lv = cmd.get("levels") or {}
        sl, tg, psl, ptg = (lv.get(k) for k in manual_levels.FIELDS)
        why = manual_levels.check(p["side"] == "CE", spot, sl, tg, f"{n.title()} level")
        if not why and (psl is not None or ptg is not None):
            why = ("This trade has no option-price stop/target to edit." if p.get("prem_sl") is None else
                   manual_levels.check(True, broker.ltp(p["key"]), psl, ptg, "option price"))
        if why:
            log(f"{n}: stop/target edit for {p['sym']} refused — {why}")
            manual_levels.finish(manual_levels.BOT_FILE, False, why)
            return
        done = []
        if sl is not None:
            done.append(f"stop {p['sl']:.1f} → {sl:.1f}")
            p["sl"], p["sl_edited"], p["trailed"] = sl, True, False
        if tg is not None:
            done.append(f"target {p['target']:.1f} → {tg:.1f}")
            p["target"] = tg
        if psl is not None:
            done.append(f"option stop {p['prem_sl']:.2f} → {psl:.2f}")
            p["prem_sl"] = psl
        if ptg is not None:
            done.append(f"option target {p['prem_target']:.2f} → {ptg:.2f}")
            p["prem_target"] = ptg
        note = ""
        if sl is not None or psl is not None:
            # the exchange stop is a backstop beyond the bot's own stop: if the stop was widened, move it down to keep
            # the same gap (dstop = entry − risk, the bot's stop ≈ entry − delta × risk), never up
            d = 1 if p["side"] == "CE" else -1
            stop_px = p["entry"] + d * risk_manager.ITM_DELTA * (p["sl"] - p["idx_entry"])
            if p.get("prem_sl") is not None:
                stop_px = max(stop_px, p["prem_sl"])
            need = round(p["entry"] - (p["entry"] - stop_px) / risk_manager.ITM_DELTA, 2)
            if need < p.get("dstop", need + 1):
                p["dstop"] = need
            cur = p.get("sl_trigger") if p.get("sl_id") else None
            if cur is not None and need < cur:
                if broker.set_stop(p["sl_id"], need):
                    p["sl_trigger"] = broker._stop_trigger.get(p["sl_id"], need)
                    note = f"; exchange stop moved down to {p['sl_trigger']}"
                else:
                    note = f"; the exchange stop at {cur} could NOT be moved down, so it may fill before the new stop"
        save_positions(positions)
        msg = f"{p['sym']}: " + ", ".join(done) + note
        log(f"{n}: levels edited from the panel — {msg}")
        manual_levels.finish(manual_levels.BOT_FILE, True, msg)

    if positions:
        log("Resuming open positions from before the restart:", ", ".join(p["sym"] for p in positions.values()))
    if live:
        try:
            positions, closed, problems = reconcile(broker, positions, {s["exchange"] for s in specs.values()})
        except Exception as e:
            closed, problems = [], [f"could not read positions from Zerodha ({e!r}) — check Kite"]
        for n, p, r in closed:
            positions[n] = p
            px = float(r.get("sell_price") or 0) or p["entry"]
            book_exit(n, px, p["qty"], "closed while bot was offline", spots.get(n, p["idx_entry"]))
        for pr in problems:
            log("RESTART CHECK:", pr)
        if problems:
            risk.lock("restart check: " + problems[0])
    save_positions(positions)

    for n in names:
        warmup(kite, specs[n], candles[n])
        gap_bucket[n] = candles[n].bucket(now())  # the candle in progress at start has no true open
        for r in runs[n]:
            r.replay(candles[n].done)             # zones already traded in history stay spent
            snap = r.snapshot()
            if snap and "active" in snap:         # zone engines (FVG, OB); the others have no zone list
                log(f"{n}: {r.spec.name} engine — {len(snap['active'])} live zone(s) after warm-up")
            elif snap:
                log(f"{n}: {r.spec.name} engine ready after warm-up"
                    + (f" — {snap['status']}" if snap.get("status") else ""))

    # agents refresh in a background thread so slow websites never delay exits
    import threading

    def agent_loop():
        while True:
            for e in pipeline.refresh(config):
                log("Agent error:", e)
            time.sleep(60)
    threading.Thread(target=agent_loop, daemon=True).start()

    while True:
        try:
            t = now()
            if os.path.exists("STOP"):
                log("STOP file found.")
                for _ in range(3):
                    if stock_auto:
                        stock_auto.close_all("kill switch", t)
                    for n in list(positions):
                        close(n, "kill switch", spots.get(n, 0))
                    if not positions and not (stock_auto and stock_auto.position):
                        break
                    time.sleep(5)
                if positions:
                    log("!!! Could not exit", list(positions), "- EXIT MANUALLY IN KITE !!!")
                if stock_auto and stock_auto.position:
                    log("!!! Could not exit stock option", stock_auto.position["sym"], "- EXIT MANUALLY IN KITE !!!")
                break
            if t.weekday() >= 5 or t < hm("09:15"):
                write_status(state="waiting for market open", positions=[], spots={}, risk=risk.s,
                             instruments=names)
                time.sleep(30); continue
            if t >= hm(config.SQUARE_OFF_AT):
                if stock_auto:
                    stock_auto.close_all("square-off time", t)
                for n in list(positions):
                    close(n, "square-off time", broker.ltp(specs[n]["index"]))
                if (positions or (stock_auto and stock_auto.position)) and t < hm("15:25"):
                    write_status(state="square-off: exit pending", positions=plist, spots=spots, risk=risk.s,
                                 instruments=names, stock_options=stock_auto.snapshot() if stock_auto else None)
                    time.sleep(config.POLL_SECONDS); continue
                if positions:
                    log("!!! Square-off failed for", list(positions), "- EXIT MANUALLY IN KITE NOW !!!")
                if stock_auto and stock_auto.position:
                    log("!!! Stock-option square-off failed for", stock_auto.position["sym"], "- EXIT MANUALLY IN KITE NOW !!!")
                log(f"Day done. P&L ₹{risk.s['pnl']:.0f} net, trades {risk.s['trades']}.")
                write_status(state="day finished", positions=[], spots=spots, risk=risk.s, instruments=names,
                             stock_options=stock_auto.snapshot() if stock_auto else None)
                break

            wanted = manual_exit.take(manual_exit.BOT_FILE, [p["sym"] for p in positions.values()])
            for p in positions.values():
                if p["sym"] == wanted:  # the flag is saved, so the exit keeps retrying until it is flat, even over a restart
                    p["manual_exit"] = True
                    log(f"Manual exit requested for {wanted} from the panel")
                    save_positions(positions)
            edit = manual_levels.take(manual_levels.BOT_FILE, [p["sym"] for p in positions.values()])

            for n in names:
                spec = specs[n]
                tk = ticks.drain(n) if ticks else []
                spot = tk[-1][1] if tk else broker.ltp(spec["index"])
                if n in last_ok and (t - last_ok[n]).total_seconds() > max(60, 6 * config.POLL_SECONDS):
                    gap_bucket[n] = candles[n].bucket(t)
                    log(f"{n}: no price for {(t - last_ok[n]).seconds} s — no entries until a clean candle.")
                last_ok[n] = t
                spots[n] = round(spot, 2)
                if tk:
                    new_candle = False
                    for at, px in tk:
                        new_candle = candles[n].update(datetime.fromtimestamp(at, IST), px) or new_candle
                    hi, lo = max(px for _, px in tk), min(px for _, px in tk)
                elif bar_feed:
                    new_candle = candles[n].sync(kite.bars(spec["index"]))
                    hi = lo = spot
                else:
                    new_candle = candles[n].update(t, spot)
                    hi = lo = spot

                # ---- manage open position on index levels
                if n in positions:
                    p = positions[n]
                    ensure_stop(p)
                    f = broker.sl_filled(p.get("sl_id"))
                    if f:  # disaster stop hit at exchange
                        p["sl_id"] = None
                        book_exit(n, f.price, f.qty, "disaster stop", spot, t)
                    elif p.get("manual_exit"):
                        close(n, "manual exit", spot)
                    else:
                        if edit and edit.get("symbol") == p["sym"]:
                            apply_levels(n, p, edit, spot)
                            edit = None
                        was_be, sl_before = p["be_done"], p["sl"]
                        owner = strategies.spec_of(p)
                        lvl, why = owner.manage(p, hi, lo, config, now=t, close=spot)
                        prem_px = broker.ltp(p["key"]) if lvl is None and p.get("prem_sl") else None
                        if lvl is not None:
                            close(n, why, spot)
                        elif prem_px is not None and prem_px <= p["prem_sl"]:
                            close(n, f"option stop {p['prem_sl']:.2f}", spot)
                        elif prem_px is not None and prem_px >= p["prem_target"]:
                            close(n, f"option target {p['prem_target']:.2f}", spot)
                        elif owner.partial_due(p, hi, lo, config) is not None:
                            book_partial(n, spot)
                        elif (p["be_done"] and not was_be) or p["sl"] != sl_before:
                            # the stop moved on this index update (to cost at 1:1, or trailing): log it, raise the
                            # exchange backstop to match and persist it, so the dashboard and a restart see the new stop
                            opt_stop = option_levels(p)[0]
                            if p["be_done"] and not was_be:
                                log(f"{n}: 1:1 reached — stop moved to lock profit (index {p['sl']:.1f}, "
                                    f"option ≈ {opt_stop:.2f} incl. charges)")
                            else:
                                log(f"{n}: trailing stop {sl_before:.1f} → {p['sl']:.1f} on the index "
                                    f"(option ≈ {opt_stop:.2f})")
                            broker.move_stop(p.get("sl_id"), opt_stop * 0.98)
                            p["sl_trigger"] = broker._stop_trigger.get(p.get("sl_id"), p.get("sl_trigger"))
                            save_positions(positions)
                        elif p["be_done"] and config.COST_STOP_ON_PREMIUM:
                            if broker.ltp(p["key"]) <= p["entry"] + p["cost_unit"]:
                                close(n, "stop at cost", spot)

                # ---- every strategy sees every closed candle (zones age, get touched, invalidated)
                cands = []
                if new_candle:
                    c = candles[n].done[-1]
                    clean = not (n in gap_bucket and c.get("t") and c["t"] <= gap_bucket[n])
                    if clean and wants_rvol:
                        c["rvol"] = candle_volume(c)[0]
                    can_enter = (n not in positions and risk.can_trade() and len(positions) + len(stock_held()) < config.MAX_OPEN_POSITIONS
                                 and hm(config.NO_ENTRY_BEFORE) <= t <= hm(config.NO_ENTRY_AFTER))
                    cands, opposite = strategies.evaluate(runs[n], candles[n].done, can_enter, positions.get(n), config, clean)
                    for s in cands:
                        if s.get("strategy") != "ema":
                            log(f"{n}: {strategies.spec_of(s).name} signal {s['side']} @ {s['entry']:.1f} SL {s['sl']:.1f} "
                                f"TGT {s['target']:.1f} — {s['why']}")
                    if opposite:
                        close(n, f"opposite {strategies.spec_of(opposite).short} signal", spot)
                        continue
                    if n in positions and clean:   # strategies 4-7: exit when the setup that opened the trade broke
                        broke = strategies.spec_of(positions[n]).invalidates(positions[n], candles[n].done, config)
                        if broke:
                            close(n, f"{strategies.spec_of(positions[n]).short} setup broke: {broke}", spot)
                            continue
                    # ---- early warning: a signal may come on the next candle(s) (alerts only, never an order)
                    if (getattr(config, "HEADS_UP_ALERTS", False) and clean and not cands and n not in positions
                            and risk.can_trade() and hm(config.NO_ENTRY_BEFORE) - timedelta(minutes=15) <= t
                            <= hm(config.NO_ENTRY_AFTER)):
                        try:
                            for w in watcher.check(n, runs[n], candles[n].done, config, t.date()):
                                log(f"{n}: heads-up ({w['stage']}) {w['side']} — {w['text']}")
                                notify.heads_up(config, log, index=n, spot=spot, side=w["side"], stage=w["stage"],
                                                text=w["text"], level=w.get("level"), tf=config.CANDLE_MINUTES,
                                                at=f"{now():%H:%M} IST")
                        except Exception as e:
                            log(f"{n}: heads-up check failed: {e!r}")

                # ---- new entries on candle close: the first signal wins, a later candidate only if the first falls away
                def skip_alert(sig, reason, symbol=None, n=n, spot=spot):
                    try:
                        notify.skipped(config, log, mode=config.MODE, index=n, spot=spot, side=sig["side"],
                                       strategy=strategies.spec_of(sig).name, entry=sig.get("entry"), sl=sig.get("sl"),
                                       target=sig.get("target"), reason=reason, why=sig.get("why"), symbol=symbol,
                                       at=f"{now():%H:%M} IST")
                    except Exception as e:
                        log(f"{n}: skipped-signal alert failed: {e!r}")

                half = False
                if gate:                       # trading both: a signal waits for the other index's same candle
                    if cands and n not in positions:
                        gate.offer(n, candles[n].done[-1]["t"], cands, t)
                    cands, half, note = gate.decide(n, {x: candles[x].done[-1].get("t") for x in names if candles[x].done}, t)
                    if note:
                        log(note)
                    if not cands:
                        continue
                elif not new_candle:
                    continue
                if n in positions:
                    for sig in cands:
                        skip_alert(sig, f"already in a {n} trade ({positions[n]['sym']})")
                    continue
                if n in gap_bucket and candles[n].done[-1].get("t") and candles[n].done[-1]["t"] <= gap_bucket[n]:
                    for sig in cands:
                        skip_alert(sig, "this candle was built across a price-feed gap")
                    continue  # this candle was built across a price gap
                if not (risk.can_trade() and hm(config.NO_ENTRY_BEFORE) <= t <= hm(config.NO_ENTRY_AFTER)):
                    why_not = (f"risk limit: {risk.s.get('reason') or 'daily limits reached'}" if not risk.can_trade()
                               else f"outside the entry window {config.NO_ENTRY_BEFORE}–{config.NO_ENTRY_AFTER}")
                    for sig in cands:
                        skip_alert(sig, why_not)
                    continue
                # the two halves of one split signal are one bet, so together they fill one position slot
                held_now = len(positions) + len(stock_held()) - (1 if half else 0)
                if held_now >= config.MAX_OPEN_POSITIONS:
                    for sig in cands:
                        skip_alert(sig, f"max open positions reached ({held_now} of {config.MAX_OPEN_POSITIONS})")
                    continue
                if len(cands) > 1:
                    log(f"{n}: {' and '.join(strategies.spec_of(s).name for s in cands)} fired on the same candle — "
                        f"{strategies.spec_of(cands[0]).name} goes first")
                for sig in cands:
                    spec_s = strategies.spec_of(sig)
                    if config.VOLUME_FILTER and spec_s.shared_filters:  # FVG and OB check volume on their displacement candle
                        rv, note = candle_volume(candles[n].done[-1])
                        if rv is None:
                            log(f"{n}: {sig['side']} signal — volume check skipped: {note}")
                        elif rv < config.VOLUME_MIN_RVOL:
                            log(f"{n}: {sig['side']} signal skipped — volume {rv:.2f}x normal, needs {config.VOLUME_MIN_RVOL:g}x ({note})")
                            pipeline.record(n, sig, dict(margin=0.0, verdict="not run", flags=[], challenges=[]),
                                            dict(action="SKIP", reason="low volume"), None,
                                            f"skipped — volume {rv:.2f}x normal (needs {config.VOLUME_MIN_RVOL:g}x)")
                            skip_alert(sig, f"low volume {rv:.2f}x normal (needs {config.VOLUME_MIN_RVOL:g}x)")
                            continue
                        else:
                            sig = dict(sig, rvol=round(rv, 2), why=sig["why"] + f", volume {rv:.1f}x")
                    if config.VIX_ADJUST_EXITS and spec_s.shared_filters:  # FVG and OB stops sit on structure
                        vix = india_vix(broker)
                        if vix:
                            sig = strategy.vix_scale(sig, vix, config)
                        else:
                            log(f"{n}: India VIX unavailable — stop/target not VIX-adjusted")
                    # ---- agent team: analysts → debate → trader → risk manager
                    reports = pipeline.reports(config)
                    if config.USE_TECHNICAL_AGENT:  # fresh read on this index's own candles
                        reports["technical"] = technical.analyse(candles[n].done, n.title())
                    deb = debate.run(reports, sig["side"])
                    dec = trader.decide(sig, deb, config)
                    if config.USE_TRADER_AGENT and dec["action"] == "HOLD":
                        log(f"{n}: {sig['side']} signal — trader HOLD: {dec['reason']}")
                        pipeline.record(n, sig, deb, dec, None, "held by trader")
                        skip_alert(sig, f"trader agent HOLD — {dec['reason']}")
                        continue
                    inst = broker.option_for(spec, sig["side"], spot)
                    if not inst:
                        log(f"{n}: no ITM option found for {sig['side']}")
                        skip_alert(sig, "no ITM option contract found"); continue
                    key = f"{spec['exchange']}:{inst['tradingsymbol']}"
                    px = broker.ltp(key)
                    lots, short = broker.lots_to_buy(px, inst["lot_size"], config.LOTS,
                                                     sum(p["entry"] * p["qty"] for p in [*positions.values(), *stock_held()]))
                    if not lots:
                        log(f"{n}: {sig['side']} {inst['tradingsymbol']} skipped — {short}")
                        pipeline.record(n, sig, deb, dec, None, "skipped: " + short)
                        skip_alert(sig, short, inst["tradingsymbol"])
                        continue
                    if half:
                        lots = pairing.half_lots(n, lots)
                        if not lots:
                            log(f"{n}: {sig['side']} skipped — one lot can't be halved, so only Sensex takes this signal")
                            pipeline.record(n, sig, deb, dec, None, "skipped: one lot can't be halved")
                            skip_alert(sig, "one lot can't be halved, so only Sensex takes this signal", inst["tradingsymbol"])
                            continue
                    sized = risk_manager.size_for_limits(sig, lots, inst["lot_size"], px, risk.s, config)
                    cut = f" — lots cut from {lots} to {sized} to fit the risk limits" if sized < lots else ""
                    qty = inst["lot_size"] * sized
                    rm = risk_manager.review(sig, qty, px, risk.s, max(0, held_now), reports.get("crude"),
                                             reports.get("news"), reports.get("fundamental"), config, reports.get("global"))
                    if not rm["approved"]:
                        log(f"{n}: {sig['side']} {inst['tradingsymbol']} vetoed by risk manager — {rm['reason']}")
                        pipeline.record(n, sig, deb, dec, rm, "vetoed by risk manager")
                        skip_alert(sig, f"risk manager veto — {rm['reason']}", inst["tradingsymbol"])
                        continue
                    log(f"{n}: trader {dec['action']} (conviction {dec['conviction']:.2f}, debate {deb['margin']:+.2f}); "
                        f"risk manager approved{cut}")
                    fill = broker.buy(spec["exchange"], inst["tradingsymbol"], qty, px)
                    if fill.status not in TERMINAL:
                        risk.lock(f"buy order {fill.oid} still open after cancel - check Kite")
                    if not fill:
                        pipeline.record(n, sig, deb, dec, rm, "order not filled")
                        skip_alert(sig, "buy order was not filled", inst["tradingsymbol"])
                        break  # an order was attempted: never send a second one in the same pass
                    if fill.qty < qty:
                        log(f"{n}: partial fill — holding {fill.qty} of {qty}")
                    qty = fill.qty
                    pipeline.record(n, sig, deb, dec, rm, f"bought {inst['tradingsymbol']} x{qty} @ {fill.price:.2f}{cut}")
                    # re-anchor index levels to the live spot at fill; record the position BEFORE the exchange stop,
                    # so a failed stop order can never leave a bought option untracked
                    shift = spot - sig["entry"]
                    prem = spec_s.premium_levels(fill.price, sig, config)
                    positions[n] = dict(sym=inst["tradingsymbol"], key=key, exchange=spec["exchange"], qty=qty,
                                        entry=fill.price, side=sig["side"], t=t, idx_entry=spot,
                                        sl=sig["sl"] + shift, sl0=sig["sl"] + shift, target=sig["target"] + shift,
                                        risk=sig["risk"], be_done=False, why=sig["why"], best=spot,
                                        cost_pts=strategy.cost_points(fill.price, qty),
                                        cost_unit=strategy.cost_per_unit(fill.price, qty),
                                        dstop=fill.price - 1.0 * sig["risk"],  # ITM delta ~0.6-0.7: well beyond the bot's stop
                                        sl_id=None, buy_oid=fill.oid, strategy=sig.get("strategy", "ema"),
                                        lot=inst["lot_size"], fvg_id=sig.get("fvg_id"), ob_id=sig.get("ob_id"),
                                        score=sig.get("score"), prem_sl=prem[0] if prem else None,
                                        prem_target=prem[1] if prem else None,
                                        # structure levels the setup-invalidation exit checks (index, not re-anchored)
                                        orb_high=sig.get("orb_high"), orb_low=sig.get("orb_low"),
                                        sweep_extreme=sig.get("sweep_extreme"))
                    save_positions(positions)
                    p = positions[n]
                    ensure_stop(p)
                    log(f"ENTRY {sig['side']} {p['sym']} x{qty} @ {fill.price:.2f} | {n} {spot:.1f} "
                        f"SL {p['sl']:.1f} TGT {p['target']:.1f} (1:{abs(sig['target'] - sig['entry']) / sig['risk']:.1f}) "
                        f"[{spec_s.name}] | {sig['why']}")
                    notify.entry(config, log, bot="Main bot", mode=config.MODE, side=sig["side"], symbol=p["sym"], qty=qty,
                                 price=fill.price, lot=inst["lot_size"], index=n, spot=spot, sl=p["sl"], target=p["target"],
                                 prem_sl=p.get("prem_sl"), prem_target=p.get("prem_target"),
                                 rr=abs(sig["target"] - sig["entry"]) / sig["risk"] if sig.get("risk") else None,
                                 strategy=spec_s.name, why=sig["why"],
                                 extra=[("Agents", f"trader {dec['action']} · conviction {dec['conviction']:.2f} · debate {deb['margin']:+.2f}")],
                                 at=f"{now():%d %b %Y, %H:%M} IST", trades_today=risk.s["trades"] + len(positions),
                                 max_trades=config.MAX_TRADES_PER_DAY)
                    break

            if stock_auto:
                stock_auto.tick(t, positions)

            # ---- status for dashboard
            if edit:  # taken but its position was being exited or closed this pass
                manual_levels.finish(manual_levels.BOT_FILE, False, f"{edit.get('symbol')} is being exited, so the change wasn't applied.")
            manual_exit.settle(manual_exit.BOT_FILE, [p["sym"] for p in positions.values()])
            plist = []
            for n, p in positions.items():
                ltp = broker.ltp(p["key"])
                opt_sl, opt_target = option_levels(p)
                plist.append(dict(index=n, symbol=p["sym"], side=p["side"], qty=p["qty"], entry=round(p["entry"], 2),
                                  ltp=ltp, pnl=round((ltp - p["entry"]) * p["qty"], 2), since=p["t"].strftime("%H:%M"),
                                  idx=spots.get(n), idx_sl=round(p["sl"], 1), idx_target=round(p["target"], 1),
                                  idx_entry=round(p["idx_entry"], 1), idx_sl0=round(p.get("sl0", p["sl"]), 1),
                                  since_ts=int(p["t"].timestamp()), trailed=bool(p.get("trailed")),
                                  breakeven=p["be_done"], exchange_stop=bool(p.get("sl_id")) if live else None,
                                  strategy=p.get("strategy") or "ema", score=p.get("score"), prem_sl=p.get("prem_sl"),
                                  prem_target=p.get("prem_target"), opt_sl=opt_sl, opt_target=opt_target,
                                  exchange_trigger=p.get("sl_trigger") if p.get("sl_id") else None,
                                  exit_pending=bool(p.get("manual_exit"))))
            errors = 0
            write_status(state="running", positions=plist, spots=spots, risk=risk.s, instruments=names,
                         strategies=[s.id for s in active],
                         fvg={n: r.snapshot() for n in names for r in runs[n] if r.spec.id == "fvg"},
                         ob={n: r.snapshot() for n in names for r in runs[n] if r.spec.id == "ob"},
                         strategy_state={sid: {n: r.snapshot() for n in names for r in runs[n] if r.spec.id == sid}
                                         for sid in ("vwap", "orb", "lsmss", "supertrend")},
                         candles={n: len(candles[n].done) for n in names},
                         last_price={n: f"{v:%H:%M:%S}" for n, v in last_ok.items()},
                         feed="zerodha ticks" if ticks and ticks.stream.connected else "polling",
                         stock_options=stock_auto.snapshot() if stock_auto else None)
            # with ticks streaming, prices cost no REST calls, so a 2 s pass reacts faster to candle closes and exits
            # while the remaining quote calls (open option's price) stay under Zerodha's 1-per-second limit
            time.sleep(min(2, config.POLL_SECONDS) if ticks and ticks.stream.connected else config.POLL_SECONDS)
        except KeyboardInterrupt:
            log("Ctrl+C - stopping.")
            if positions:
                log("!!! Open positions", list(positions), "- exit manually in Kite or rerun bot !!!")
            break
        except Exception as e:
            errors += 1
            if errors == 1 or errors % 30 == 0:
                log(f"Error ({errors} in a row):", repr(e))
            if is_fatal(e) and not positions:
                msg = ("Zerodha refused the call: " + str(e) + " — your Kite Connect plan may not include market "
                       "data, or the login expired. Log in again, or use Paper feed = free.")
                log(msg)
                write_status(state="error", error=msg, positions=[], spots=spots, risk=risk.s, instruments=names)
                break
            write_status(state="error", error=repr(e)[:200], errors_in_row=errors, positions=plist, spots=spots,
                         risk=risk.s, instruments=names)
            time.sleep(10)


if __name__ == "__main__":
    main()
