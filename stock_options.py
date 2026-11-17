"""Risk-managed stock-option automation for the main bot process."""
import json
import os
import re
import threading
import time
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import config
import notify
import strategy
from agents import debate, market, pipeline, risk_manager, trader
import stocks

IST = ZoneInfo("Asia/Kolkata")
POSITIONS_FILE = "stock_positions.json"
COMMAND_FILE = "stock_command.json"
SCAN_SECONDS = 900
MAX_QUOTE_AGE = 180


def signal_ready(analysis):
    """Only take a fresh, directional setup whose analysis gates all pass."""
    if not analysis or analysis.get("stale") or analysis.get("lean") not in ("CE", "PE"):
        return False
    checks = analysis.get("checks") or []
    return bool(checks) and all(c.get("st") == "ok" for c in checks)


def levels(spot, atr, side, rr):
    if not spot or not atr or atr <= 0 or side not in ("CE", "PE"):
        return None, None
    if side == "CE":
        return round(spot - atr, 2), round(spot + rr * atr, 2)
    return round(spot + atr, 2), round(spot - rr * atr, 2)


class StockOptionsBot:
    def __init__(self, broker, risk, live, log, trade_writer):
        self.broker, self.risk, self.live = broker, risk, live
        self.log, self.trade_writer = log, trade_writer
        self.position = self._load_position()
        self.traded = self._load_traded()
        self.candidate = None
        self.scanning = False
        self.last_scan = 0.0
        self.last_error = ""
        self.last_error_at = 0.0
        self._scan_lock = threading.Lock()
        self._instrument_cache = None
        self._instrument_cache_at = 0.0
        self._command = self._read_json(COMMAND_FILE, {})
        if self._command.get("status") == "processing":
            self._command.update(status="blocked",
                                 result="Bot restarted during this action; check Zerodha before retrying.")
            self._write_json(COMMAND_FILE, self._command)
        self._reconciled = not live
        if live:
            self._reconcile()

    @staticmethod
    def _read_json(path, default):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    @staticmethod
    def _write_json(path, data):
        with open(path + ".tmp", "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(path + ".tmp", path)

    def _load_position(self):
        data = self._read_json(POSITIONS_FILE, {})
        if data.get("mode") != config.MODE:
            return None
        if not self.live and data.get("date") != datetime.now(IST).date().isoformat():
            return None
        raw = data.get("position")
        if not raw:
            return None
        try:
            raw["t"] = datetime.fromisoformat(raw["t"])
            return raw
        except (KeyError, TypeError, ValueError):
            self.log("Stock-option state is invalid; refusing new stock entries.")
            self.risk.lock("stock-option state needs manual reconciliation")
            return None

    def _load_traded(self):
        data = self._read_json(POSITIONS_FILE, {})
        if data.get("date") != datetime.now(IST).date().isoformat() or data.get("mode") != config.MODE:
            return set()
        return set(data.get("traded") or [])

    def _save(self):
        self._write_json(POSITIONS_FILE, dict(
            date=datetime.now(IST).date().isoformat(), mode=config.MODE, position=self.position,
            traded=sorted(self.traded)))

    def _reconcile(self):
        try:
            rows = self.broker.kite.positions().get("net", [])
            orders = self.broker.kite.orders()
        except Exception as e:
            self.last_error = f"Could not reconcile stock-option positions: {e}"
            self.risk.lock("stock-option positions could not be reconciled")
            return
        pending = [o for o in orders if o.get("exchange") == "NFO"
                   and self._is_stock_option(o.get("tradingsymbol", ""))
                   and o.get("status") not in ("COMPLETE", "REJECTED", "CANCELLED")
                   and (not self.position or str(o.get("order_id")) != str(self.position.get("sl_id")))]
        if pending:
            self.last_error = "Found a pending stock-option order; check Zerodha before restarting."
            self.risk.lock("pending stock-option order needs manual reconciliation")
            return
        if self.position and self.position.get("stop_uncertain"):
            self.last_error = f"Protective stop outcome for {self.position['sym']} is uncertain; check Zerodha manually."
            self.risk.lock("stock-option protective stop needs manual reconciliation")
            return
        if self.position:
            row = next((r for r in rows if r.get("tradingsymbol") == self.position["sym"]
                        and r.get("exchange") == "NFO" and r.get("product") == "MIS"), None)
            if not row or int(row.get("quantity") or 0) != int(self.position["qty"]):
                self.last_error = f"Position mismatch for {self.position['sym']}; check Zerodha before restarting."
                self.risk.lock("stock-option position needs manual reconciliation")
                return
        elif any(self._is_stock_option(r.get("tradingsymbol", "")) and int(r.get("quantity") or 0) != 0
                 for r in rows if r.get("exchange") == "NFO"):
            self.last_error = "Found an unmanaged Nifty-50 stock-option position; check Zerodha before restarting."
            self.risk.lock("unmanaged stock-option position")
            return
        self._reconciled = True

    @staticmethod
    def _is_stock_option(symbol):
        m = re.match(r"(.+?)\d", symbol)    # the underlying is everything before the expiry digits
        return bool(m) and m.group(1) in stocks.BY_SYM

    def _market_open(self, now):
        return now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 30)

    def _entry_window(self, now):
        start = datetime.strptime(config.NO_ENTRY_BEFORE, "%H:%M").time()
        end = datetime.strptime(config.NO_ENTRY_AFTER, "%H:%M").time()
        return self._market_open(now) and start <= now.time() <= end

    def _scan_async(self):
        if self.scanning or time.time() - self.last_scan < SCAN_SECONDS:
            return
        self.scanning = True

        def scan():
            from concurrent.futures import ThreadPoolExecutor, as_completed

            ready = []
            errors = []
            try:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    settings = {k: v for k, v in vars(config).items() if k.isupper()}
                    jobs = {pool.submit(stocks.analyse, sym, settings): sym for sym, *_ in stocks.TOP15}
                    for future in as_completed(jobs):
                        sym = jobs[future]
                        try:
                            analysis = future.result()
                            if signal_ready(analysis) and sym not in self.traded:
                                ready.append((abs(float(analysis.get("score") or 0)), sym, analysis))
                        except Exception as e:
                            errors.append(f"{sym}: {str(e)[:100]}")
                ready.sort(reverse=True, key=lambda x: x[0])
                with self._scan_lock:
                    self.candidate = (ready[0][1], ready[0][2]) if ready else None
                    self.last_scan = time.time()
                    self.last_error = "; ".join(errors[:3])
            finally:
                self.last_scan = time.time()
                self.scanning = False

        threading.Thread(target=scan, name="stock-option-scan", daemon=True).start()

    def _quote(self, key):
        result = self.broker.kite.quote([key]).get(key)
        if not result or not result.get("last_price"):
            raise RuntimeError(f"No live quote for {key}")
        stamp = result.get("timestamp") or result.get("last_trade_time")
        if isinstance(stamp, str):
            try:
                stamp = datetime.fromisoformat(stamp)
            except ValueError:
                stamp = None
        if stamp is not None:
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=IST)
            if abs(time.time() - stamp.timestamp()) > MAX_QUOTE_AGE:
                raise RuntimeError(f"Stale quote for {key}")
        elif self._market_open(datetime.now(IST)):
            raise RuntimeError(f"Quote for {key} has no exchange timestamp")
        return result

    def _spot(self, sym):
        if self.live:
            q = self._quote(f"NSE:{sym}")
            return float(q["last_price"])
        d = market.chart(stocks.yahoo(sym), "1m", "1d", max_age=10)
        age = time.time() - d["t"][-1]
        if d.get("stale") or (self._market_open(datetime.now(IST)) and age > 300):
            raise RuntimeError(f"Stale Yahoo price for {sym}")
        return float(d["c"][-1])

    def _instruments(self):
        if self._instrument_cache and time.time() - self._instrument_cache_at < 3600:
            return self._instrument_cache
        self._instrument_cache = self.broker.kite.instruments("NFO")
        self._instrument_cache_at = time.time()
        return self._instrument_cache

    def _contract(self, sym, side, spot):
        step = stocks.strike_step(spot)
        wanted = strategy.itm_strike(spot, side, step, config.ITM_STRIKES)
        today = datetime.now(IST).date()
        rows = [i for i in self._instruments()
                if i.get("name") == sym and i.get("instrument_type") == side and i.get("expiry") >= today]
        if not rows:
            return None
        expiry = min(i["expiry"] for i in rows)
        rows = [i for i in rows if i["expiry"] == expiry]
        if side == "CE":
            itm = [i for i in rows if float(i["strike"]) <= spot]
        else:
            itm = [i for i in rows if float(i["strike"]) >= spot]
        if not itm:
            return None
        return min(itm, key=lambda i: abs(float(i["strike"]) - wanted))

    @staticmethod
    def _book_price(quote, book_side, fallback):
        levels_ = ((quote.get("depth") or {}).get(book_side) or [])
        return float(levels_[0]["price"]) if levels_ and levels_[0].get("price") else float(fallback)

    def _option_quote(self, sym):
        q = self._quote(f"NFO:{sym}")
        premium = float(q["last_price"])
        ask = self._book_price(q, "sell", premium)
        bid = self._book_price(q, "buy", premium)
        if ask <= 0 or bid <= 0 or (ask - bid) / ask > 0.15:
            raise RuntimeError(f"Option spread is too wide for {sym}")
        return q, ask, bid

    def _analysis(self, sym):
        settings = {k: v for k, v in vars(config).items() if k.isupper()}
        analysis = stocks.analyse(sym, settings)
        if not signal_ready(analysis):
            raise RuntimeError("Stock analysis has no fresh, fully-approved directional setup")
        return analysis

    def _enter(self, sym, analysis, index_positions, now):
        if self.position:
            raise RuntimeError("A stock-option position is already open")
        if not self._reconciled:
            raise RuntimeError(self.last_error or "Stock-option positions need reconciliation")
        if sym in self.traded:
            raise RuntimeError(f"{sym} already had a stock-option attempt today")
        if not self._entry_window(now):
            raise RuntimeError("Outside the configured stock-option entry window")
        if not self.risk.can_trade() or self.risk.s["trades"] >= config.MAX_TRADES_PER_DAY:
            raise RuntimeError(self.risk.s.get("reason") or "Daily risk/trade limit reached")
        if len(index_positions) >= config.MAX_OPEN_POSITIONS:
            raise RuntimeError("Maximum open positions already reached")

        analysis = self._analysis(sym)
        side = analysis["lean"]
        spot = self._spot(sym)
        atr = float(analysis["technical"].get("atr") or 0)
        sl, target = levels(spot, atr, side, config.RR)
        if sl is None:
            raise RuntimeError("Stock ATR is unavailable; no safe stop can be defined")

        if self.live:
            inst = self._contract(sym, side, spot)
            if not inst:
                raise RuntimeError(f"No current ITM {side} contract for {sym}")
            option = inst["tradingsymbol"]
            lot = int(inst["lot_size"])
            expiry = inst["expiry"]
            _, premium, _ = self._option_quote(option)
        else:
            item = stocks.BY_SYM[sym]
            lot = item[3]
            expiry_dt = stocks.monthly_expiry(now)
            expiry = expiry_dt.date()
            strike = strategy.itm_strike(spot, side, stocks.strike_step(spot), config.ITM_STRIKES)
            option = f"{sym}{expiry:%y%m%d}{int(strike)}{side}"
            years = (expiry_dt - now).total_seconds() / (365 * 24 * 3600)
            iv = max(0.15, float(analysis["technical"].get("iv") or 25) / 100)
            from feed import bs_price
            premium = max(0.05, round(bs_price(spot, strike, years, iv, side) / 0.05) * 0.05)

        in_use = sum(float(p["entry"]) * int(p["qty"]) for p in index_positions.values())
        order_price = premium * 1.01 if self.live else premium
        lots, reason = self.broker.lots_to_buy(order_price, lot, config.LOTS, in_use=in_use)
        if not lots:
            raise RuntimeError(reason or "Available funds do not cover one lot")
        if self.live and order_price - risk_manager.ITM_DELTA * atr <= 0.5:
            raise RuntimeError("The estimated ATR stop is too close to the option's minimum valid stop price")
        sig = dict(side=side, sl=sl, target=target, risk=atr, why=f"{analysis['label']} stock setup")
        sized = risk_manager.size_for_limits(sig, lots, lot, order_price, self.risk.s, config)
        cut = f" — lots cut from {lots} to {sized} to fit the risk limits" if sized < lots else ""
        qty = sized * lot
        reports = pipeline.reports(config)
        reports["technical"] = analysis["technical"]
        reports["fundamental"] = analysis["fundamental"]
        discussion = debate.run(reports, side)
        decision = trader.decide(sig, discussion, config)
        if config.USE_TRADER_AGENT and decision["action"] == "HOLD":
            raise RuntimeError(f"Trader agent HOLD: {decision['reason']}")
        review = risk_manager.review(sig, qty, order_price, self.risk.s,
                                     len(index_positions), reports.get("crude"),
                                     reports.get("news"), analysis.get("fundamental"),
                                     config, reports.get("global"))
        if not review["approved"]:
            raise RuntimeError("Risk manager veto: " + review["reason"])

        self.traded.add(sym)
        self._save()
        try:
            fill = self.broker.buy("NFO", option, qty, premium)
        except Exception as e:
            self.risk.lock(f"stock-option entry outcome is uncertain for {option}; check Zerodha")
            self.last_error = f"Entry outcome is uncertain for {option}: {e}"
            raise RuntimeError(self.last_error) from e
        if not fill:
            raise RuntimeError(f"Entry order was not filled ({fill.status})")
        if fill.status not in ("COMPLETE", "REJECTED", "CANCELLED"):
            self.risk.lock(f"stock-option buy order {fill.oid} is unresolved")
            self.last_error = f"Entry order {fill.oid} is unresolved; check Zerodha before further trading"
            self._reconciled = False
            raise RuntimeError(f"Entry order {fill.oid} is unresolved; check Zerodha")

        self.position = dict(symbol=sym, sym=option, key=f"NFO:{option}", exchange="NFO",
                             qty=fill.qty, entry=fill.price, side=side, t=now,
                             idx_entry=spot, sl=sl, sl0=sl, target=target, risk=atr,
                             dstop=fill.price - risk_manager.ITM_DELTA * atr,
                             sl_id=None, buy_oid=fill.oid, lot=lot, why=analysis["label"],
                             expiry=str(expiry), iv=analysis["technical"].get("iv"),
                             strike=float(inst["strike"]) if self.live else float(strike))
        self._save()
        self._ensure_stop()
        if self.live and not self.position.get("sl_id"):
            if self.position.get("stop_uncertain"):
                raise RuntimeError(self.last_error or "Protective stop outcome is uncertain; check Zerodha manually.")
            self.risk.lock(f"protective exchange stop for {option} could not be placed")
            self._close("protective stop unavailable", spot, self.broker.ltp(self.position["key"]))
            raise RuntimeError("Protective exchange stop could not be placed; the bot attempted to exit")
        self.log(f"STOCK OPTION ENTRY {side} {option} x{fill.qty} @ {fill.price:.2f} | "
                 f"{sym} {spot:.2f} SL {sl:.2f} TGT {target:.2f} | "
                 f"debate {discussion['margin']:+.2f}, trader {decision['action']}{cut}")
        notify.entry(config, self.log, bot="Stock options", mode="live" if self.live else "paper", side=side,
                     symbol=option, qty=fill.qty, price=fill.price, index=sym, spot=spot, sl=sl, target=target,
                     rr=abs(target - spot) / abs(spot - sl) if spot != sl else None, strategy="Stock option setup",
                     extra=[("Agents", f"trader {decision['action']} · debate {discussion['margin']:+.2f}{cut}")],
                     at=datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %H:%M IST"))

    def _ensure_stop(self):
        p = self.position
        if (not self.live or not p or p.get("sl_id") or p.get("stop_uncertain")
                or time.time() - p.get("sl_try", 0) < 30):
            return
        p["sl_try"] = time.time()
        if p["dstop"] <= 0.5:
            self.risk.lock(f"stock-option stop for {p['sym']} is invalid")
            self.last_error = "Could not set a valid exchange-side stop."
            return
        try:
            sl_id = self.broker.disaster_stop(p["exchange"], p["sym"], p["qty"], p["dstop"])
            if not sl_id:
                self.last_error = f"Exchange stop for {p['sym']} was not placed; retrying."
                self.risk.lock(f"protective exchange stop for {p['sym']} was not placed")
                self._save()
                return
            p["sl_id"] = sl_id
            status = self.broker.order_state(sl_id)["status"]
            if status in ("REJECTED", "CANCELLED"):
                p["sl_id"] = None
                self.last_error = f"Exchange stop for {p['sym']} was rejected; retrying."
                self.risk.lock(f"protective exchange stop for {p['sym']} was rejected")
            elif status not in ("COMPLETE", "OPEN", "TRIGGER PENDING"):
                self._mark_stop_uncertain(p, f"unexpected order status {status}")
            else:
                self.log(f"Exchange stop placed for {p['sym']} (trigger {p['dstop']:.2f})")
        except Exception as e:
            self._mark_stop_uncertain(p, str(e))
        self._save()

    def _mark_stop_uncertain(self, position, reason):
        position["stop_uncertain"] = True
        self._reconciled = False
        self.risk.lock(f"protective stop outcome for {position['sym']} is uncertain; check Zerodha manually")
        self.last_error = (f"Exchange stop outcome for {position['sym']} is uncertain; "
                           f"check Zerodha manually: {reason}")
        self.log(self.last_error)

    def _close(self, reason, spot=None, option_price=None):
        p = self.position
        if not p:
            return False
        if p.get("stop_uncertain"):
            self.last_error = f"Refusing an automated exit for {p['sym']} until its stop is reconciled in Zerodha."
            return False
        try:
            spot = float(spot if spot is not None else self._spot(p["symbol"]))
            if option_price is None:
                if self.live:
                    q = self._quote(p["key"])
                    option_price = self._book_price(q, "buy", q["last_price"])
                else:
                    option_price = self._paper_option(p, spot)
            option_price = float(option_price)
            f = self.broker.sell(p["exchange"], p["sym"], p["qty"], option_price, p.get("sl_id"))
        except Exception as e:
            self.last_error = f"Could not close {p['sym']}: {e}"
            self.log(self.last_error)
            return False
        if f.status != "STOP_STILL_OPEN":
            p["sl_id"] = None
            p["sl_try"] = 0
        if f.qty:
            p["sold"] = p.get("sold", 0) + f.qty
            p["sold_value"] = p.get("sold_value", 0.0) + f.qty * f.price
            p["qty"] -= f.qty
        if p["qty"] > 0:
            self._save()
            self._ensure_stop()
            self.last_error = f"Exit incomplete: {p['qty']} units of {p['sym']} remain."
            return False
        exit_price = p["sold_value"] / p["sold"] if p.get("sold") else option_price
        gross = (exit_price - p["entry"]) * (p.get("sold") or 0)
        charges = strategy.charges(p["entry"], exit_price, p.get("sold") or 0, p["exchange"])
        net = gross - charges
        self._finish(reason, spot, exit_price, gross, charges, net)
        return True

    def _finish(self, reason, spot, exit_price, gross, charges, net):
        p = self.position
        if not p:
            return
        if p.get("stop_uncertain"):
            self.last_error = f"Protective stop for {p['sym']} needs manual reconciliation in Zerodha."
            return
        self.trade_writer(dict(date=datetime.now(IST).date(), mode=config.MODE, symbol=p["sym"], qty=p.get("sold", 0),
                              entry_time=p["t"].strftime("%H:%M"), entry=p["entry"],
                              exit_time=datetime.now(IST).strftime("%H:%M"), exit=round(exit_price, 2),
                              pnl=round(gross, 2), reason=reason, index=p["symbol"], side=p["side"],
                              idx_entry=round(p["idx_entry"], 2), idx_sl=round(p["sl0"], 2),
                              idx_target=round(p["target"], 2), idx_exit=round(spot, 2), why=p["why"],
                              charges=charges, net_pnl=round(net, 2)))
        self.log(f"STOCK OPTION EXIT {p['sym']} @ {exit_price:.2f} ({reason}; {p['symbol']} {spot:.2f}) "
                 f"net P&L ₹{net:.0f}")
        self.position = None
        self._save()
        self.risk.record(net)
        self.last_error = ""

    def _paper_option(self, p, spot):
        from feed import bs_price
        expiry = date.fromisoformat(p["expiry"])
        expiry_dt = datetime.combine(expiry, dtime(15, 30), tzinfo=IST)
        years = max(1 / (365 * 24 * 60), (expiry_dt - datetime.now(IST)).total_seconds() / (365 * 24 * 3600))
        strike = float(p.get("strike") or 0)
        if not strike:
            return float(p["entry"])
        iv = max(0.15, float(p.get("iv") or 25) / 100)
        return max(0.05, round(bs_price(spot, strike, years, iv, p["side"]) / 0.05) * 0.05)

    def _manage(self, now):
        p = self.position
        if not p:
            return
        try:
            spot = self._spot(p["symbol"])
            if self.live:
                q = self._quote(p["key"])
                option = self._book_price(q, "buy", q["last_price"])
            else:
                option = self._paper_option(p, spot)
            p["ltp"], p["spot"] = option, spot
            if self.live:
                stopped = self.broker.sl_filled(p.get("sl_id"))
                if stopped:
                    p["sl_id"] = None
                    p["sl_try"] = 0
                    p["sold"] = p.get("sold", 0) + stopped.qty
                    p["sold_value"] = p.get("sold_value", 0.0) + stopped.qty * stopped.price
                    p["qty"] -= stopped.qty
                    if p["qty"] <= 0:
                        exit_price = p["sold_value"] / p["sold"]
                        gross = (exit_price - p["entry"]) * p["sold"]
                        charges = strategy.charges(p["entry"], exit_price, p["sold"], p["exchange"])
                        self._finish("exchange stop", spot, exit_price, gross, charges, gross - charges)
                        return
                    self._save()
                    self._close("exchange stop (partial)", spot, option)
                    return
            if now.time() >= datetime.strptime(config.SQUARE_OFF_AT, "%H:%M").time():
                self._close("square-off time", spot, option)
            elif (p["side"] == "CE" and spot <= p["sl"]) or (p["side"] == "PE" and spot >= p["sl"]):
                self._close("ATR stop", spot, option)
            elif (p["side"] == "CE" and spot >= p["target"]) or (p["side"] == "PE" and spot <= p["target"]):
                self._close("risk/reward target", spot, option)
            else:
                self._ensure_stop()
        except Exception as e:
            self.last_error = f"Stock-option monitor is waiting for fresh prices: {e}"
            if time.time() - self.last_error_at > 60:
                self.log(self.last_error)
                self.last_error_at = time.time()

    def _process_command(self, now, index_positions):
        cmd = self._read_json(COMMAND_FILE, {})
        if cmd.get("status") != "pending":
            self._command = cmd
            return
        cmd.update(status="processing", started=now.strftime("%Y-%m-%d %H:%M:%S"))
        self._write_json(COMMAND_FILE, cmd)
        self._command = cmd
        try:
            action, sym = cmd.get("action"), cmd.get("symbol")
            if not self._reconciled:
                raise RuntimeError(self.last_error or "Stock-option positions need reconciliation")
            if action == "buy" and sym in stocks.BY_SYM:
                if not config.USE_STOCK_OPTIONS:
                    raise RuntimeError("Enable Auto-trade stock options in Settings first")
                self._enter(sym, None, index_positions, now)
                result = f"Entry accepted for {sym}; risk checks passed."
            elif action == "sell" and self.position and self.position["symbol"] == sym:
                if not self._close("manual exit", self._spot(sym)):
                    raise RuntimeError(self.last_error or "Exit is still pending")
                result = f"Exit completed for {sym}."
            else:
                raise RuntimeError("No matching open position or valid stock action.")
            cmd.update(status="done", result=result)
        except Exception as e:
            cmd.update(status="blocked", result=str(e)[:200])
        cmd["finished"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        self._write_json(COMMAND_FILE, cmd)
        self._command = cmd

    def tick(self, now, index_positions):
        self._process_command(now, index_positions)
        if self.position:
            self._manage(now)
        if not config.USE_STOCK_OPTIONS or not self._entry_window(now) or self.position:
            return
        if not self._reconciled or not self.risk.can_trade():
            return
        self._scan_async()
        if len(index_positions) >= config.MAX_OPEN_POSITIONS:
            return
        with self._scan_lock:
            candidate = self.candidate
        if candidate:
            sym, analysis = candidate
            try:
                self._enter(sym, analysis, index_positions, now)
                with self._scan_lock:
                    self.candidate = None
            except Exception as e:
                self.last_error = str(e)[:200]
                self.log(f"Stock-option entry skipped for {sym}: {self.last_error}")
                with self._scan_lock:
                    self.candidate = None

    def close_all(self, reason, now):
        if self.position:
            try:
                if not self._close(reason, self._spot(self.position["symbol"])):
                    self.log(self.last_error or f"Could not close stock option during {reason}.")
            except Exception as e:
                self.last_error = f"Could not close stock option during {reason}: {e}"
                self.log(self.last_error)

    def snapshot(self):
        p = self.position
        position = None
        if p:
            position = {k: p.get(k) for k in ("symbol", "sym", "side", "qty", "entry", "sl", "target", "t", "ltp", "spot")}
            position.update(opt_sl=round(p["dstop"], 2),  # the exchange stop sits at the option's estimated price at the ATR stop
                            opt_target=round(p["entry"] + risk_manager.ITM_DELTA * abs(p["target"] - p["idx_entry"]), 2))
        return dict(enabled=bool(config.USE_STOCK_OPTIONS), live=self.live, scanning=self.scanning,
                    last_scan=self.last_scan, candidate=self.candidate[0] if self.candidate else None,
                    position=position, command=self._command, error=self.last_error,
                    reconciled=self._reconciled)
