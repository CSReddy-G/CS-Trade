"""
Live index prices streamed from Zerodha's WebSocket (KiteTicker), shared by every panel user.

When any panel user is logged in to Zerodha today, one WebSocket subscribes to Nifty, Sensex, Bank Nifty
and India VIX and keeps the latest tick of each in memory. The panel's /api/stream pushes those ticks to
browsers as they arrive, and live_indices() in app.py reads them before trying Kite quotes or Yahoo.

Twisted's reactor (which KiteTicker runs on) can only be started once per process, so a reconnect
(new day's token, a different user) closes the old socket and opens a new one on the same reactor;
the reactor itself is never stopped.
"""
import logging, threading, time

TOKENS = {256265: "nifty", 265: "sensex", 260105: "banknifty", 264969: "vix"}
RETRY_AFTER = 60          # seconds before trying again after a refused / failed connection

log = logging.getLogger("kitebot.live")


def _on_reactor(fn):
    """Run fn on the reactor thread once it is running. Called from another thread, Twisted doesn't wake up
    for new work, so a new login's connection could sit unstarted, with no error, until something else woke it."""
    from twisted.internet import reactor
    if reactor.running:
        reactor.callFromThread(fn)
    else:
        fn()


class Stream:
    def __init__(self, creds, tokens=None, on_tick=None):
        self.creds = creds                    # () -> (uid, api_key, access_token) or None
        self.tokens = tokens or TOKENS        # instrument token -> key
        self.on_tick = on_tick                # optional (key, price, received_at) for every tick, on the reactor thread
        self.ticks = {}                       # key -> dict(price, prev_close, high, low, as_of)
        self.version = 0
        self.cond = threading.Condition()
        self.kt = None
        self.who = None                       # (uid, access_token) the socket was opened with
        self.connected = False
        self.error = None
        self.next_try = 0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- connection
    def ensure(self):
        """Open, keep or replace the socket so it matches the current Zerodha login. Cheap to call often."""
        with self._lock:
            c = self.creds()
            if not c:
                if self.kt:
                    self._close("no panel user is logged in to Zerodha today")
                return
            uid, key, token = c
            if self.kt and self.who == (uid, token) and (self.connected or time.time() < self.next_try):
                return
            if time.time() < self.next_try:
                return
            if self.kt:
                self._close(None)
            self._open(uid, key, token)

    def _open(self, uid, key, token):
        from kiteconnect import KiteTicker
        kt = KiteTicker(key, token, reconnect=True, reconnect_max_tries=50, reconnect_max_delay=30)
        kt.on_connect = self._on_connect
        kt.on_ticks = self._on_ticks
        kt.on_close = self._on_close
        kt.on_error = self._on_error
        kt.on_noreconnect = lambda ws: self._fail("Zerodha stream gave up reconnecting")
        self.kt, self.who, self.error = kt, (uid, token), None
        self.next_try = time.time() + RETRY_AFTER     # don't hammer it while this attempt is in flight

        def start():
            try:
                kt.connect(threaded=True)
            except Exception as e:
                self._fail(f"couldn't start the Zerodha stream: {str(e)[:120]}")
        _on_reactor(start)

    def _close(self, why):
        kt, self.kt, self.who, self.connected = self.kt, None, None, False
        if why:
            self.error = why
        if kt:
            def stop():
                try:
                    kt.close()
                except Exception:
                    pass
            _on_reactor(stop)

    def _fail(self, msg):
        self.connected, self.error, self.next_try = False, msg, time.time() + RETRY_AFTER
        log.warning(msg)
        if self.kt:
            try:
                self.kt.stop_retry()
            except Exception:
                pass

    # ---------------------------------------------------------------- KiteTicker callbacks (reactor thread)
    def _on_connect(self, ws, response):
        self.connected, self.error = True, None
        toks = list(self.tokens)
        ws.subscribe(toks)
        ws.set_mode(ws.MODE_QUOTE, toks)

    def _on_ticks(self, ws, ticks):
        now = time.time()
        with self.cond:
            for t in ticks:
                key = self.tokens.get(t.get("instrument_token"))
                if not key or not t.get("last_price"):
                    continue
                if self.on_tick:
                    self.on_tick(key, t["last_price"], now)
                o = t.get("ohlc") or {}
                self.ticks[key] = dict(price=t["last_price"], prev_close=o.get("close") or None,
                                       high=o.get("high") or None, low=o.get("low") or None, as_of=now)
            self.version += 1
            self.cond.notify_all()

    def _on_close(self, ws, code, reason):
        self.connected = False
        if code and code != 1000 and not self.error:     # keep the more specific error from _on_error
            self.error = f"Zerodha stream closed ({code}{': ' + reason if reason else ''})"

    def _on_error(self, ws, code, reason):
        # 403 = token expired / app not allowed to stream; retrying won't help until the login changes
        msg = f"Zerodha stream error {code}: {reason}" if code else f"Zerodha stream error: {reason}"
        if code == 403 or "403" in str(reason):
            self._fail(msg + " (log in to Zerodha again; your Kite Connect plan must include live market data)")
        else:
            self.connected, self.error = False, msg

    # ---------------------------------------------------------------- readers
    def latest(self):
        """{key: tick} when the socket is live, else {}."""
        return dict(self.ticks) if self.connected else {}

    def wait(self, version, timeout):
        """Block until a tick newer than `version` arrives (or timeout). Returns the current version."""
        with self.cond:
            if self.version == version:
                self.cond.wait(timeout)
            return self.version

    def status(self):
        return dict(streaming=self.connected, error=None if self.connected else self.error,
                    user=self.who[0] if self.who else None)
