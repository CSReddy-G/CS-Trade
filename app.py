"""
Kite Bot control panel — http://127.0.0.1:8765 on this computer. Phones on the same Wi-Fi
can open the phone link printed at startup; everyone signs in with their own user and password.

Several people can be signed in at once (accounts.py). Each has their own settings, Zerodha login,
bot, scalper, trades and logs in users/<id>/; the market agents are shared.

    python app.py        (start.command does this for you)
"""
import csv, ipaddress, json, os, secrets, signal, socket, subprocess, sys, threading, time, urllib.parse, webbrowser
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, Response, g, has_request_context, jsonify, redirect, request, send_from_directory

import accounts
import manual_exit
import manual_levels
import notify
import strategies

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
IST = ZoneInfo("Asia/Kolkata")
PORT = int(os.environ.get("KITEBOT_PORT", 8765))
# Where people open the panel; on a server behind HTTPS, e.g. https://kitebot.example.com (Kite's redirect URL uses it)
PUBLIC_URL = os.environ.get("KITEBOT_PUBLIC_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
SETTINGS = "settings.json"
TOKEN_META = "token_meta.json"

app = Flask(__name__, static_folder=None)
LOCAL_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # UDP connect sends nothing; it just picks the Wi-Fi address
            return s.getsockname()[0]
    except OSError:
        return None


# Extra hostnames you trust, e.g. KITEBOT_ALLOWED_HOSTS="kitebot.home.lan,myserver.example.com"
EXTRA_HOSTS = {h.strip() for h in os.environ.get("KITEBOT_ALLOWED_HOSTS", "").split(",") if h.strip()}


def allowed_host(host):
    """localhost, a bare IP address on our port, or a name listed in KITEBOT_ALLOWED_HOSTS.
    Other domain names are refused (DNS rebinding)."""
    if host in LOCAL_HOSTS or host in EXTRA_HOSTS or host.rpartition(":")[0] in EXTRA_HOSTS:
        return True
    name, _, port = host.rpartition(":")
    try:
        ipaddress.ip_address(name.strip("[]"))
    except ValueError:
        return False
    return port == str(PORT)


@app.before_request
def only_this_computer():
    """Any web page open in your browser can send requests to 127.0.0.1. Refuse anything that
    isn't this control panel: wrong Host (DNS rebinding), a foreign Origin, or a POST that isn't
    JSON (a plain-form/text POST skips the browser's CORS check). Every API call also needs a
    signed-in panel user."""
    if not allowed_host(request.host):
        return jsonify(ok=False, error="forbidden host"), 403
    origin = request.headers.get("Origin")
    if origin and origin.split("://", 1)[-1] != request.host:   # http locally, https behind the server's proxy
        return jsonify(ok=False, error="forbidden origin"), 403
    if request.method == "POST" and not request.is_json:
        return jsonify(ok=False, error="JSON body required"), 415
    # Signed-in panel user: the page keeps its session token in sessionStorage and sends it on every call.
    if request.path.startswith("/api/") and request.path != "/api/auth/login":
        uid = accounts.session_user(request.headers.get("X-Session", ""))
        if not uid:
            return jsonify(ok=False, error="Please sign in.", auth=True), 401
        g.user = uid


accounts.setup()
procs = {}                             # (user id, "bot" | "scalp") -> Popen
JOB = {"running": False, "output": "", "started": None, "finished": None}
bts, sbts = {}, {}                     # user id -> that user's backtest / scalp backtest job

# ------------------------------------------------------------------ settings schema
# Sections appear on the Settings page in this order.
SECTIONS = [
    ("account", "Account & price feed", "Where prices come from, and your Zerodha app keys. Paper trading on the free feed needs no keys."),
    ("trade", "What to trade", "Which index the main bot trades, how big, and how deep in the money."),
    ("risk", "Risk limits", "Hard limits for the main bot. When one is hit, it stops trading for the rest of the day."),
    ("timing", "Trading hours", "When the main bot may open trades, and when it closes everything. India time."),
    ("exits", "Stop-loss & target", "How the main bot gets out of a trade. Stops and targets are measured on the index."),
    ("strategies", "Strategies", "Pick one or more. Whichever fires first takes the position, and that trade keeps its own strategy's exits. If two fire on the same candle, the one listed first goes first."),
    ("entry", "EMA cross rules", "When an EMA cross becomes a trade. The defaults are the tested values; change them only with a reason."),
    ("fvg", "FVG rules", "Fair Value Gap retest rules. The EMA rules, volume filter and VIX-scaled stops don't apply to FVG trades."),
    ("ob", "Order Block rules", "Sweep, structure shift, order block, retest, rejection, confirmation. The EMA rules, volume filter and VIX-scaled stops don't apply to these trades. The score is a checklist, not a chance of winning."),
    ("vwap", "VWAP + Volume rules", "Pullback to VWAP or EMA 20 in a VWAP trend, confirmed by a strong candle on high volume. Index candles only; volume is the NIFTYBEES proxy. Paper first. The confidence score is a checklist, not a chance of winning."),
    ("orb", "Opening Range Breakout rules", "The 09:15–09:30 range, a volume breakout, a retest that holds, then confirmation. Nothing trades before the range is final. Paper first."),
    ("lsmss", "Liquidity Sweep + MSS rules", "Sweep of swing or equal highs/lows, a structure shift on a displacement candle, then a retrace into its FVG. 15-min swing structure must not oppose. Paper first."),
    ("supertrend", "Supertrend + ADX rules", "Supertrend direction, ADX at least 25 with +DI/−DI agreeing, EMA 50 and RSI, entered on a pullback and a confirmation candle. Paper first."),
    ("tc", "Trend Continuation rules", "After the first hour, the first close beyond the 09:15–09:45 range on the VWAP side; one trade per index per day. Chosen on 24 Jul – 29 Sep data and tested once on the next 30 sessions (reports/strategy_review/RESULTS.md). Option prices in those tests are estimates. Paper first."),
    ("agents", "Agent team", "Market analysts that can block a trade. They never start one."),
    ("scalper", "Scalper", "The separate 1-minute scalping bot (Scalper page). It has its own limits."),
    ("alerts", "Trade alerts", "A WhatsApp and/or Telegram message each time the main bot, the scalper or stock options enter a trade."),
]


def F(key, section, typ, label, help="", unit="", lo=None, hi=None, adv=False, choices=None, dep=None, danger=False, on=None):
    """on: for a number where 0 = off, the value its on/off switch fills in when turned on (if the default is 0)."""
    return dict(key=key, section=section, type=typ, label=label, help=help, unit=unit, min=lo, max=hi,
                advanced=adv, choices=choices, dep=dep, danger=danger, off0="0 = off" in help, on=on)


SCHEMA = [
    F("STRATEGIES", "strategies", "multi", "Active strategies", "At least one. Run a backtest after changing the mix.",
      choices=[(x.id, f"{x.name} · {x.tag}") for x in strategies.ALL]),
    F("PAPER_FEED", "account", "select", "Paper trading prices",
      "Free uses Yahoo index prices (about a minute behind) and estimated option prices. Zerodha uses real option prices and needs a daily login.",
      choices=[("free", "Free — no keys needed"), ("zerodha", "Zerodha — real option prices")]),
    F("API_KEY", "account", "text", "Zerodha API key", "From your app at developers.kite.trade. Needed for live trading or Zerodha prices."),
    F("API_SECRET", "account", "secret", "Zerodha API secret", "Stored only on this Mac. Leave blank to keep the saved one."),

    F("TRADE_INSTRUMENTS", "trade", "select", "Index", "Nifty and Sensex move together, so trading both mostly doubles the same bet.",
      choices=[("NIFTY", "Nifty 50"), ("SENSEX", "Sensex"), ("BOTH", "Both (one position at a time)")]),
    F("BOTH_SAME_SIGNAL", "trade", "select", "Same call on both indices",
      "Applies when Index is Both and Nifty and Sensex fire the same way on the same candle. A single lot can't be halved, so with half quantity at 1 lot only Sensex is traded.",
      choices=[("sensex", "Sensex only, skip Nifty"), ("split", "Half quantity on each"), ("off", "No special handling")]),
    F("USE_STOCK_OPTIONS", "trade", "bool", "Auto-trade stock options",
      "When the main bot is running, scan the 30 largest Nifty stocks. Long options only; uses the stock ATR stop, configured risk/reward target, premium cap, daily limits and square-off time. Paper mode by default; Live requires the existing typed confirmation."),
    F("LOTS", "trade", "int", "Max lots per trade", "The most the bot buys. The actual lots come from 90% of your Available to trade funds ÷ the cost of one lot (Nifty lot = 65 units, Sensex lot = 20). The trade is skipped if one lot doesn't fit.", "lots", 1, 50),
    F("PAPER_FUNDS", "trade", "float", "Paper funds", "Paper mode only: the funds it sizes trades against. Live mode uses your Zerodha Available to trade.", "₹", 0),
    F("ITM_STRIKES", "trade", "int", "Strikes in the money",
      "1 = at least one full strike in the money (Nifty ≥ 50 points, Sensex ≥ 100). Deeper costs more and moves more like the index — both ways.", "strikes", 1, 3),
    F("MAX_OPEN_POSITIONS", "trade", "int", "Open positions at once", "Keep at 1 when trading both indices.", "", 1, 2, adv=True),

    F("MAX_DAILY_LOSS", "risk", "float", "Max loss per day", "Stop for the day once losses reach this.", "₹", 0),
    F("MAX_TRADES_PER_DAY", "risk", "int", "Max trades per day", "Fewer, better trades usually beat more trades.", "trades", 1, 20),
    F("MAX_CONSECUTIVE_LOSSES", "risk", "int", "Stop after losses in a row", "Stop for the day after this many losing trades in a row.", "losses", 1, 10),
    F("MAX_PREMIUM_PER_TRADE", "risk", "float", "Max premium per trade", "Caps the money spent buying options in one trade. In-the-money options cost more.", "₹", 0),

    F("NO_ENTRY_BEFORE", "timing", "time", "First entry at", "Skips the noisy first 15 minutes after the 09:15 open."),
    F("NO_ENTRY_AFTER", "timing", "time", "Last entry at", "In testing, late entries were where most losses came from. 13:30 is the tested value."),
    F("SQUARE_OFF_AT", "timing", "time", "Close everything at", "15:20 at the latest. Zerodha auto-closes F&O intraday positions at 15:26 and charges ₹50 + GST per order."),

    F("RR", "exits", "float", "Target", "Target distance as a multiple of the stop distance. 2 means risk 1 to make 2.", "× stop", 0.5, 5),
    F("BREAKEVEN_AT_R", "exits", "float", "Move stop to cost at",
      "Once the trade gains this many times its risk (1 = 1:1), the stop moves to your entry plus charges, so it can't turn into a loss. 0 = off.", "× stop", 0, 5),
    F("BE_LOCK_PCT", "exits", "float", "Profit locked at 1:1",
      "When the stop moves at 1:1, lock this % of the risk above entry (never less than charges). Entry 180, stop 150: at 210 the stop goes to 187.5. 0 = just cost. Applies to the scalper too.", "% of stop", 0, 100),
    F("TRAIL_STEP_POINTS", "exits", "float", "Then trail every",
      "After 1:1, each time the price moves this many more points in your favour, the stop moves up the same points (210 → 213 lifts 187.5 → 190.5). 0 = off. Applies to the scalper too.", "pts", 0, 100),
    F("COST_STOP_ON_PREMIUM", "exits", "bool", "Cost stop on option price too",
      "Also exit if the option price falls back to cost. Stricter; it cut profit in testing because time decay triggers it early.", adv=True),
    F("TRAIL_START_R", "exits", "float", "Start trailing stop at", "After this profit, the stop follows the best price. Mixed results in testing. 0 = off.", "× stop", 0, 5, adv=True, on=1.5),
    F("TRAIL_R", "exits", "float", "Trailing distance", "How far behind the best price the trailing stop sits. 0 = off.", "× stop", 0, 5, adv=True),
    F("VIX_ADJUST_EXITS", "exits", "bool", "Scale stop & target with India VIX",
      "Stop and target are multiplied by √(VIX ÷ normal VIX): wider when the market is fearful, tighter when calm. Target stays the same multiple of the stop."),
    F("VIX_BASE", "exits", "float", "Normal India VIX", "At this VIX the stop is unchanged.", "", 8, 40, dep="VIX_ADJUST_EXITS", adv=True),
    F("VIX_MULT_MIN", "exits", "float", "Tightest VIX scaling", "", "×", 0.5, 1, dep="VIX_ADJUST_EXITS", adv=True),
    F("VIX_MULT_MAX", "exits", "float", "Widest VIX scaling", "A wider stop also means a bigger loss when it's hit; the risk manager checks it against today's loss budget.", "×", 1, 2, dep="VIX_ADJUST_EXITS", adv=True),
    F("TIME_STOP_MIN", "exits", "float", "Time stop", "Exit a trade that hasn't reached half its risk in profit after this long. 0 = off.", "min", 0, 360, adv=True, on=60),

    F("HTF_FILTER", "entry", "bool", "15-minute trend must agree", "Only trade when the 15-minute trend points the same way. Helped in testing."),
    F("ADX_MIN", "entry", "float", "Minimum trend strength (ADX)", "Skip choppy markets. 0 = off. 20 means fewer trades and smaller drawdowns, but lower profit in testing.", "", 0, 50, on=20),
    F("VOLUME_FILTER", "entry", "bool", "Volume must confirm the signal",
      "Indices have no volume, so the Nifty ETF (NIFTYBEES) is used: the signal candle must trade more than usual. If volume data is missing, the check is skipped."),
    F("VOLUME_MIN_RVOL", "entry", "float", "Volume needed", "Signal candle volume as a multiple of the average of the previous candles.", "× normal", 0.5, 5, dep="VOLUME_FILTER"),
    F("VOLUME_AVG_N", "entry", "int", "Volume average over", "", "candles", 5, 100, dep="VOLUME_FILTER", adv=True),
    F("MIN_ATR_PCT", "entry", "float", "Minimum volatility", "Skip dead markets where the 5-minute range is below this share of the index.", "%", 0, 1),
    F("EMA_FAST", "entry", "int", "Fast average", "Entry signal: the fast average crossing the slow one.", "candles", 2, 50, adv=True),
    F("EMA_SLOW", "entry", "int", "Slow average", "", "candles", 3, 100, adv=True),
    F("EMA_TREND", "entry", "int", "Trend average", "Trades only in the direction of this longer average.", "candles", 10, 200, adv=True),
    F("RSI_BUY_CE", "entry", "float", "Calls need RSI at least", "Momentum check for bullish trades.", "", 50, 80, adv=True),
    F("RSI_BUY_PE", "entry", "float", "Puts need RSI at most", "Momentum check for bearish trades.", "", 20, 50, adv=True),
    F("RSI_MAX", "entry", "float", "Skip exhausted moves beyond RSI", "Skips calls above this RSI and puts below 100 minus it.", "", 60, 95, adv=True),

    F("FVG_ENTRY_MODE", "fvg", "select", "Entry",
      "Aggressive: the candle that touches the gap. Balanced: price reaches the gap's midpoint and is rejected. Conservative: a confirmation candle closes in the trade's direction after a touch.",
      choices=[("balanced", "Balanced — midpoint rejection"), ("aggressive", "Aggressive — touch of the zone"),
               ("conservative", "Conservative — confirmation candle")]),
    F("FVG_SL_METHOD", "fvg", "select", "Stop-loss at", "Below a bullish gap (above a bearish one), the setup's swing low/high, or a fixed ATR distance.",
      choices=[("fvg", "Gap boundary"), ("swing", "Setup's swing low / high"), ("atr", "ATR distance")]),
    F("FVG_RR", "fvg", "float", "Target", "1, 1.5, 2 or 3 times the stop distance.", "× stop", 1, 3),
    F("FVG_BREAKEVEN_AT_R", "fvg", "float", "Move stop to cost at", "0 = off.", "× stop", 0, 5),
    F("FVG_TREND_MODE", "fvg", "select", "Trend (EMA 9 / 21 / 50)", "Bullish gaps prefer price above EMA 50 and EMA 9 above EMA 21; bearish the opposite.",
      choices=[("prefer", "Prefer — adds to the score"), ("require", "Require"), ("off", "Ignore")]),
    F("FVG_MIN_SCORE", "fvg", "int", "Minimum gap score", "Displacement 25, volume 15, trend 25, break of structure 20, liquidity sweep 15.", "/ 100", 0, 100),
    F("FVG_VOLUME_FILTER", "fvg", "bool", "Volume must confirm the displacement",
      "The middle candle's NIFTYBEES volume vs its 20-candle average. Skipped when volume data is missing."),
    F("FVG_VOLUME_MULT", "fvg", "float", "Volume needed", "", "× average", 0.5, 5, dep="FVG_VOLUME_FILTER"),
    F("FVG_REQUIRE_BOS", "fvg", "bool", "Require a break of structure", "The displacement must close through the last swing high (low). Off: it only adds to the score."),
    F("FVG_REQUIRE_SWEEP", "fvg", "bool", "Require a liquidity sweep", "Price must first take out the last swing low (high) and reclaim it. Off: it only adds to the score."),
    F("FVG_EXIT_ON_OPPOSITE", "fvg", "bool", "Exit on an opposite FVG signal"),
    F("FVG_ALLOW_LIVE", "fvg", "bool", "Allow live FVG trading (real money)", "Off by default: the FVG strategy refuses live mode until it has been paper-traded.", danger=True),
    F("FVG_DISP_BODY_ATR", "fvg", "float", "Displacement body at least", "Middle candle body vs ATR(14).", "× ATR", 0, 5, adv=True),
    F("FVG_DISP_BODY_PCT", "fvg", "float", "Displacement body share", "Body as a share of the candle's high-low range (0.6 = 60%).", "", 0, 1, adv=True),
    F("FVG_MIN_SIZE_ATR", "fvg", "float", "Smallest gap", "", "× ATR", 0, 5, adv=True),
    F("FVG_MAX_SIZE_ATR", "fvg", "float", "Largest gap", "", "× ATR", 0.1, 10, adv=True),
    F("FVG_MAX_AGE", "fvg", "int", "Gap stays tradeable for", "Gaps also expire at the end of the day.", "candles", 1, 75, adv=True),
    F("FVG_MAX_TOUCHES", "fvg", "int", "Visits allowed", "Separate returns into the gap before it counts as spent.", "", 1, 10, adv=True),
    F("FVG_SL_BUFFER_ATR", "fvg", "float", "Stop buffer", "Extra room beyond the gap boundary or swing.", "× ATR", 0, 2, adv=True),
    F("FVG_SL_ATR_MULT", "fvg", "float", "ATR stop distance", "Used when the stop is 'ATR distance'.", "× ATR", 0.2, 5, adv=True),
    F("FVG_MAX_RISK_ATR", "fvg", "float", "Skip if the stop is wider than", "", "× ATR", 0.5, 10, adv=True),
    F("FVG_TRAIL_START_R", "fvg", "float", "Start trailing stop at", "0 = off.", "× stop", 0, 5, adv=True, on=1.5),
    F("FVG_TRAIL_R", "fvg", "float", "Trailing distance", "", "× stop", 0, 5, adv=True),
    F("FVG_PARTIAL_AT_R", "fvg", "float", "Book part of the profit at", "Sells the share below in whole lots, so it needs at least 2 lots. 0 = off.", "× stop", 0, 5, adv=True, on=1),
    F("FVG_PARTIAL_PCT", "fvg", "float", "Share to book", "", "%", 10, 90, adv=True),
    F("FVG_MAX_HOLD_MIN", "fvg", "float", "Max holding time", "Exit after this long whatever the price. 0 = off.", "min", 0, 360, adv=True, on=90),
    F("OB_ALLOW_LIVE", "ob", "bool", "Allow live Order Block trading (real money)", "Off by default: this strategy refuses live mode until it is switched on. Backtest and paper trade it first.", danger=True),
    F("OB_REQUIRE_SWEEP", "ob", "bool", "Require a liquidity sweep", "Price must trade through a confirmed swing and close back, with a real rejection wick."),
    F("OB_REQUIRE_MSS", "ob", "bool", "Require a structure shift", "The displacement candle must close through the last confirmed swing. After a sweep this is an MSS; without one it counts as a BOS."),
    F("OB_REQUIRE_FVG", "ob", "bool", "Strict: require FVG overlap", "On: only zones overlapping a Fair Value Gap. Off: order block with strong displacement and a structure shift; an overlap only adds score."),
    F("OB_REQUIRE_HTF", "ob", "bool", "15-minute trend must agree", "Uses completed 15-minute candles only."),
    F("OB_REQUIRE_VOLUME", "ob", "bool", "Volume must confirm the displacement", "Indices have no volume, so this uses the NIFTYBEES proxy. With no volume data the setup is skipped."),
    F("OB_REQUIRE_VWAP", "ob", "bool", "Close must be on the right side of VWAP", "Today's VWAP on the proxy volume when available, otherwise an average of typical prices."),
    F("OB_ZONE_MODE", "ob", "select", "Order block zone", "Which part of the order block candle counts as the zone.",
      choices=[("OPEN_TO_WICK", "Open to wick (default)"), ("FULL_CANDLE", "Full candle, high to low"), ("BODY", "Body only")]),
    F("OB_SL_METHOD", "ob", "select", "Stop-loss beyond", "Plus the stop buffer. Wider of the two gives the setup the most room and the biggest loss when hit.",
      choices=[("wider", "Zone or sweep, whichever is further"), ("ob", "The order block zone"), ("sweep", "The liquidity sweep")]),
    F("OB_TARGET_MODE", "ob", "select", "Target", "A swing target is skipped if it pays less than the minimum reward : risk.",
      choices=[("rr", "Multiple of the stop"), ("liquidity", "Opposing liquidity that pays the minimum"), ("swing", "Nearest opposing swing")]),
    F("OB_RR", "ob", "float", "Target multiple", "1:2 or 1:3. Used when Target is a multiple of the stop.", "× stop", 2, 3),
    F("OB_MIN_SCORE", "ob", "int", "Minimum score", "Sweep 20, MSS 20, displacement 15, order block 15, FVG 15, 15-min trend 10, volume 5. Ranks setup quality; it is not a chance of winning.", "of 100", 0, 100),
    F("OB_MAX_TRADES_PER_DAY", "ob", "int", "Order block signals per day", "Per index. Max trades per day under Risk limits still caps the whole bot.", "trades", 1, 20),
    F("OB_BREAKEVEN_AT_R", "ob", "float", "Move stop to cost at", "0 = off.", "× stop", 0, 5),
    F("OB_TRAIL_START_R", "ob", "float", "Start trailing stop at", "0 = off.", "× stop", 0, 5, on=1.5),
    F("OB_PARTIAL_AT_R", "ob", "float", "Book part of the profit at", "Sells the share below in whole lots, so it needs at least 2 lots. 0 = off.", "× stop", 0, 5, on=1.0),
    F("OB_PREMIUM_EXITS", "ob", "bool", "Option-price stop and target too", "Also exits on levels set from the option's fill price, so time decay can stop a trade even if the index hasn't moved."),
    F("OB_SWING_LOOKBACK", "ob", "int", "Swing size", "Candles each side of a swing. It only counts once those candles have closed.", "candles", 3, 5, adv=True),
    F("OB_DISPLACEMENT_ATR", "ob", "float", "Displacement body at least", "", "× ATR", 0.2, 5, adv=True),
    F("OB_DISPLACEMENT_BODY_PCT", "ob", "float", "Displacement body share", "Body as a share of the candle's range.", "", 0.1, 1, adv=True),
    F("OB_SWEEP_MIN_WICK_PCT", "ob", "float", "Sweep rejection wick at least", "Share of the sweep candle's range.", "", 0, 1, adv=True),
    F("OB_SWEEP_MAX_AGE", "ob", "int", "Sweep stays usable for", "", "candles", 1, 50, adv=True),
    F("OB_REJECTION_WICK_PCT", "ob", "float", "Rejection wick at least", "Share of the rejection candle's range, unless it also closes beyond the previous candle.", "", 0, 1, adv=True),
    F("OB_MAX_ZONE_ATR", "ob", "float", "Widest zone", "", "× ATR", 0.1, 10, adv=True),
    F("OB_MAX_AGE", "ob", "int", "Zone stays tradeable for", "Zones also end with the session.", "candles", 1, 75, adv=True),
    F("OB_MAX_DISTANCE_ATR", "ob", "float", "Furthest entry from the zone", "", "× ATR", 0.1, 10, adv=True),
    F("OB_FVG_MIN_OVERLAP_PCT", "ob", "float", "FVG overlap needed", "As a share of the narrower of the zone and the gap.", "%", 1, 100, adv=True),
    F("OB_VOLUME_MULT", "ob", "float", "Volume needed", "", "× average", 0.5, 5, adv=True),
    F("OB_SL_BUFFER_ATR", "ob", "float", "Stop buffer", "", "× ATR", 0, 2, adv=True),
    F("OB_MAX_RISK_ATR", "ob", "float", "Skip if the stop is wider than", "", "× ATR", 0.5, 10, adv=True),
    F("OB_MIN_RR", "ob", "float", "Minimum reward : risk", "Setups paying less are skipped.", "× stop", 2, 5, adv=True),
    F("OB_TRAIL_R", "ob", "float", "Trailing distance", "", "× stop", 0.1, 5, adv=True),
    F("OB_PARTIAL_PCT", "ob", "float", "Share to book", "", "%", 10, 90, adv=True),
    F("OB_OPTION_DELTA", "ob", "float", "Assumed option delta", "Turns index distances into option-price levels.", "", 0.2, 1, adv=True),
    # ---- Strategy 4: VWAP + Volume
    F("VWAP_ALLOW_LIVE", "vwap", "bool", "Allow live VWAP trading (real money)", "Off by default: this strategy refuses live mode until it is switched on. Backtest and paper trade it first.", danger=True),
    F("VWAP_VOLUME_MULT", "vwap", "float", "Volume needed", "Signal candle volume vs the average.", "× average", 0.5, 5),
    F("VWAP_VOLUME_BASIS", "vwap", "select", "Volume compared with", "Volume is heavy at the open and close, so the previous-20 rule favours those times; time of day compares with the same slot on recent days.",
      choices=[("rolling", "Previous 20 candles"), ("time_of_day", "Same time on recent days")]),
    F("VWAP_REQUIRE_VOLUME", "vwap", "bool", "Skip when volume is unavailable", "Indices have no volume; the NIFTYBEES proxy can be missing."),
    F("VWAP_REQUIRE_HTF", "vwap", "bool", "15-minute trend must agree", "Completed 15-minute candles only."),
    F("VWAP_RSI_CE_MIN", "vwap", "float", "CE: RSI at least", "", "", 0, 100),
    F("VWAP_RSI_CE_MAX", "vwap", "float", "CE: RSI at most", "", "", 0, 100),
    F("VWAP_RSI_PE_MIN", "vwap", "float", "PE: RSI at least", "", "", 0, 100),
    F("VWAP_RSI_PE_MAX", "vwap", "float", "PE: RSI at most", "", "", 0, 100),
    F("VWAP_MAX_EXT_ATR", "vwap", "float", "Skip when price is this far from VWAP", "Too extended to chase.", "× ATR", 0.2, 10),
    F("VWAP_RR", "vwap", "float", "Target", "At least 2.", "× stop", 2, 5),
    F("VWAP_BREAKEVEN_AT_R", "vwap", "float", "Move stop to cost at", "0 = off.", "× stop", 0, 5),
    F("VWAP_TRAIL_START_R", "vwap", "float", "Start trailing stop at", "0 = off.", "× stop", 0, 5, on=1.5),
    F("VWAP_EXIT_ON_INVALIDATION", "vwap", "bool", "Exit when VWAP turns against the trade", "A close back through VWAP, or VWAP sloping the other way."),
    F("VWAP_MAX_SIGNALS_PER_DAY", "vwap", "int", "VWAP signals per day", "Per index. Max trades per day under Risk limits still caps the whole bot.", "trades", 1, 20),
    F("VWAP_EMA_FAST", "vwap", "int", "Fast EMA", "", "candles", 5, 100, adv=True),
    F("VWAP_EMA_SLOW", "vwap", "int", "Slow EMA", "", "candles", 10, 200, adv=True),
    F("VWAP_VOLUME_AVG_N", "vwap", "int", "Volume average over", "", "candles", 5, 60, adv=True),
    F("VWAP_SLOPE_BARS", "vwap", "int", "VWAP slope measured over", "", "candles", 2, 30, adv=True),
    F("VWAP_MIN_SLOPE_ATR", "vwap", "float", "Flat VWAP below", "Slope over that many candles.", "× ATR", 0, 5, adv=True),
    F("VWAP_MAX_CROSSES", "vwap", "int", "Choppy when price crossed VWAP", "Within the lookback below.", "times", 1, 20, adv=True),
    F("VWAP_CROSS_LOOKBACK", "vwap", "int", "Chop lookback", "", "candles", 4, 60, adv=True),
    F("VWAP_PULLBACK_BARS", "vwap", "int", "Pullback within the last", "", "candles", 1, 20, adv=True),
    F("VWAP_TOUCH_ATR", "vwap", "float", "Pullback counts within", "Of VWAP or the fast EMA.", "× ATR", 0, 2, adv=True),
    F("VWAP_MIN_BODY_PCT", "vwap", "float", "Confirmation body share", "", "", 0.1, 1, adv=True),
    F("VWAP_SL_BUFFER_ATR", "vwap", "float", "Stop buffer", "Beyond the pullback swing.", "× ATR", 0, 2, adv=True),
    F("VWAP_MIN_RISK_ATR", "vwap", "float", "Narrowest stop", "Applied before entry, never after.", "× ATR", 0.1, 5, adv=True),
    F("VWAP_MAX_RISK_ATR", "vwap", "float", "Skip if the stop is wider than", "", "× ATR", 0.5, 10, adv=True),
    F("VWAP_TRAIL_R", "vwap", "float", "Trailing distance", "", "× stop", 0.1, 5, adv=True),
    F("VWAP_COOLDOWN_BARS", "vwap", "int", "Candles before the same side may fire again", "", "candles", 0, 50, adv=True),

    # ---- Strategy 5: Opening Range Breakout + Retest
    F("ORB_ALLOW_LIVE", "orb", "bool", "Allow live ORB trading (real money)", "Off by default: this strategy refuses live mode until it is switched on. Backtest and paper trade it first.", danger=True),
    F("ORB_RANGE_START", "orb", "time", "Range starts", "Market open."),
    F("ORB_RANGE_END", "orb", "time", "Range ends", "The range is final when the candle before this time closes."),
    F("ORB_REQUIRE_RETEST", "orb", "bool", "Wait for a retest", "Off: enter on the breakout candle itself."),
    F("ORB_REQUIRE_VOLUME", "orb", "bool", "Breakout needs volume", "NIFTYBEES proxy. With no volume data the breakout is skipped."),
    F("ORB_VOLUME_MULT", "orb", "float", "Breakout volume needed", "", "× average", 0.5, 5),
    F("ORB_VOLUME_BASIS", "orb", "select", "Volume compared with", "See VWAP rules: time of day removes the open/close volume bias.",
      choices=[("rolling", "Previous 20 candles"), ("time_of_day", "Same time on recent days")]),
    F("ORB_REQUIRE_HTF", "orb", "bool", "15-minute trend must agree", "Completed 15-minute candles only."),
    F("ORB_NO_ENTRY_AFTER", "orb", "time", "No ORB entry after", "Late breakouts rarely pay 1:2."),
    F("ORB_RR", "orb", "float", "Target", "At least 2.", "× stop", 2, 5),
    F("ORB_BREAKEVEN_AT_R", "orb", "float", "Move stop to cost at", "0 = off.", "× stop", 0, 5),
    F("ORB_TRAIL_START_R", "orb", "float", "Start trailing stop at", "0 = off.", "× stop", 0, 5, on=1.5),
    F("ORB_EXIT_ON_STRUCTURE_FAIL", "orb", "bool", "Exit when price closes back inside the range", "The breakout has failed."),
    F("ORB_MAX_SIGNALS_PER_DAY", "orb", "int", "ORB signals per day", "Per index.", "trades", 1, 5),
    F("ORB_MIN_RANGE_ATR", "orb", "float", "Skip ranges narrower than", "", "× ATR", 0, 10, adv=True),
    F("ORB_MAX_RANGE_ATR", "orb", "float", "Skip ranges wider than", "", "× ATR", 0.5, 20, adv=True),
    F("ORB_MAX_RANGE_PCT", "orb", "float", "Skip ranges wider than", "Of the index.", "%", 0.1, 5, adv=True),
    F("ORB_MIN_BODY_PCT", "orb", "float", "Breakout body share", "", "", 0.1, 1, adv=True),
    F("ORB_BREAK_BUFFER_ATR", "orb", "float", "Close beyond the range by", "", "× ATR", 0, 2, adv=True),
    F("ORB_RETEST_BARS", "orb", "int", "Retest within", "", "candles", 1, 30, adv=True),
    F("ORB_RETEST_TOL_ATR", "orb", "float", "Retest counts within", "Of the range edge.", "× ATR", 0, 2, adv=True),
    F("ORB_CONFIRM_BARS", "orb", "int", "Confirmation within", "After the retest.", "candles", 1, 20, adv=True),
    F("ORB_FAIL_BUFFER_ATR", "orb", "float", "False breakout when closing back inside by", "", "× ATR", 0, 2, adv=True),
    F("ORB_MAX_CHASE_ATR", "orb", "float", "Skip entries this far past the range", "", "× ATR", 0.1, 10, adv=True),
    F("ORB_SL_BUFFER_ATR", "orb", "float", "Stop buffer", "Beyond the retest swing.", "× ATR", 0, 2, adv=True),
    F("ORB_MIN_RISK_ATR", "orb", "float", "Narrowest stop", "Applied before entry, never after.", "× ATR", 0.1, 5, adv=True),
    F("ORB_MAX_RISK_ATR", "orb", "float", "Skip if the stop is wider than", "", "× ATR", 0.5, 10, adv=True),
    F("ORB_TRAIL_R", "orb", "float", "Trailing distance", "", "× stop", 0.1, 5, adv=True),

    # ---- Strategy 6: Liquidity Sweep + MSS
    F("LSM_ALLOW_LIVE", "lsmss", "bool", "Allow live Liquidity Sweep trading (real money)", "Off by default: this strategy refuses live mode until it is switched on. Backtest and paper trade it first.", danger=True),
    F("LSM_REQUIRE_EQUAL", "lsmss", "bool", "Only sweeps of equal highs / lows", "Off: a single swing high or low also counts as liquidity; equal ones add score."),
    F("LSM_ENTRY_MODE", "lsmss", "select", "Entry into the FVG", "Same rules as the FVG strategy.",
      choices=[("aggressive", "Touch of the gap"), ("balanced", "Midpoint rejection"), ("conservative", "Confirmation candle after a touch")]),
    F("LSM_REQUIRE_HTF", "lsmss", "bool", "15-minute structure check", "Swings of completed 15-minute candles."),
    F("LSM_HTF_MODE", "lsmss", "select", "15-minute structure must", "",
      choices=[("not_against", "Not be against the trade"), ("with", "Agree with the trade")]),
    F("LSM_RR", "lsmss", "float", "Target", "At least 2.", "× stop", 2, 5),
    F("LSM_BREAKEVEN_AT_R", "lsmss", "float", "Move stop to cost at", "0 = off.", "× stop", 0, 5),
    F("LSM_TRAIL_START_R", "lsmss", "float", "Start trailing stop at", "0 = off.", "× stop", 0, 5, on=1.5),
    F("LSM_EXIT_ON_INVALIDATION", "lsmss", "bool", "Exit on a close beyond the sweep extreme", ""),
    F("LSM_MAX_SIGNALS_PER_DAY", "lsmss", "int", "Liquidity sweep signals per day", "Per index.", "trades", 1, 10),
    F("LSM_SWING_N", "lsmss", "int", "Swing size", "Candles each side; known only once they close.", "candles", 1, 5, adv=True),
    F("LSM_EQUAL_TOL_ATR", "lsmss", "float", "Equal highs / lows within", "", "× ATR", 0, 1, adv=True),
    F("LSM_SWEEP_MIN_PEN_ATR", "lsmss", "float", "Sweep goes past the level by at least", "", "× ATR", 0, 2, adv=True),
    F("LSM_SWEEP_MAX_PEN_ATR", "lsmss", "float", "... and at most", "Deeper is a breakdown.", "× ATR", 0.1, 5, adv=True),
    F("LSM_SWEEP_MIN_WICK_PCT", "lsmss", "float", "Sweep rejection wick at least", "Share of the sweep candle's range.", "", 0, 1, adv=True),
    F("LSM_MSS_BARS", "lsmss", "int", "Structure shift within", "After the sweep.", "candles", 1, 30, adv=True),
    F("LSM_DISP_ATR", "lsmss", "float", "Displacement body at least", "", "× ATR", 0.2, 5, adv=True),
    F("LSM_DISP_BODY_PCT", "lsmss", "float", "Displacement body share", "", "", 0.1, 1, adv=True),
    F("LSM_FVG_MIN_ATR", "lsmss", "float", "Smallest FVG", "", "× ATR", 0, 3, adv=True),
    F("LSM_ZONE_MAX_AGE", "lsmss", "int", "FVG stays tradeable for", "", "candles", 1, 75, adv=True),
    F("LSM_SL_BUFFER_ATR", "lsmss", "float", "Stop buffer", "Beyond the sweep extreme.", "× ATR", 0, 2, adv=True),
    F("LSM_MAX_RISK_ATR", "lsmss", "float", "Skip if the stop is wider than", "", "× ATR", 0.5, 10, adv=True),
    F("LSM_TRAIL_R", "lsmss", "float", "Trailing distance", "", "× stop", 0.1, 5, adv=True),

    # ---- Strategy 7: Supertrend + ADX
    F("ST_ALLOW_LIVE", "supertrend", "bool", "Allow live Supertrend trading (real money)", "Off by default: this strategy refuses live mode until it is switched on. Backtest and paper trade it first.", danger=True),
    F("ST_ATR_PERIOD", "supertrend", "int", "Supertrend ATR period", "", "candles", 5, 50),
    F("ST_MULT", "supertrend", "float", "Supertrend multiplier", "", "× ATR", 1, 6),
    F("ST_ADX_MIN", "supertrend", "float", "ADX at least", "Trend strength; below it the market counts as choppy.", "", 10, 60),
    F("ST_RSI_FILTER", "supertrend", "bool", "Require RSI in the bands below", "Off by default: in testing the RSI band, the EMA-50 check and rising ADX together left ~13 trades in 47 sessions."),
    F("ST_EMA_FILTER", "supertrend", "bool", "Require close on the trend-EMA side", "The Trend EMA (advanced). Off by default."),
    F("ST_VWAP_FILTER", "supertrend", "bool", "Require close on the session-VWAP side", "Off by default: it cut winners as much as losers in testing."),
    F("ST_CONFIRM", "supertrend", "select", "Confirmation candle", "Both need a strong body in the trend direction.",
      choices=[("close", "Strong candle on the trend side"), ("break", "…and closes beyond the previous candle")]),
    F("ST_RSI_CE_MIN", "supertrend", "float", "CE: RSI at least", "", "", 0, 100),
    F("ST_RSI_CE_MAX", "supertrend", "float", "CE: RSI at most", "", "", 0, 100),
    F("ST_RSI_PE_MIN", "supertrend", "float", "PE: RSI at least", "", "", 0, 100),
    F("ST_RSI_PE_MAX", "supertrend", "float", "PE: RSI at most", "", "", 0, 100),
    F("ST_SL_MODE", "supertrend", "select", "Stop-loss", "",
      choices=[("atr", "ATR distance"), ("wider", "ATR distance or beyond the Supertrend line, whichever is wider")]),
    F("ST_SL_ATR_MULT", "supertrend", "float", "ATR stop distance", "", "× ATR", 0.3, 5),
    F("ST_REQUIRE_HTF", "supertrend", "bool", "15-minute trend must agree", "Completed 15-minute candles only."),
    F("ST_RR", "supertrend", "float", "Target", "At least 2.", "× stop", 2, 5),
    F("ST_BREAKEVEN_AT_R", "supertrend", "float", "Lock profit at", "When the trade reaches this, the stop moves to entry plus the share below. 0 = off.", "× stop", 0, 5),
    F("ST_BE_LOCK_PCT", "supertrend", "float", "Profit locked", "Share of the stop distance locked in (at least costs).", "% of stop", 0, 90),
    F("ST_TRAIL_START_R", "supertrend", "float", "Start trailing stop at", "0 = off.", "× stop", 0, 5, on=1.5),
    F("ST_EXIT_ON_FLIP", "supertrend", "bool", "Exit when Supertrend flips", ""),
    F("ST_MAX_SIGNALS_PER_DAY", "supertrend", "int", "Supertrend signals per day", "Per index.", "trades", 1, 20),
    F("ST_ADX_PERIOD", "supertrend", "int", "ADX period", "", "candles", 5, 50, adv=True),
    F("ST_ADX_RISING_BARS", "supertrend", "int", "ADX must be higher than this many candles ago", "0 = off.", "candles", 0, 20, adv=True, on=3),
    F("ST_DI_GAP", "supertrend", "float", "+DI / −DI gap at least", "", "", 0, 50, adv=True),
    F("ST_EMA_TREND", "supertrend", "int", "Trend EMA", "", "candles", 10, 200, adv=True),
    F("ST_EMA_PULLBACK", "supertrend", "int", "Pullback EMA", "", "candles", 5, 100, adv=True),
    F("ST_PULLBACK_BARS", "supertrend", "int", "Pullback within the last", "", "candles", 1, 20, adv=True),
    F("ST_TOUCH_ATR", "supertrend", "float", "Pullback counts within", "Of the Supertrend line or pullback EMA.", "× ATR", 0, 2, adv=True),
    F("ST_MIN_BODY_PCT", "supertrend", "float", "Confirmation body share", "", "", 0.1, 1, adv=True),
    F("ST_MIN_RISK_ATR", "supertrend", "float", "Narrowest stop", "Applied before entry, never after.", "× ATR", 0.1, 5, adv=True),
    F("ST_MAX_RISK_ATR", "supertrend", "float", "Skip if the stop is wider than", "", "× ATR", 0.5, 10, adv=True),
    F("ST_TRAIL_R", "supertrend", "float", "Trailing distance", "", "× stop", 0.1, 5, adv=True),
    F("ST_COOLDOWN_BARS", "supertrend", "int", "Candles before the same side may fire again", "", "candles", 0, 50, adv=True),
    # ---- Strategy 8: Trend Continuation
    F("TC_ALLOW_LIVE", "tc", "bool", "Allow live Trend Continuation trading (real money)", "Off by default: this strategy refuses live mode until it is switched on. Backtest and paper trade it first.", danger=True),
    F("TC_SL_ATR", "tc", "float", "Stop-loss distance", "Tested: 1.5 ATR lost most of the edge; normal pullbacks were about 1.3–1.5 ATR.", "× ATR", 1, 5),
    F("TC_RR", "tc", "float", "Target", "At least 2.", "× stop", 2, 5),
    F("TC_MIN_BREAK_ATR", "tc", "float", "Minimum break past the range", "A close only just past the opening range doesn't count; the strategy waits for one at least this far past it. 0 = any close past it.", "× ATR", 0, 3),
    F("TC_BREAKEVEN_AT_R", "tc", "float", "Lock profit at", "When the trade reaches this, the stop moves to entry plus the share below. 0 = off.", "× stop", 0, 5),
    F("TC_BE_LOCK_PCT", "tc", "float", "Profit locked", "Share of the stop distance locked in (at least costs). 25% means a trade that reached 1R can't end a loser.", "% of stop", 0, 90),
    F("TC_TRAIL_START_R", "tc", "float", "Start trailing stop at", "0 = off.", "× stop", 0, 5, on=1.5),
    F("TC_EXIT_ON_FAIL", "tc", "bool", "Exit when price falls back inside the range and through VWAP", "Off by default: in testing it cut trades that later recovered."),
    F("TC_ENTRY_FROM", "tc", "time", "First entry at", "A candle closing at this time or later. The first hour is for the range to form."),
    F("TC_ENTRY_UNTIL", "tc", "time", "Last entry at", "No first aligned close by then: no trade that day."),
    F("TC_OR_START", "tc", "time", "Opening range starts", "", adv=True),
    F("TC_OR_END", "tc", "time", "Opening range ends", "", adv=True),
    F("TC_TRAIL_R", "tc", "float", "Trailing distance", "", "× stop", 0.1, 5, adv=True),

    F("WHATSAPP_ALERTS", "alerts", "bool", "Send a WhatsApp message on every entry",
      "Uses the free CallMeBot WhatsApp API. From the phone, send \"I allow callmebot to send me messages\" to the number at callmebot.com/blog/free-api-whatsapp-messages, then paste the key it replies with below."),
    F("WHATSAPP_NUMBER", "alerts", "text", "WhatsApp number", "With country code, digits only, e.g. 919030953685.", dep="WHATSAPP_ALERTS"),
    F("WHATSAPP_APIKEY", "alerts", "secret", "CallMeBot API key", "Leave blank to keep the saved one.", dep="WHATSAPP_ALERTS"),
    F("TELEGRAM_ALERTS", "alerts", "bool", "Send a Telegram message on every entry",
      "Free, no key: open api2.callmebot.com/txt/login.php once and log in with Telegram, then enter your username below."),
    F("TELEGRAM_USER", "alerts", "text", "Telegram usernames",
      "One or more, separated by commas, e.g. @saichandui, @friend1. Everyone listed gets every alert; each person must log in once at api2.callmebot.com/txt/login.php with their own Telegram.", dep="TELEGRAM_ALERTS"),
    F("SKIP_ALERTS", "alerts", "bool", "Alert on skipped signals",
      "Main bot: a message when a signal fires but no trade is placed — low volume, trader HOLD, risk-manager veto, not enough funds, risk limits, outside the entry window, already in a trade, or order not filled — with the reason."),
    F("HEADS_UP_ALERTS", "alerts", "bool", "Get-ready alerts before a signal",
      "Main bot: a message when a signal may come on the next candle(s) — EMA cross forming or waiting for its confirming candle, or price near a live FVG / order block zone. Each setup is sent once. Not a trade."),

    F("USE_CRUDE_AGENT", "agents", "bool", "Crude-oil agent", "Blocks calls when Brent jumps and puts when it falls. Reduced drawdown in testing."),
    F("CRUDE_BLOCK_PCT", "agents", "float", "Crude move that blocks", "Brent up this much blocks calls; down this much blocks puts.", "%", 0.1, 10, dep="USE_CRUDE_AGENT"),
    F("USE_NEWS_AGENT", "agents", "bool", "News agent", "Scores the last 24 hours of Indian market headlines."),
    F("NEWS_BLOCK_SCORE", "agents", "float", "News score that blocks", "Sentiment runs from −1 to +1. Beyond this level, the opposite side is blocked.", "", 0, 1, dep="USE_NEWS_AGENT"),
    F("NEWS_BLOCK_ON_EVENT", "agents", "bool", "No trades on big-event days", "RBI policy, Fed decision, Budget and results days often whipsaw.", dep="USE_NEWS_AGENT"),
    F("USE_GLOBAL_AGENT", "agents", "bool", "Global markets agent",
      "GIFT Nifty, US, Europe and Asia indices, US futures, the dollar, the rupee and US yields, rolled into one score for the debate."),
    F("GLOBAL_BLOCK_SCORE", "agents", "float", "Global score that blocks",
      "From −1 to +1. A worldwide sell-off this strong blocks calls; a rally this strong blocks puts.", "", 0.1, 1, dep="USE_GLOBAL_AGENT"),
    F("USE_TECHNICAL_AGENT", "agents", "bool", "Technical analyst", "RSI, MACD and Bollinger read for the debate."),
    F("USE_SENTIMENT_AGENT", "agents", "bool", "Sentiment analyst", "Reddit chatter, India VIX and how many big stocks are up."),
    F("USE_FUNDAMENTAL_AGENT", "agents", "bool", "Fundamental analyst", "Earnings health of the 12 biggest Nifty stocks. Refreshes every 12 hours."),
    F("USE_TRADER_AGENT", "agents", "bool", "Let the debate overrule signals", "When the bull vs bear debate is strongly against a signal, the trader holds."),
    F("TRADER_BLOCK_MARGIN", "agents", "float", "How strongly against", "The debate scores bull minus bear from −1 to +1. At this much against a signal, the trader holds.", "", 0, 1, dep="USE_TRADER_AGENT"),
    F("AGENT_AUTO_REFRESH", "agents", "bool", "Auto-refresh while the bot is stopped",
      "Keeps agents current while this panel is open. Each refetches only when due (crude 3 min … fundamentals 12 h)."),

    F("SCALP_STRATEGY", "scalper", "select", "Entry rule",
      "EMA cross: the main bot's rules on fast candles. Volume spike: a breakout candle on 1.5× normal volume, on the VWAP side. Experimental rules are research from the Strategy lab page and refuse live mode. Every rule lost money in the backtest with realistic fills.",
      choices=[("v2", "Scalper V2 — filtered (recommended for paper)"), ("ema", "EMA cross (main bot rules)"), ("volume", "Volume spike breakout"),
               ("sweep", "EXPERIMENTAL · Liquidity sweep reversal (paper only)"),
               ("retest", "EXPERIMENTAL · Breakout-retest (paper only)")]),
    F("TRADE_SCORE_MIN", "scalper", "int", "V2: minimum trade score", "Trend 20, momentum 15, VWAP 10, RSI 10, MACD 10, volume 10, breakout 10, option liquidity 10, volatility 5. Higher = fewer, cleaner trades.", "/ 100", 50, 100),
    F("SCALP_CE_ENABLED", "scalper", "bool", "V2: take CE (bullish) scalps"),
    F("SCALP_PE_ENABLED", "scalper", "bool", "V2: take PE (bearish) scalps"),
    F("SCALP_DYNAMIC_SL", "scalper", "bool", "V2: stop sized to volatility", "On: premium stop = 1-min ATR × delta × 0.8 (5–12 pts), target = 2 × the stop. Off (current): V2 uses the fixed Stop-loss above and a 2× target — 15 / 30 points."),
    F("SCALP_BE_AT_R", "scalper", "float", "V2: move stop to cost at", "In multiples of the stop. 1.0, 1.2 and 1.5 gave the same backtest result. 0 = off.", "× stop", 0, 5),
    F("SCALP_MAX_SPREAD_PCT", "scalper", "float", "V2: max option spread", "Skip an option whose bid/ask spread is wider than this. Needs the Zerodha feed — the free feed has no order book.", "%", 0.1, 5),
    F("SCALP_V2_STRICT", "scalper", "bool", "V2: strict mode", "Every V2 rule mandatory (RSI, MACD, structure, body). The backtest found ~1 trade in 18 days.", adv=True),
    F("SCALP_REGIME_FILTER", "scalper", "bool", "V2: trend-regime filter", "Only strong up/downtrends trade; range, choppy and high-volatility minutes are skipped.", adv=True),
    F("SCALP_ADX_MIN", "scalper", "float", "V2: ADX for a strong trend", "", "", 10, 50, adv=True),
    F("SCALP_ATR_MIN_PCT", "scalper", "float", "V2: minimum 1-min ATR", "As a % of the index. Below this the market counts as dead.", "%", 0, 1, adv=True),
    F("SCALP_EMA_SEPARATION_MIN", "scalper", "float", "V2: minimum EMA9–EMA21 gap", "", "%", 0, 1, adv=True),
    F("SCALP_MAX_VWAP_DISTANCE_PCT", "scalper", "float", "V2: max distance from VWAP", "Don't chase: wait for a pullback when price is further than this from VWAP.", "%", 0.02, 1, adv=True),
    F("SCALP_CE_RSI_MIN", "scalper", "float", "V2: CE RSI from", "", "", 0, 100, adv=True),
    F("SCALP_CE_RSI_MAX", "scalper", "float", "V2: CE RSI up to", "", "", 0, 100, adv=True),
    F("SCALP_PE_RSI_MIN", "scalper", "float", "V2: PE RSI from", "", "", 0, 100, adv=True),
    F("SCALP_PE_RSI_MAX", "scalper", "float", "V2: PE RSI up to", "", "", 0, 100, adv=True),
    F("SCALP_USE_MACD", "scalper", "bool", "V2: MACD confirmation", adv=True),
    F("SCALP_PULLBACK_ENTRY", "scalper", "bool", "V2: pullback entries", "Pullback to EMA9/21 that holds EMA21, then a break of the pullback high/low.", adv=True),
    F("SCALP_BREAKOUT_ENTRY", "scalper", "bool", "V2: volume-breakout entries", "Entered one candle later, only if the breakout holds.", adv=True),
    F("SCALP_CROSS_INDEX_FILTER", "scalper", "bool", "V2: check the other index", "Nifty checks Bank Nifty, Sensex checks Nifty. Skips a trade the other index strongly disagrees with.", adv=True),
    F("SCALP_VIX_FILTER", "scalper", "bool", "V2: India VIX filter", "Skips the calmest 10% of days (with low ATR) and VIX spikes above the 95th percentile of the last 20 days.", adv=True),
    F("SCALP_OPENING_RANGE_FILTER", "scalper", "bool", "V2: opening-range filter", "If the first 5 minutes moved more than 0.35%, wait until volatility settles.", adv=True),
    F("SCALP_MIN_SL_POINTS", "scalper", "float", "V2: smallest premium stop", "", "pts", 1, 50, adv=True),
    F("SCALP_MAX_SL_POINTS", "scalper", "float", "V2: largest premium stop", "", "pts", 1, 100, adv=True),
    F("SCALP_DELTA_MIN", "scalper", "float", "V2: option delta from", "Preferred delta band when choosing the strike.", "", 0.3, 0.95, adv=True),
    F("SCALP_DELTA_MAX", "scalper", "float", "V2: option delta up to", "", "", 0.3, 0.95, adv=True),
    F("SCALP_STRUCTURAL_COOLDOWN", "scalper", "bool", "V2: fresh setup after a stop", "After a stop, the same side needs price to reset through EMA9 and a new setup, not just time.", adv=True),
    F("SCALP_SIDE_ADAPT", "scalper", "bool", "V2: tougher on a losing side", "If a side's last 8 closed scalps have profit factor under 0.8, it needs 10 more score points.", adv=True),
    F("SCALP_VOL_MULT", "scalper", "float", "Volume spike size", "Candle volume must be this many times its recent average. Indices have no volume, so NIFTYBEES volume is used.", "×", 1, 10, adv=True),
    F("SCALP_VOL_AVG_N", "scalper", "int", "Volume average over", "", "candles", 5, 100, adv=True),
    F("SCALP_VOL_BREAKOUT_N", "scalper", "int", "Breakout of last", "The close must clear the high (CE) or low (PE) of this many earlier candles.", "candles", 2, 30, adv=True),
    F("SCALP_POINTS_ON", "scalper", "select", "Points measured on", "Option price: 5/10 points on the premium. Index: 5/10 points on Nifty/Sensex.",
      choices=[("premium", "Option price"), ("index", "Index")]),
    F("SCALP_SL_POINTS", "scalper", "float", "Stop-loss", "On an ITM option (delta ≈ 0.6–0.7) 15 premium points is about 22 index points. Backtest: 5-point stops lost to noise and costs; 15/30 did better but on few trades.", "pts", 0.5, 200),
    F("SCALP_TARGET_POINTS", "scalper", "float", "Target", "", "pts", 0.5, 400),
    F("SCALP_BE_AT_POINTS", "scalper", "float", "Move stop to cost at", "After this many points in profit, the stop moves to entry plus charges. 0 = off.", "pts", 0, 200),
    F("SCALP_LOTS", "scalper", "int", "Max lots per scalp", "The most a scalp buys. The actual lots come from 90% of your funds, like the main bot.", "lots", 1, 50),
    F("SCALP_MAX_DAILY_LOSS", "scalper", "float", "Max scalp loss per day", "Checked before every scalp against its loss at the stop.", "₹", 0),
    F("SCALP_MAX_TRADES_PER_DAY", "scalper", "int", "Max scalps per day", "", "scalps", 1, 50),
    F("SCALP_MAX_CONSECUTIVE_LOSSES", "scalper", "int", "Stop after losses in a row", "", "losses", 1, 10),
    F("SCALP_NO_ENTRY_BEFORE", "scalper", "time", "First scalp at"),
    F("SCALP_NO_ENTRY_AFTER", "scalper", "time", "Last scalp at", "Must be before the main square-off time."),
    F("SCALP_ALLOW_LIVE", "scalper", "bool", "Allow live scalping (real money)",
      "Off by default. The scalper backtest lost money with realistic fills. Paper-trade it first.", danger=True),
    F("SCALP_CANDLE_MINUTES", "scalper", "int", "Signal candles", "", "min", 1, 5, adv=True),
    F("SCALP_MAX_HOLD_MIN", "scalper", "float", "Time stop", "Exit a scalp that has gone nowhere after this long. 0 = off.", "min", 0, 120, adv=True),
    F("SCALP_COOLDOWN_MIN", "scalper", "float", "Wait after each exit", "", "min", 0, 60, adv=True),
    F("SCALP_AVOID_OVERLAP", "scalper", "bool", "Skip an index the main bot holds", "Avoids doubling up on the same move.", adv=True),
]
# Risk profiles: one click fills these into the Settings form (nothing is saved until Save).
# All three share the trading hours and differ in capital per trade (₹50K / ₹75K / ₹1L), daily limits and
# how loose the entry filters are: Medium and High mean more trades and bigger losing days.
# Lot size, mode and the live-trading switches are never touched.
# Same for every profile: new entries 09:30–15:00 (scalps too), everything closed at 15:15.
_HOURS = dict(NO_ENTRY_BEFORE="09:30", NO_ENTRY_AFTER="15:00", SQUARE_OFF_AT="15:15", SCALP_NO_ENTRY_AFTER="15:00")
_LOW = dict(_HOURS,
    MAX_DAILY_LOSS=3000.0, MAX_TRADES_PER_DAY=3, MAX_CONSECUTIVE_LOSSES=2, MAX_PREMIUM_PER_TRADE=50000.0,
    RR=2.0, BREAKEVEN_AT_R=1.0, VIX_MULT_MAX=1.3,
    HTF_FILTER=True, ADX_MIN=0.0, VOLUME_FILTER=True, VOLUME_MIN_RVOL=1.2, MIN_ATR_PCT=0.05,
    RSI_BUY_CE=55.0, RSI_BUY_PE=45.0, RSI_MAX=75.0,
    FVG_ENTRY_MODE="balanced", FVG_MIN_SCORE=50, FVG_VOLUME_FILTER=True, FVG_VOLUME_MULT=1.5,
    FVG_MAX_TOUCHES=2, FVG_MAX_RISK_ATR=2.0,
    TRADE_SCORE_MIN=75, SCALP_REGIME_FILTER=True,
    SCALP_MAX_DAILY_LOSS=2500.0, SCALP_MAX_TRADES_PER_DAY=6, SCALP_MAX_CONSECUTIVE_LOSSES=2,
)
RISK_PROFILES = [
    ("low", "Low risk", "The tested entry filters. Few, well-filtered trades and tight daily limits.", _LOW),
    ("medium", "Medium risk", "Looser entry filters. More trades, bigger losing days possible.", dict(_LOW,
        MAX_DAILY_LOSS=5000.0, MAX_TRADES_PER_DAY=5, MAX_CONSECUTIVE_LOSSES=3, MAX_PREMIUM_PER_TRADE=75000.0,
        RR=1.8, BREAKEVEN_AT_R=1.2, VIX_MULT_MAX=1.4,
        VOLUME_MIN_RVOL=1.0, MIN_ATR_PCT=0.04, RSI_BUY_CE=53.0, RSI_BUY_PE=47.0, RSI_MAX=80.0,
        FVG_MIN_SCORE=40, FVG_VOLUME_MULT=1.2, FVG_MAX_RISK_ATR=2.5,
        TRADE_SCORE_MIN=70, SCALP_MAX_DAILY_LOSS=4000.0, SCALP_MAX_TRADES_PER_DAY=8, SCALP_MAX_CONSECUTIVE_LOSSES=3)),
    ("high", "High risk", "Trend and volume checks off. Many more trades and much bigger swings.", dict(_LOW,
        MAX_DAILY_LOSS=8000.0, MAX_TRADES_PER_DAY=8, MAX_CONSECUTIVE_LOSSES=4, MAX_PREMIUM_PER_TRADE=100000.0,
        RR=1.5, BREAKEVEN_AT_R=1.5, VIX_MULT_MAX=1.5,
        HTF_FILTER=False, VOLUME_FILTER=False, VOLUME_MIN_RVOL=1.0, MIN_ATR_PCT=0.03,
        RSI_BUY_CE=52.0, RSI_BUY_PE=48.0, RSI_MAX=85.0,
        FVG_ENTRY_MODE="aggressive", FVG_MIN_SCORE=30, FVG_VOLUME_FILTER=False, FVG_VOLUME_MULT=1.2,
        FVG_MAX_TOUCHES=3, FVG_MAX_RISK_ATR=3.0,
        TRADE_SCORE_MIN=60, SCALP_REGIME_FILTER=False,
        SCALP_MAX_DAILY_LOSS=6000.0, SCALP_MAX_TRADES_PER_DAY=12, SCALP_MAX_CONSECUTIVE_LOSSES=4)),
]

_PY = {"int": int, "float": float, "text": str, "secret": str, "time": str}
EDITABLE = {f["key"]: (tuple(c for c, _ in f["choices"]) if f["type"] == "select" else f["type"] if f["type"] in ("bool", "multi")
                       else _PY[f["type"]], f["section"], f["label"]) for f in SCHEMA}
LIMITS = {f["key"]: (f["min"], f["max"]) for f in SCHEMA}
TIME_KEYS = {f["key"] for f in SCHEMA if f["type"] == "time"}


# ------------------------------------------------------------------ helpers
def today():
    return datetime.now(IST).strftime("%Y-%m-%d")


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def P(name, uid=None):
    """A file in the user's own folder (settings, Kite login, trades, logs, bot state). Default: the signed-in user."""
    return os.path.join(accounts.user_dir(uid or g.user), name)


def load_config(uid=None):
    """Fresh read of config.py + the user's settings.json (no module caching)."""
    ns = {"__file__": os.path.join(BASE, "config.py"), "_settings_path": P(SETTINGS, uid)}
    with open("config.py") as f:
        exec(f.read(), ns)
    return {k: v for k, v in ns.items() if k.isupper()}


def default_config():
    """config.py values WITHOUT settings.json (what 'Reset to default' restores)."""
    ns = {"__file__": os.path.join(BASE, "__defaults__", "config.py")}
    with open("config.py") as f:
        exec(f.read(), ns)
    return {k: v for k, v in ns.items() if k.isupper()}


def kite_client(cfg):
    from kiteconnect import KiteConnect
    return KiteConnect(api_key=cfg["API_KEY"])


def kite_session(cfg):
    kite = kite_client(cfg)
    with open(P("access_token.txt")) as f:
        kite.set_access_token(f.read().strip())
    return kite


def kite_creds():
    """(user id, API key, access token) of a panel user logged in to Zerodha today, preferring the signed-in
    user. Index prices are the same for everyone, so any logged-in user's session can fetch them, and a session
    Zerodha just refused market data to gives way to one that may have it (a free app next to a paid one)."""
    me = g.get("user") if has_request_context() else None
    refused = None
    for uid in [me] + [u for u in accounts.ids() if u != me]:
        if not uid:
            continue
        tok = P("access_token.txt", uid)
        if read_json(P(TOKEN_META, uid), {}).get("date") != today() or not os.path.exists(tok):
            continue
        key = load_config(uid).get("API_KEY")
        if key:
            with open(tok) as f:
                c = uid, key, f.read().strip()
            if time.time() >= quote_denied.get(c[2], 0):
                return c
            refused = refused or c
    return refused


def login_state():
    meta = read_json(P(TOKEN_META), {})
    ok = os.path.exists(P("access_token.txt")) and meta.get("date") == today()
    return {"logged_in": ok, "user": meta.get("user") if ok else None,
            "login_time": meta.get("time") if ok else None}


_server_ip = {}


def server_ip():
    """The public IP Kite must whitelist when the panel runs on a server (its domain's address), else None."""
    host = urllib.parse.urlparse(PUBLIC_URL).hostname
    if host in ("127.0.0.1", "localhost"):
        return None
    if host not in _server_ip:
        try:
            _server_ip[host] = socket.gethostbyname(host)
        except OSError:
            return None
    return _server_ip[host]


def needs_login(cfg):
    return cfg["MODE"] == "live" or cfg.get("PAPER_FEED", "free") == "zerodha"


def pid_running(pidfile, script):
    """A bot left running by an earlier copy of this app still counts (its pid file says so),
    so Start can't launch a second copy that would double every order."""
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip() or 0)
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    return script in out


SCRIPTS = {"bot": "bot.py", "scalp": "scalper.py"}


def proc_running(kind, uid=None):
    uid = uid or g.user
    p = procs.get((uid, kind))
    if p is not None and p.poll() is None:
        return True
    procs.pop((uid, kind), None)
    return pid_running(P(kind + ".pid", uid), SCRIPTS[kind])


def bot_running(uid=None):
    return proc_running("bot", uid)


def scalp_running(uid=None):
    return proc_running("scalp", uid)


def any_bot_running():
    return any(bot_running(u) for u in accounts.ids())


def launch(kind, env):
    """Start the user's bot or scalper in their own folder: it reads their settings and Kite token
    and writes its state, trades and logs there, so each user's bot trades on their own account."""
    env.update(KITEBOT_SETTINGS=P(SETTINGS), PYTHONPATH=BASE)
    out = open(P(kind + "_console.log"), "a")
    procs[(g.user, kind)] = subprocess.Popen([sys.executable, "-u", os.path.join(BASE, SCRIPTS[kind])],
                                             cwd=accounts.user_dir(g.user), env=env,
                                             stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)


def stop_later(kind, stop_file):
    """Ask the bot to exit its position and quit; clear the STOP file once it has."""
    uid = g.user
    open(P(stop_file), "w").close()

    def reaper():
        for _ in range(60):
            if not proc_running(kind, uid):
                break
            time.sleep(1)
        if os.path.exists(P(stop_file, uid)):
            os.remove(P(stop_file, uid))
    threading.Thread(target=reaper, daemon=True).start()


def read_trades(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("qty", "entry", "exit", "pnl", "charges"):
            try:
                r[k] = float(r[k])
            except (ValueError, KeyError, TypeError):
                r[k] = 0.0
        try:  # older rows have no charges recorded: their net is the gross
            r["net_pnl"] = float(r["net_pnl"])
        except (ValueError, KeyError, TypeError):
            r["net_pnl"] = r["pnl"]
    return rows


def tail(path, n=200):
    if not os.path.exists(path):
        return ""
    with open(path, errors="replace") as f:
        return "".join(f.readlines()[-n:])


def today_tail(path, n=200):
    """The last n lines of a bot log written today (India time). Lines start with their date; a line without
    one (a wrapped message) belongs to the dated line above it."""
    if not os.path.exists(path):
        return ""
    day, keep, out = today(), False, []
    with open(path, errors="replace") as f:
        for ln in f.readlines()[-5000:]:
            if ln[:4].isdigit() and ln[4:5] == "-":
                keep = ln.startswith(day)
            if keep:
                out.append(ln)
    return "".join(out[-n:])


# ------------------------------------------------------------------ pages
@app.get("/")
def index():
    return send_from_directory(os.path.join(BASE, "web"), "index.html")


@app.get("/signin")
def signin_page():
    return send_from_directory(os.path.join(BASE, "web"), "signin.html")


@app.get("/guide")
@app.get("/guide/")
def guide_page():
    """The Strategy guide is a page of the panel now (#guide); old /guide links land there."""
    return redirect("/#guide")


@app.get("/guide/<path:name>")
def guide_file(name):
    return send_from_directory(os.path.join(BASE, "web", "guide"), name)


@app.get("/sw.js")
def service_worker():
    """Served from the root so it may show trade alerts for the whole panel."""
    resp = send_from_directory(os.path.join(BASE, "web"), "sw.js", max_age=0)
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


# ------------------------------------------------------------------ panel users
@app.post("/api/auth/login")
def auth_login():
    body = request.get_json(force=True)
    try:
        uid = accounts.check(str(body.get("username", "")), str(body.get("password", "")))
    except accounts.LockedOut as e:
        return jsonify(ok=False, error=f"Too many wrong passwords. Try again in {e} min."), 429
    if not uid:
        time.sleep(0.5)  # slows down password guessing
        return jsonify(ok=False, error="Wrong user name or password."), 401
    return jsonify(ok=True, token=accounts.open_session(uid), user=accounts.name_of(uid),
                   default_password=accounts.is_default(uid))


@app.get("/api/auth/me")
def auth_me():
    return jsonify(ok=True, id=g.user, user=accounts.name_of(g.user), default_password=accounts.is_default(g.user))


@app.post("/api/auth/logout")
def auth_logout():
    accounts.close_session(request.headers.get("X-Session", ""))
    return jsonify(ok=True)


@app.post("/api/auth/password")
def auth_password():
    body = request.get_json(force=True)
    err = accounts.set_password(g.user, str(body.get("current", "")), str(body.get("new", "")))
    if err:
        return jsonify(ok=False, error=err), 400
    accounts.close_other_sessions(g.user, keep=request.headers.get("X-Session", ""))
    return jsonify(ok=True)


# ------------------------------------------------------------------ zerodha login
# The Kite redirect is a plain page load that can't carry the panel's session header, so the login URL
# carries a one-time code (Kite hands redirect_params back on /callback) that says whose login it is.
kite_logins = {}                       # code -> (user id, issued at)


@app.post("/api/kite/login")
def kite_login():
    cfg = load_config()
    if not cfg.get("API_KEY"):
        return jsonify(ok=True, url="/#settings?need=key")
    code = secrets.token_urlsafe(16)
    now = time.time()
    for c in [c for c, (_, at) in kite_logins.items() if now - at > 600]:
        del kite_logins[c]
    kite_logins[code] = (g.user, now)
    return jsonify(ok=True, url=kite_client(cfg).login_url() + "&redirect_params=" + urllib.parse.quote(f"kb={code}"))


@app.get("/callback")
def callback():
    token = request.args.get("request_token")
    uid, at = kite_logins.pop(request.args.get("kb", ""), (None, 0))
    if not uid or time.time() - at > 600:
        return redirect("/#login?error=Login+expired+-+press+Log+in+with+Zerodha+again")
    if request.args.get("status") != "success" or not token:
        return redirect("/#login?error=Login+cancelled+or+failed")
    g.user = uid
    cfg = load_config()
    try:
        kite = kite_client(cfg)
        data = kite.generate_session(token, api_secret=cfg["API_SECRET"])
    except Exception as e:
        return redirect("/#login?error=" + str(e).replace(" ", "+")[:200])
    with open(P("access_token.txt"), "w") as f:
        f.write(data["access_token"])
    acct_cache.pop(uid, None)
    write_json(P(TOKEN_META), {"date": today(), "time": datetime.now(IST).strftime("%H:%M"),
                               "user": data.get("user_name") or data.get("user_id")})
    return redirect("/#dashboard")


@app.post("/api/logout")
def logout():
    acct_cache.pop(g.user, None)
    for p in (P("access_token.txt"), P(TOKEN_META)):
        if os.path.exists(p):
            os.remove(p)
    return jsonify(ok=True)


# ------------------------------------------------------------------ zerodha account (read-only)
acct_cache = {}                        # user id -> {"at": time, "data": ...}


@app.get("/api/zerodha/account")
def zerodha_account():
    """Funds, today's orders and positions from Kite. Only read calls — nothing is placed or changed."""
    if not login_state()["logged_in"]:
        return jsonify(ok=False, error="Log in to Zerodha to see your account."), 401
    cache = acct_cache.get(g.user)
    if cache and time.time() - cache["at"] < 10 and not request.args.get("force"):
        return jsonify(cache["data"])
    cfg = load_config()
    kite = kite_session(cfg)
    try:
        funds = kite.margins()
        orders = kite.orders()
        positions = kite.positions()
    except Exception as e:
        if type(e).__name__ == "TokenException":   # token expired or revoked on Zerodha's side
            return jsonify(ok=False, error="Your Zerodha session has expired. Log in again."), 401
        return jsonify(ok=False, error=f"Zerodha didn't answer: {str(e)[:160]}"), 502

    def seg(name):
        s = funds.get(name) or {}
        av, ut = s.get("available") or {}, s.get("utilised") or {}
        return dict(net=s.get("net", 0), cash=av.get("live_balance", av.get("cash", 0)),
                    opening=av.get("opening_balance", 0), collateral=av.get("collateral", 0),
                    used=ut.get("debits", 0), m2m=ut.get("m2m_realised", 0) + ut.get("m2m_unrealised", 0))

    pos = [dict(symbol=p["tradingsymbol"], exchange=p["exchange"], product=p["product"], qty=p["quantity"],
                avg=p["average_price"], ltp=p["last_price"], pnl=p["pnl"], m2m=p.get("m2m", 0),
                realised=p.get("realised", 0), unrealised=p.get("unrealised", 0))
           for p in positions.get("net", [])]
    ords = [dict(time=str(o.get("order_timestamp") or "")[11:19], symbol=o["tradingsymbol"], exchange=o["exchange"],
                 side=o["transaction_type"], type=o["order_type"], product=o["product"], qty=o["quantity"],
                 filled=o.get("filled_quantity", 0), price=o.get("average_price") or o.get("price") or 0,
                 trigger=o.get("trigger_price", 0), status=o["status"], msg=o.get("status_message") or "",
                 tag=o.get("tag") or "")
            for o in reversed(orders)]
    data = dict(ok=True, fetched=datetime.now(IST).strftime("%H:%M:%S"),
                equity=seg("equity"), commodity=seg("commodity"),
                positions=pos, orders=ords,
                day_pnl=round(sum(p["pnl"] for p in pos), 2),
                open_count=sum(1 for p in pos if p["qty"]))
    acct_cache[g.user] = {"at": time.time(), "data": data}
    return jsonify(data)


# ------------------------------------------------------------------ state
@app.get("/api/state")
def state():
    cfg = load_config()
    risk = read_json(P("state.json"), {})
    if risk.get("date") != today():
        risk = {"date": today(), "pnl": 0, "trades": 0, "consec_losses": 0, "locked": False, "reason": ""}
    status = read_json(P("status.json"), {})
    running = bot_running()
    if not running:
        status = {**status, "state": "stopped", "positions": []}
    return jsonify(
        login=login_state(), running=running, mode=cfg["MODE"], status=status, risk=risk,
        paper_feed=cfg.get("PAPER_FEED", "free"), needs_login=needs_login(cfg),
        backtest_done=os.path.exists(P("backtest_trades.csv")),
        instruments=cfg.get("TRADE_INSTRUMENTS", "NIFTY"), lots=cfg.get("LOTS", 1),
        limits={k: cfg[k] for k in ("MAX_DAILY_LOSS", "MAX_TRADES_PER_DAY", "MAX_CONSECUTIVE_LOSSES",
                                     "LOTS", "TRADE_INSTRUMENTS", "USE_STOCK_OPTIONS", "ITM_STRIKES", "RR",
                                     "USE_CRUDE_AGENT", "USE_NEWS_AGENT", "USE_TRADER_AGENT", "USE_GLOBAL_AGENT",
                                     "VOLUME_FILTER", "VOLUME_MIN_RVOL", "VIX_ADJUST_EXITS", "FVG_RR",
                                     "FVG_ENTRY_MODE", "FVG_SL_METHOD")},
        strategies=[dict(id=s.id, name=s.name, rr=s.rr(cfg)) for s in strategies.active(cfg)],
        has_key=bool(cfg.get("API_KEY")), has_secret=bool(cfg.get("API_SECRET")),
        redirect_url=f"{PUBLIC_URL}/callback", server_ip=server_ip(),
        stop_pending=os.path.exists(P("STOP")), now=datetime.now(IST).strftime("%H:%M:%S"),
        started=os.path.getmtime(P("bot.pid")) if running and os.path.exists(P("bot.pid")) else None,
        warmup_need=cfg.get("EMA_TREND", 50) + 10,
        scalp_running=scalp_running(), error=status.get("error") if running else None)


# ------------------------------------------------------------------ settings
@app.get("/api/settings")
def get_settings():
    cfg, dflt, saved = load_config(), default_config(), read_json(P(SETTINGS), {})
    fields = []
    for f in SCHEMA:
        k = f["key"]
        v = [s.id for s in strategies.active(cfg)] if k == "STRATEGIES" else cfg.get(k, "")
        fields.append(dict(f, value="" if f["type"] == "secret" else v,
                           default="" if f["type"] == "secret" else dflt.get(k, ""),
                           has_value=bool(cfg.get(k)), saved=k in saved,
                           choices=[dict(value=c, label=l) for c, l in f["choices"]] if f["choices"] else None))
    return jsonify(sections=[dict(id=i, title=t, desc=d) for i, t, d in SECTIONS], fields=fields, mode=cfg["MODE"],
                   strategies=[dict(id=s.id, name=s.name, short=s.short, tag=s.tag, blurb=s.blurb, section=s.section,
                                   live_flag=s.live_flag, rr_key=s.rr_key) for s in strategies.ALL],
                   running=bot_running() or scalp_running(), warnings=cfg.get("TIME_WARNINGS", []),
                   profiles=[dict(id=i, label=l, desc=d, values=v) for i, l, d, v in RISK_PROFILES],
                   best_mix=strategies.BEST_MIX,
                   redirect_url=f"{PUBLIC_URL}/callback")


@app.post("/api/settings")
def save_settings():
    if bot_running() or scalp_running():
        return jsonify(ok=False, error="Stop the bot and the scalper before changing settings."), 409
    data = request.get_json(force=True)
    saved = read_json(P(SETTINGS), {})
    errors = []
    for k, (typ, _, label) in EDITABLE.items():
        if k not in data:
            continue
        if typ == "multi":
            chosen = data[k] if isinstance(data[k], list) else []
            picked = [c for c, _ in next(f for f in SCHEMA if f["key"] == k)["choices"] if c in chosen]
            if not picked or len(picked) != len(set(chosen)):
                errors.append(f"{label}: choose at least one from the list"); continue
            saved[k] = picked
            saved.pop("STRATEGY", None)            # the old single choice
            continue
        raw = str(data[k]).strip()
        if k in ("API_SECRET", "WHATSAPP_APIKEY") and raw == "":
            continue  # blank = keep existing secret
        if typ == "bool":
            saved[k] = raw.lower() in ("true", "1", "on", "yes"); continue
        if isinstance(typ, tuple):
            if raw not in typ:
                errors.append(f"{label}: choose one of {', '.join(typ)}"); continue
            saved[k] = raw; continue
        try:
            val = typ(raw) if typ is not str else raw
        except ValueError:
            errors.append(f"{label}: not a valid number"); continue
        lo, hi = LIMITS.get(k, (None, None))
        if typ in (int, float) and ((lo is not None and val < lo) or (hi is not None and val > hi)):
            rng = f"between {lo:g} and {hi:g}" if lo is not None and hi is not None else f"at least {lo:g}"
            errors.append(f"{label}: must be {rng}"); continue
        if k in TIME_KEYS:
            try:
                datetime.strptime(val, "%H:%M")
            except ValueError:
                errors.append(f"{label}: use HH:MM"); continue
        saved[k] = val
    if errors:
        return jsonify(ok=False, error="; ".join(errors)), 400
    if saved.get("LOTS", 1) < 1:
        return jsonify(ok=False, error="Lots must be at least 1"), 400
    if not 0 <= saved.get("ITM_STRIKES", 1) <= 5:
        return jsonify(ok=False, error="Strikes in-the-money must be 0-5"), 400
    if saved.get("RR", 2) < 0.5:
        return jsonify(ok=False, error="Reward:risk must be at least 0.5"), 400
    if not 0 <= saved.get("TRADER_BLOCK_MARGIN", 0.35) <= 1:
        return jsonify(ok=False, error="Debate margin must be between 0 and 1"), 400
    mins = lambda v: (lambda h, m: h * 60 + m)(*map(int, v.split(":")))
    sq, last_in = mins(saved.get("SQUARE_OFF_AT", "15:10")), mins(saved.get("NO_ENTRY_AFTER", "13:30"))
    if sq > mins("15:20"):
        return jsonify(ok=False, error="Square off by 15:20 at the latest. Zerodha auto-squares F&O intraday "
                                       "positions at 15:26 for ₹50 + GST per order; after 15:30 the market is shut."), 400
    if last_in >= sq:
        return jsonify(ok=False, error="'No entry after' must be earlier than the square-off time"), 400
    if saved.get("MAX_OPEN_POSITIONS", 1) < 1:
        return jsonify(ok=False, error="Max open positions must be at least 1"), 400
    write_json(P(SETTINGS), saved)
    return jsonify(ok=True)


@app.post("/api/mode")
def set_mode():
    if bot_running() or scalp_running():
        return jsonify(ok=False, error="Stop the bot and the scalper before switching mode."), 409
    mode = request.get_json(force=True).get("mode")
    if mode not in ("paper", "live"):
        return jsonify(ok=False, error="bad mode"), 400
    saved = read_json(P(SETTINGS), {})
    saved["MODE"] = mode
    write_json(P(SETTINGS), saved)
    return jsonify(ok=True, mode=mode)


@app.post("/api/alerts/test")
def alerts_test():
    """Send a WhatsApp/Telegram test message now, with the saved settings, and show CallMeBot's reply."""
    from types import SimpleNamespace
    cfg = SimpleNamespace(**load_config())
    if not (notify.enabled(cfg) or notify.tg_enabled(cfg)):
        return jsonify(ok=False, error="No alert channel is on. Turn on Telegram (or WhatsApp) alerts, fill in the "
                                       "username, press Save, then try again."), 400
    return jsonify(ok=True, results=notify.test(cfg))


# ------------------------------------------------------------------ bot control
@app.post("/api/bot/start")
def start_bot():
    if bot_running():
        return jsonify(ok=False, error="Bot is already running."), 409
    cfg = load_config()
    if needs_login(cfg) and not login_state()["logged_in"]:
        return jsonify(ok=False, error="Log in to Zerodha first (required every day for live or Zerodha-feed paper)."), 400
    env = dict(os.environ)
    if cfg["MODE"] == "live":
        if request.get_json(force=True).get("confirm") != "YES":
            return jsonify(ok=False, error="Type YES to confirm live trading."), 400
        env["BOT_CONFIRM_LIVE"] = "YES"
    if os.path.exists(P("STOP")):
        os.remove(P("STOP"))
    launch("bot", env)
    return jsonify(ok=True)


@app.post("/api/bot/stop")
def stop_bot():
    if not bot_running():
        return jsonify(ok=True, msg="Bot was not running.")
    stop_later("bot", "STOP")  # bot exits any position, then quits
    return jsonify(ok=True, msg="Stopping — any open position is being exited.")


@app.post("/api/positions/exit")
def exit_position():
    """Queue the Exit button for the bot that holds this option. Stock options use /api/stocks/<sym>/action."""
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict) or body.get("bot") not in ("bot", "scalp"):
        return jsonify(ok=False, error="Say which bot holds the position."), 400
    scalp = body["bot"] == "scalp"
    if not (scalp_running() if scalp else bot_running()):
        return jsonify(ok=False, error="That bot isn't running, so it can't exit the position. Check Kite."), 409
    symbol = body.get("symbol")
    held = read_json(P("scalp_status.json" if scalp else "status.json"), {}).get("positions") or []
    if not symbol or symbol not in [p.get("symbol") for p in held]:
        return jsonify(ok=False, error=f"The bot doesn't hold {symbol or 'that option'} any more."), 409
    path = P(manual_exit.SCALP_FILE if scalp else manual_exit.BOT_FILE)
    if manual_exit.queued(path):
        return jsonify(ok=False, error="An exit is already waiting for the bot."), 409
    try:
        manual_exit.request(path, symbol)
    except OSError as e:
        return jsonify(ok=False, error=f"Could not send the exit to the bot: {e}"), 500
    return jsonify(ok=True, msg=f"Exiting {symbol}…"), 202


def _level(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise ValueError
    v = float(v)
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError
    return v


@app.post("/api/positions/levels")
def edit_levels():
    """Queue new stop-loss / target levels for a position the bot or scalper holds. The bot checks them against the
    live price before using them; GET this endpoint to see whether it did."""
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict) or body.get("bot") not in ("bot", "scalp"):
        return jsonify(ok=False, error="Say which bot holds the position."), 400
    scalp = body["bot"] == "scalp"
    if not (scalp_running() if scalp else bot_running()):
        return jsonify(ok=False, error="That bot isn't running, so it can't change the position."), 409
    try:
        levels = {k: _level(body.get(k)) for k in (("sl", "target") if scalp else manual_levels.FIELDS)}
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Enter the stop-loss and target as numbers."), 400
    levels = {k: v for k, v in levels.items() if v is not None}
    if not levels:
        return jsonify(ok=False, error="Nothing to change."), 400
    symbol = body.get("symbol")
    held = read_json(P("scalp_status.json" if scalp else "status.json"), {}).get("positions") or []
    p = next((x for x in held if x.get("symbol") == symbol), None) if symbol else None
    if not p:
        return jsonify(ok=False, error=f"The bot doesn't hold {symbol or 'that option'} any more."), 409
    if p.get("exit_pending"):
        return jsonify(ok=False, error=f"{symbol} is already being exited."), 409
    # early check on the last price the bot reported; the bot checks again on the live price before applying
    on_prem = scalp and p.get("on_prem")
    why = manual_levels.check(True if on_prem else p.get("side") == "CE", p.get("ltp") if on_prem else p.get("idx"),
                              levels.get("sl"), levels.get("target"), "option price" if on_prem else "index level")
    if not why and ("prem_sl" in levels or "prem_target" in levels):
        why = ("This trade has no option-price stop/target to edit." if p.get("prem_sl") is None else
               manual_levels.check(True, p.get("ltp"), levels.get("prem_sl"), levels.get("prem_target"), "option price"))
    if why:
        return jsonify(ok=False, error=why), 400
    path = P(manual_levels.SCALP_FILE if scalp else manual_levels.BOT_FILE)
    if manual_levels.queued(path):
        return jsonify(ok=False, error="An earlier change is still waiting for the bot."), 409
    try:
        manual_levels.request(path, symbol, levels)
    except OSError as e:
        return jsonify(ok=False, error=f"Could not send the change to the bot: {e}"), 500
    return jsonify(ok=True, msg=f"Updating {symbol}…"), 202


@app.get("/api/positions/levels")
def edit_levels_status():
    scalp = request.args.get("bot") == "scalp"
    cmd = manual_levels.read(P(manual_levels.SCALP_FILE if scalp else manual_levels.BOT_FILE))
    return jsonify(ok=True, symbol=cmd.get("symbol"), status=cmd.get("status"), msg=cmd.get("msg") or "",
                   created=cmd.get("created"))


@app.get("/api/log")
def get_log():
    return jsonify(log=today_tail(P("bot.log"), 300), console=tail(P("bot_console.log"), 60))


# ------------------------------------------------------------------ trades & pnl
@app.get("/api/trades")
def trades():
    mode = request.args.get("mode")
    rows = read_trades(P("trades.csv"))
    if mode in ("paper", "live"):
        rows = [r for r in rows if r.get("mode") == mode]
    return jsonify(trades=list(reversed(rows)))


def strategy_perf(rows):
    """Closed trades per strategy, from the journal. Trades from before the strategy column count as EMA cross."""
    out = {}
    for r in rows:
        sid = r.get("strategy") or "ema"
        d = out.setdefault(sid, dict(id=sid, name=getattr(strategies.BY_ID.get(sid), "name", sid), trades=0, wins=0,
                                     net=0.0, won=0.0, lost=0.0))
        n = r["net_pnl"]
        d["trades"] += 1; d["wins"] += n > 0; d["net"] += n
        d["won" if n > 0 else "lost"] += abs(n)
    for d in out.values():
        d["win_pct"] = round(100 * d["wins"] / d["trades"], 1)
        d["profit_factor"] = round(d["won"] / d["lost"], 2) if d["lost"] else None
        d["expectancy"] = round(d["net"] / d["trades"], 2)
        d["net"] = round(d["net"], 2)
    return out


@app.get("/api/strategy-perf")
def strategy_perf_api():
    mode = request.args.get("mode")
    rows = read_trades(P("trades.csv"))
    if mode in ("paper", "live"):
        rows = [r for r in rows if r.get("mode") == mode]
    return jsonify(strategy_perf(rows))


@app.get("/api/pnl")
def pnl():
    mode = request.args.get("mode")
    rows = read_trades(P("trades.csv"))
    if mode in ("paper", "live"):
        rows = [r for r in rows if r.get("mode") == mode]
    days = {}
    for r in rows:
        d = days.setdefault(r["date"], {"date": r["date"], "pnl": 0.0, "trades": 0, "wins": 0})
        d["pnl"] += r["net_pnl"]; d["trades"] += 1; d["wins"] += r["net_pnl"] > 0
    daily = sorted(days.values(), key=lambda d: d["date"])
    months = {}
    for d in daily:
        m = months.setdefault(d["date"][:7], {"month": d["date"][:7], "pnl": 0.0, "trades": 0, "wins": 0})
        m["pnl"] += d["pnl"]; m["trades"] += d["trades"]; m["wins"] += d["wins"]
    pnls = [r["net_pnl"] for r in rows]  # everything on this page is after estimated charges
    eq, peak, dd = 0, 0, 0
    for p in pnls:
        eq += p; peak = max(peak, eq); dd = min(dd, eq - peak)
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    summary = {
        "total": sum(pnls), "gross": sum(r["pnl"] for r in rows), "charges": sum(r["charges"] for r in rows),
        "trades": len(pnls), "wins": len(wins),
        "win_rate": len(wins) / len(pnls) * 100 if pnls else 0,
        "avg_win": sum(wins) / len(wins) if wins else 0,
        "avg_loss": sum(losses) / len(losses) if losses else 0,
        "best": max(pnls) if pnls else 0, "worst": min(pnls) if pnls else 0,
        "max_dd": dd,
        "best_day": max(daily, key=lambda d: d["pnl"]) if daily else None,
        "worst_day": min(daily, key=lambda d: d["pnl"]) if daily else None,
    }
    return jsonify(daily=daily, monthly=sorted(months.values(), key=lambda m: m["month"]),
                   summary=summary)


# ------------------------------------------------------------------ backtest
@app.post("/api/backtest")
def run_backtest():
    bt = bts.setdefault(g.user, dict(JOB))
    if bt["running"]:
        return jsonify(ok=False, error="A backtest is already running."), 409
    body = request.get_json(force=True)
    days = max(1, min(int(body.get("days", 30)), 90))
    lots = max(1, min(int(body.get("lots", 2)), 50))
    inst = body.get("instrument", "NIFTY")
    if inst not in ("NIFTY", "SENSEX", "BOTH"):
        inst = "NIFTY"
    bt.update(running=True, output="", started=datetime.now(IST).strftime("%H:%M:%S"), finished=None)
    cwd, env = accounts.user_dir(g.user), dict(os.environ, KITEBOT_SETTINGS=P(SETTINGS))  # the user's own settings
    creds = kite_creds()                    # a Zerodha session lets the backtest reach back past Yahoo's ~60 days
    if creds:
        env.update(KITEBOT_KITE_KEY=creds[1], KITEBOT_KITE_TOKEN=creds[2])

    def work():
        try:
            p = subprocess.run([sys.executable, os.path.join(BASE, "backtest.py"), "--days", str(days), "--lots", str(lots),
                                "--instrument", inst], cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
            bt["output"] = p.stdout + (("\n" + p.stderr[-3000:]) if p.returncode else "")
        except Exception as e:
            bt["output"] = f"Backtest failed: {e}"
        bt.update(running=False, finished=datetime.now(IST).strftime("%H:%M:%S"))
    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True)


def backtest_by_strategy(rows, selected=()):
    """The Backtest page's per-strategy table, in Settings order. A selected strategy that took no trades still gets
    its row. Files from before strategies were recorded have no name to give, so they stay apart instead of being
    credited to one."""
    perf = strategy_perf([dict(strategy=r.get("strategy") or "unknown", net_pnl=float(r.get("net") or 0)) for r in rows])
    if rows:
        for s in selected:
            perf.setdefault(s.id, dict(id=s.id, name=s.name, trades=0, wins=0, net=0.0, win_pct=0.0, profit_factor=None, expectancy=0.0))
    order = [s.id for s in strategies.ALL] + ["unknown"]
    out = [perf[k] for k in order if k in perf]
    for d in out:
        if d["id"] == "unknown":
            d["name"] = "Not recorded"
    return out


@app.get("/api/backtest")
def backtest_result():
    bt = bts.get(g.user, JOB)
    rows = []
    if not bt["running"] and os.path.exists(P("backtest_trades.csv")):
        with open(P("backtest_trades.csv")) as f:
            rows = list(csv.DictReader(f))
    for r in rows:
        spec = strategies.BY_ID.get(r.get("strategy"))
        r["strategy_name"] = spec.name if spec else ""
    return jsonify(**bt, trades=rows, by_strategy=backtest_by_strategy(rows, strategies.active(load_config())))


# ------------------------------------------------------------------ agents
agent_busy = {"v": False}
agent_lock = threading.Lock()          # one refresh at a time (manual or automatic)
AUTO_EVERY = 60                        # seconds between checks; each agent refetches only when its data is due
auto = {"last": None, "next": None, "errors": []}


def run_refresh(cfg, force):
    if not agent_lock.acquire(blocking=False):
        return
    agent_busy["v"] = True
    try:
        from agents import pipeline
        return pipeline.refresh(cfg, force=force)
    finally:
        agent_busy["v"] = False
        agent_lock.release()


def auto_refresh_loop():
    """Keeps agent readings fresh while the panel is open. Each agent still refetches only when its
    own interval is up (crude 3 min, technical 5, news 10, sentiment 15, fundamentals 12 h). The agents
    are shared by all users; while anyone's bot runs it refreshes them itself, so this loop stands aside
    to avoid double requests. It runs if any user has auto-refresh on."""
    time.sleep(5)
    while True:
        try:
            wanted = [c for c in (load_config(u) for u in accounts.ids()) if c.get("AGENT_AUTO_REFRESH", True)]
            if wanted and not any_bot_running():
                auto["errors"] = run_refresh(wanted[0], force=False) or []
                auto["last"] = time.time()
        except Exception as e:
            auto["errors"] = [repr(e)[:150]]
        auto["next"] = time.time() + AUTO_EVERY
        time.sleep(AUTO_EVERY)


@app.get("/api/agents")
def agents_get():
    from agents import load_cache, pipeline
    c = load_cache()
    cfg = load_config()
    deb, dec = pipeline.stance(cfg)
    return jsonify(**{k: c.get(k) for k, *_ in pipeline.ANALYSTS}, busy=agent_busy["v"],
                   on={k: bool(cfg.get(flag)) for k, _, flag, _ in pipeline.ANALYSTS},
                   trader_on=bool(cfg.get("USE_TRADER_AGENT")), debate=deb, trader=dec,
                   decisions=(c.get("decisions") or {}).get("items", []),
                   auto=dict(on=bool(cfg.get("AGENT_AUTO_REFRESH", True)), by_bot=any_bot_running(),
                             last=auto["last"], next=auto["next"], errors=auto["errors"], now=time.time()),
                   crude_on=cfg["USE_CRUDE_AGENT"], news_on=cfg["USE_NEWS_AGENT"])


@app.post("/api/agents/refresh")
def agents_refresh():
    if agent_busy["v"]:
        return jsonify(ok=True)
    threading.Thread(target=run_refresh, args=(load_config(), True), daemon=True).start()
    return jsonify(ok=True)


# ------------------------------------------------------------------ next trade preview
def _next_expiry(weekday, now):
    """Next weekly expiry on `weekday` (Mon=0), rolling past today after 15:30. Ignores holidays."""
    from datetime import timedelta, time as dtime
    d = now.date() + timedelta(days=(weekday - now.weekday()) % 7)
    if d == now.date() and now.time() > dtime(15, 30):
        d += timedelta(days=7)
    return datetime.combine(d, dtime(15, 30), tzinfo=IST)


def _trade_funds(cfg):
    """The funds the bot would size a trade from now. None in live mode when Zerodha can't say."""
    if cfg["MODE"] != "live":
        return cfg["PAPER_FUNDS"]
    if not login_state()["logged_in"]:
        return None
    try:
        return kite_session(cfg).margins("equity")["net"]
    except Exception:
        return None


@app.get("/api/next-trade")
def next_trade():
    """The option the main bot would buy on its next signal, priced now, and what still has to line up.
    Premiums are Black-Scholes estimates (IV = India VIX), like the free paper feed."""
    import strategy
    from feed import bs_price
    from agents import load_cache, market, pipeline
    cfg, now = load_config(), datetime.now(IST)
    c = load_cache()
    _, dec = pipeline.stance(cfg)
    lean = "CE" if "call" in dec.get("stance", "") else "PE" if "put" in dec.get("stance", "") else None
    idx = {r["key"]: r for r in live_indices()["indices"]}
    vix = (idx.get("vix") or {}).get("price") or 13.0
    traded = ["NIFTY", "SENSEX"] if cfg["TRADE_INSTRUMENTS"] == "BOTH" else [cfg["TRADE_INSTRUMENTS"]]
    names = traded + [n for n in ("NIFTY", "SENSEX") if n not in traded]   # always preview both; traded first
    active = strategies.active(cfg)
    rrs = list(dict.fromkeys(s.rr(cfg) for s in active))
    rr = rrs[0] if len(rrs) == 1 else " / ".join(f"{v:g}" for v in rrs)
    funds = _trade_funds(cfg)
    out = []
    for name in names:
        spec, spot = cfg["INSTRUMENT_SPECS"][name], (idx.get(name.lower()) or {}).get("price")
        if not spot:
            out.append(dict(index=name, traded=name in traded, error="no index price right now")); continue
        exp = _next_expiry(spec["expiry_weekday"], now)
        t = (exp - now).total_seconds() / (365 * 24 * 3600)
        try:
            ch = market.chart(spec["yahoo"], "5m", "5d", max_age=120)
            a = strategy.atr([dict(h=h, l=l, c=cl) for h, l, cl in zip(ch["h"], ch["l"], ch["c"])][-100:], 14)
        except Exception:
            a = None
        sides = []
        for side in ("CE", "PE"):
            strike = strategy.itm_strike(spot, side, spec["step"], cfg["ITM_STRIKES"])
            prem = round(max(0.05, bs_price(spot, strike, t, vix / 100, side)), 2)
            lots = cfg["LOTS"] if funds is None else strategy.lots_for_funds(
                funds, prem, spec["lot"], cfg["LOTS"], cfg["FUNDS_USE_PCT"], cfg["MAX_PREMIUM_PER_TRADE"])
            qty = lots * spec["lot"]
            cost = prem * (qty or spec["lot"])          # with 0 lots: what the one lot it can't afford costs
            sides.append(dict(side=side, strike=strike, symbol=f"{name} {exp:%d %b} {strike:g} {side}", premium=prem,
                              lots=lots, qty=qty, cost=round(cost), over_cap=bool(cfg["MAX_PREMIUM_PER_TRADE"]) and cost > cfg["MAX_PREMIUM_PER_TRADE"],
                              veto=strategy.agent_veto(side, c.get("crude"), c.get("news"), cfg, c.get("global"))))
        out.append(dict(index=name, traded=name in traded, spot=spot, expiry=exp.strftime("%a %d %b"), lot=spec["lot"], sides=sides,
                        stop_pts=[round(0.6 * a), round(1.5 * a)] if a else None))
    risk = read_json(P("state.json"), {})
    if risk.get("date") != today():
        risk = {"trades": 0, "consec_losses": 0, "locked": False}
    mins = lambda v: int(v[:2]) * 60 + int(v[3:5])
    t_now = now.hour * 60 + now.minute
    in_window = now.weekday() < 5 and mins(cfg["NO_ENTRY_BEFORE"]) <= t_now <= mins(cfg["NO_ENTRY_AFTER"])
    checks = [
        dict(name="Bot running", ok=bot_running(), note="Start the bot on this page" if not bot_running() else cfg["MODE"] + " mode"),
        dict(name="Entry window", ok=in_window, note=f"{cfg['NO_ENTRY_BEFORE']}–{cfg['NO_ENTRY_AFTER']} IST, weekdays"),
        dict(name="Risk limits", ok=not risk.get("locked") and risk.get("trades", 0) < cfg["MAX_TRADES_PER_DAY"]
             and risk.get("consec_losses", 0) < cfg["MAX_CONSECUTIVE_LOSSES"],
             note=risk.get("reason") or f"{risk.get('trades', 0)} of {cfg['MAX_TRADES_PER_DAY']} trades used today"),
    ]
    return jsonify(lean=lean, stance=dec.get("stance"), conviction=dec.get("conviction"), signal_names=[s.signal_name for s in active],
                   rr=rr, lots=cfg["LOTS"], funds=funds, funds_pct=cfg["FUNDS_USE_PCT"], cap=cfg["MAX_PREMIUM_PER_TRADE"], vix=vix, items=out, checks=checks,
                   at=now.strftime("%H:%M:%S"))


# ------------------------------------------------------------------ stock analysis (Stock trades page)
@app.get("/api/stocks")
def stocks_list():
    import stocks
    if request.args.get("view") == "combined":
        return jsonify(stocks=stocks.combined_overview(load_config()), view="combined")
    return jsonify(stocks=stocks.overview(), view="day")


@app.get("/api/stocks/<sym>")
def stock_detail(sym):
    import stocks
    sym = sym.upper()
    if sym not in stocks.BY_SYM:
        return jsonify(ok=False, error="Not one of the top 15 Nifty stocks."), 404
    try:
        return jsonify(stocks.analyse(sym, load_config()))
    except Exception as e:
        return jsonify(ok=False, error=f"Couldn't analyse {sym}: {str(e)[:120]}"), 502


@app.get("/api/stock-options/state")
def stock_options_state():
    cfg = load_config()
    status = read_json(P("status.json"), {})
    return jsonify(enabled=bool(cfg.get("USE_STOCK_OPTIONS")), running=bot_running(),
                   mode=cfg["MODE"], stock_options=status.get("stock_options") or {},
                   command=read_json(P("stock_command.json"), {}))


@app.post("/api/stocks/<sym>/action")
def stock_options_action(sym):
    import stocks
    sym = sym.upper()
    if sym not in stocks.BY_SYM:
        return jsonify(ok=False, error="Not one of the top 15 Nifty stocks."), 404
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify(ok=False, error="A JSON object is required."), 400
    action = body.get("action")
    if action not in ("buy", "sell"):
        return jsonify(ok=False, error="Action must be buy or sell-to-close."), 400
    cfg = load_config()
    if not bot_running():
        return jsonify(ok=False, error="Start the main bot first; stock actions use its risk and order manager."), 409
    if action == "buy" and not cfg.get("USE_STOCK_OPTIONS"):
        return jsonify(ok=False, error="Enable Auto-trade stock options in Settings first."), 409
    if action == "sell":
        position = (read_json(P("status.json"), {}).get("stock_options") or {}).get("position") or {}
        if position.get("symbol") != sym:
            return jsonify(ok=False, error=f"No managed {sym} stock-option position is open."), 409
    path = P("stock_command.json")
    current = read_json(path, {})
    if current.get("status") in ("pending", "processing"):
        return jsonify(ok=False, error="A stock-option action is already being processed."), 409
    command = dict(id=secrets.token_hex(8), action=action, symbol=sym, status="pending",
                   created=datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"))
    try:
        write_json(path, command)
    except OSError as e:
        return jsonify(ok=False, error=f"Could not queue stock-option action: {e}"), 500
    return jsonify(ok=True, command_id=command["id"], status="pending"), 202


# ------------------------------------------------------------------ index chart with the strategy on it
@app.get("/api/index-chart")
def index_chart_get():
    import index_chart
    index = request.args.get("index", "NIFTY").upper()
    tf = 5 if request.args.get("tf") == "5" else 15
    if index not in index_chart.SPECS:
        return jsonify(ok=False, error="Index must be NIFTY or SENSEX."), 400
    try:
        cfg = load_config()
        try:
            with open(P("bot.log"), errors="replace") as f:
                lines = f.readlines()[-20000:]
        except OSError:
            lines = []
        # signals the bot saw and didn't take (agent HOLD / veto / low volume) open no position in the replay
        j = index_chart.build(index, tf, cfg, kite_creds(), blocks=index_chart.bot_blocks(lines, index))
        j["today_signals"] = index_chart.bot_actions(j, lines, cfg)
        return jsonify(j)
    except Exception as e:
        return jsonify(ok=False, error=f"Couldn't build the {index} chart: {str(e)[:120]}"), 502


# ------------------------------------------------------------------ live market data
LIVE_INDICES = [  # key, name, Kite symbol, Yahoo symbol, exchange
    ("nifty", "Nifty 50", "NSE:NIFTY 50", "^NSEI", "NSE"),
    ("sensex", "Sensex", "BSE:SENSEX", "^BSESN", "BSE"),
    ("banknifty", "Bank Nifty", "NSE:NIFTY BANK", "^NSEBANK", "NSE"),
    ("vix", "India VIX", "NSE:INDIA VIX", "^INDIAVIX", "NSE"),
]
live_cache = {"at": 0, "data": None}
gift_hist = []                         # GIFT Nifty prices seen while the panel is open (no free intraday series)
live_lock = threading.Lock()
import live
STREAM = live.Stream(lambda: kite_creds())   # one shared Zerodha WebSocket for index ticks


QUOTE_DENIED_MSG = ("this Kite API app has no market data access. The free Personal plan doesn't include it; "
                    "use a paid Connect app (developers.kite.trade) with its API key in Settings, then log in again")
QUOTE_DENIED_RETRY = 600               # seconds between checks, so an upgrade is noticed without a restart
quote_denied = {}                      # access token -> when to ask Zerodha again


def _kite_quotes():
    """Real-time index quotes from Kite when a panel user is logged in today: ({key: quote}, error or None)."""
    c = kite_creds()
    if not c:
        return {}, None
    if time.time() < quote_denied.get(c[2], 0):
        return {}, QUOTE_DENIED_MSG
    from kiteconnect import KiteConnect
    try:
        k = KiteConnect(api_key=c[1])
        k.set_access_token(c[2])
        q = k.quote([s for _, _, s, _, _ in LIVE_INDICES])
    except Exception as e:
        if type(e).__name__ == "PermissionException":   # the plan refuses it; retrying every 10 s can't help
            quote_denied[c[2]] = time.time() + QUOTE_DENIED_RETRY
            return {}, QUOTE_DENIED_MSG
        return {}, f"Zerodha quotes failed: {str(e)[:120]}"
    out = {}
    for key, _, sym, _, _ in LIVE_INDICES:
        d = q.get(sym)
        if d and d.get("last_price"):
            prev = (d.get("ohlc") or {}).get("close") or None
            out[key] = dict(price=d["last_price"], prev_close=prev, source="Zerodha (live)", as_of=time.time(),
                            high=(d.get("ohlc") or {}).get("high"), low=(d.get("ohlc") or {}).get("low"))
    return out, None


def _yahoo_quote(sym):
    from agents import market
    d = market.chart(sym, "1m", "1d", max_age=10)
    daily = market.chart(sym, "1d", "5d", max_age=300)
    day = datetime.fromtimestamp(d["t"][-1], IST).date()
    prev = [c for t, c in zip(daily["t"], daily["c"]) if datetime.fromtimestamp(t, IST).date() < day]
    return dict(price=d["c"][-1], prev_close=prev[-1] if prev else None, high=max(d["h"]), low=min(d["l"]),
                source="Yahoo (~1 min delay)", as_of=d["t"][-1], spark=_spark(d["c"]))


def _spark(closes, n=60):
    """Today's closes thinned to about n points for a sparkline."""
    step = max(1, len(closes) // n)
    pts = closes[::step]
    if pts and pts[-1] != closes[-1]:
        pts.append(closes[-1])
    return [round(c, 2) for c in pts]


def live_indices():
    """Nifty / Sensex / Bank Nifty / India VIX now, plus GIFT Nifty. Prices from the Zerodha stream when it
    is live (tick by tick), else Kite quotes, else Yahoo; the slower parts are cached 10 s."""
    STREAM.ensure()
    base, ticks = _indices_base(), STREAM.latest()
    zerodha = dict(STREAM.status(), quote_error=base.get("quote_error"), logged_in=bool(kite_creds()))
    if not ticks:
        return dict(base, zerodha=zerodha)
    rows = []
    for r in base["indices"]:
        t = ticks.get(r["key"])
        if t:
            r = dict(r, **{k: v for k, v in t.items() if v is not None}, source="Zerodha (streaming)")
            r.pop("error", None)
            if r.get("prev_close"):
                r["change"] = round(r["price"] - r["prev_close"], 2)
                r["change_pct"] = round(r["change"] / r["prev_close"] * 100, 2)
            if r.get("spark"):
                r["spark"] = r["spark"][:-1] + [round(r["price"], 2)]
        rows.append(r)
    nifty, gift = (next((r.get("price") for r in rows if r["key"] == k), None) for k in ("nifty", "gift"))
    return dict(base, indices=rows, gift_premium=round(gift - nifty, 1) if nifty and gift else None, zerodha=zerodha)


def _indices_base():
    """Cached 10 s. When the copy is older, it is still returned at once and refreshed in the background
    (stale-while-revalidate), so no panel request ever waits on Yahoo except the very first one."""
    d = live_cache["data"]
    if d and time.time() - live_cache["at"] < 10:
        return d
    if d:
        if not live_cache.get("busy"):
            live_cache["busy"] = True
            threading.Thread(target=_indices_refresh, daemon=True).start()
        return d
    return _indices_refresh()


def _indices_refresh():
    try:
        return _indices_fetch()
    finally:
        live_cache["busy"] = False


def _indices_fetch():
    with live_lock:
        kq, qerr = ({}, None) if STREAM.connected else _kite_quotes()
        rows = []
        for key, name, _, ysym, exch in LIVE_INDICES:
            q = kq.get(key)
            if not q:
                try:
                    q = _yahoo_quote(ysym)
                except Exception as e:
                    q = dict(error=str(e)[:80])
            elif "spark" not in q:
                try:
                    from agents import market
                    q["spark"] = _spark(market.chart(ysym, "1m", "1d", max_age=60)["c"])
                except Exception:
                    pass
            rows.append(dict(key=key, name=name, exchange=exch, **q))
        try:
            from agents import global_markets
            g = global_markets.moneycontrol().get("GIFT NIFTY")
            if g:
                if not gift_hist or gift_hist[-1] != g["price"]:
                    gift_hist.append(g["price"])
                    del gift_hist[:-120]
                rows.insert(2, dict(key="gift", name="GIFT Nifty", exchange="NSE IX", price=g["price"], prev_close=g["prev_close"],
                                    high=g.get("high"), low=g.get("low"), source="Moneycontrol", as_of=g.get("updated"),
                                    spark=list(gift_hist) if len(gift_hist) > 1 else None))
        except Exception as e:
            rows.insert(2, dict(key="gift", name="GIFT Nifty", error=str(e)[:80]))
        for r in rows:
            if r.get("price") is not None and r.get("prev_close"):
                r["change"] = round(r["price"] - r["prev_close"], 2)
                r["change_pct"] = round(r["change"] / r["prev_close"] * 100, 2)
        nifty, gift = (next((r.get("price") for r in rows if r["key"] == k), None) for k in ("nifty", "gift"))
        live_cache.update(at=time.time(), data=dict(indices=rows, gift_premium=round(gift - nifty, 1) if nifty and gift else None,
                                                    quote_error=qerr))
        return live_cache["data"]


@app.get("/api/markets")
def markets_get():
    """Live index strip + global markets table. Hours/status are recomputed every call so countdowns
    stay current; prices come from the global agent's last reading."""
    from agents import global_markets, load_cache
    g = load_cache().get("global") or {}
    hours = global_markets.sessions()
    rows = [dict(r, **hours.get(r["key"], {})) for r in g.get("markets") or []] or list(hours.values())
    return jsonify(**live_indices(), markets=rows, macro=g.get("macro") or [], read=dict(
        ok=g.get("ok"), score=g.get("score"), label=g.get("label"), note=g.get("note"), bull=g.get("bull") or [],
        bear=g.get("bear") or [], fetched_at=g.get("fetched_at")), on=bool(load_config().get("USE_GLOBAL_AGENT")),
        now=datetime.now(IST).strftime("%H:%M:%S"))


# ------------------------------------------------------------------ market strength page
import market_strength as ms
import market_strength_feed as msf
MS_FILE = "market_strength.json"          # the user's applied weights (version, timestamp) in their own folder


def _ms_companies():
    import stocks
    return [n for _, n, _, _ in stocks.TOP15] + [s for s, _, _, _ in stocks.TOP15 if len(s) > 3]


def market_strength_payload(conf=None):
    """Gauges, parameter table, EMA/FVG alignment, levels, risk and feed status for the Market Strength page.
    Reads the shared feeds only; never trades."""
    cfg = load_config()
    conf = conf or ms.load(P(MS_FILE), read_json)
    li = live_indices()
    quotes = {r["key"]: r for r in li.get("indices") or []}
    creds = kite_creds()
    inp = msf.gather(creds, cfg, quotes)
    res = msf.evaluate(inp, conf, companies=_ms_companies())
    sig = msf.signals(creds, cfg)
    comb = res["combined"]["score"]
    align = []
    for sid in ("ema", "fvg"):
        s = sig.get(sid) or {}
        a = ms.alignment(s.get("side"), comb) if not s.get("error") else dict(key="unknown", text="Signal unavailable", why=s["error"])
        align.append(dict(s, id=sid, alignment=a, combined=comb))
    news = (inp.get("agents") or {}).get("news") or {}
    if (time.time() - (news.get("fetched_at") or 0) > 900) and not agent_busy["v"]:
        threading.Thread(target=run_refresh, args=(cfg, False), daemon=True).start()   # existing agent refresh, cached
    risk = read_json(P("state.json"), {})
    if risk.get("date") != today():
        risk = {"pnl": 0, "trades": 0, "consec_losses": 0, "locked": False, "reason": ""}
    der = inp.get("derivatives") or {}
    feeds = [
        dict(name="Zerodha stream", ok=bool(li.get("zerodha", {}).get("streaming")),
             detail=li.get("zerodha", {}).get("error") or ("live ticks" if li.get("zerodha", {}).get("streaming") else "not streaming — Kite quotes or Yahoo used")),
        dict(name="Nifty candles", ok=bool(inp["candles_meta"].get("as_of")), detail=inp["candles_meta"].get("source") or inp["candles_meta"].get("error")),
        dict(name="Futures & options (Zerodha)", ok=not der.get("error"), detail=der.get("error") or f"expiry {der.get('expiry')}"),
        dict(name="News feed", ok=bool(news.get("ok")), detail=news.get("note") or "no news yet"),
        dict(name="FII/DII", ok=False, detail="no feed connected"),
    ]
    return dict(res, config=conf, defaults=ms.defaults(), labels=dict(technical=[[k, lab] for k, lab, _ in ms.TECHNICAL], news=[[k, lab] for k, lab, _ in ms.NEWS]),  # lists keep the order
                thresholds=dict(bear_below=ms.BEAR_BELOW, bull_above=ms.BULL_ABOVE, min_coverage=ms.MIN_COVERAGE),
                alignment=align, feeds=feeds, market_open=inp.get("market_open"),
                risk=dict(risk, running=bot_running(), mode=cfg.get("MODE"), max_loss=cfg.get("MAX_DAILY_LOSS"),
                          max_trades=cfg.get("MAX_TRADES_PER_DAY"), max_consec=cfg.get("MAX_CONSECUTIVE_LOSSES")),
                now=time.time())


@app.get("/api/market-strength")
def market_strength_get():
    try:
        return jsonify(ok=True, **market_strength_payload())
    except Exception as e:
        return jsonify(ok=False, error=f"Market strength unavailable: {str(e)[:160]}"), 502


@app.post("/api/market-strength/config")
def market_strength_apply():
    """Validate and apply new weights (technical 100%, news 100%, ratio 100%); saved in the user's folder with a
    version and timestamp. Returns the recalculated page."""
    body = request.get_json(force=True, silent=True) or {}
    conf, err = ms.apply(P(MS_FILE), read_json, write_json, body, who=g.get("user"))
    if err:
        return jsonify(ok=False, errors=err), 400
    try:
        return jsonify(ok=True, applied=True, **market_strength_payload(conf))
    except Exception as e:
        return jsonify(ok=True, applied=True, config=conf, error=f"Saved, but recalculation failed: {str(e)[:120]}")


@app.get("/api/stream")
def stream():
    """Server-sent events: index prices pushed as they change (every Zerodha tick, at most ~2 a second;
    every 5 s on the Yahoo fallback), plus a heartbeat every 15 s so proxies keep the connection open."""
    keys = ("key", "price", "prev_close", "change", "change_pct", "high", "low", "source", "as_of", "error")

    def gen():
        v, last, sent = -1, None, 0.0
        yield "retry: 3000\n\n"
        while True:
            try:
                d = live_indices()
            except Exception as e:
                d = dict(indices=[], zerodha=dict(streaming=False, error=str(e)[:100]))
            body = json.dumps(dict(indices=[{k: r.get(k) for k in keys} for r in d["indices"]],
                                   gift_premium=d.get("gift_premium"), zerodha=d.get("zerodha")))
            if body != last or time.time() - sent > 15:
                yield f"data: {body}\n\n"
                last, sent = body, time.time()
            if STREAM.connected:
                v = STREAM.wait(v, 15)
                time.sleep(0.4)
            else:
                time.sleep(5)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ scalper
def today_scalps():
    st = read_json(P("scalp_state.json"), {})
    return st if st.get("date") == today() else {"date": today(), "pnl": 0, "trades": 0, "consec_losses": 0,
                                                   "locked": False, "reason": ""}


@app.get("/api/scalp/state")
def scalp_state():
    cfg = load_config()
    status = read_json(P("scalp_status.json"), {})
    running = scalp_running()
    if not running:
        status = {**status, "state": "stopped", "positions": []}
    rows = read_trades(P("scalp_trades.csv"))
    mode = cfg["MODE"]
    mine = [r for r in rows if r.get("mode") == mode]
    return jsonify(running=running, mode=mode, status=status, risk=today_scalps(),
                   allow_live=bool(cfg.get("SCALP_ALLOW_LIVE")), stop_pending=os.path.exists(P("STOP_SCALP")),
                   needs_login=needs_login(cfg), logged_in=login_state()["logged_in"],
                   rules=dict(on=cfg.get("SCALP_POINTS_ON"), sl=cfg.get("SCALP_SL_POINTS"), tg=cfg.get("SCALP_TARGET_POINTS"),
                              be=cfg.get("SCALP_BE_AT_POINTS"), lots=cfg.get("SCALP_LOTS"), candle=cfg.get("SCALP_CANDLE_MINUTES"),
                              hold=cfg.get("SCALP_MAX_HOLD_MIN"), max_trades=cfg.get("SCALP_MAX_TRADES_PER_DAY"),
                              max_loss=cfg.get("SCALP_MAX_DAILY_LOSS"), streak=cfg.get("SCALP_MAX_CONSECUTIVE_LOSSES"),
                              window=f"{cfg.get('SCALP_NO_ENTRY_BEFORE')}–{cfg.get('SCALP_NO_ENTRY_AFTER')}",
                              strategy=cfg.get("SCALP_STRATEGY"), inst=cfg.get("TRADE_INSTRUMENTS", "NIFTY"),
                              dynamic=bool(cfg.get("SCALP_DYNAMIC_SL")) and cfg.get("SCALP_STRATEGY") == "v2",
                              be_r=cfg.get("SCALP_BE_AT_R")),
                   total=dict(trades=len(mine), pnl=sum(r["pnl"] for r in mine),
                              wins=sum(1 for r in mine if r["pnl"] > 0)))


@app.post("/api/scalp/start")
def scalp_start():
    if scalp_running():
        return jsonify(ok=False, error="The scalper is already running."), 409
    cfg = load_config()
    if needs_login(cfg) and not login_state()["logged_in"]:
        return jsonify(ok=False, error="Log in to Zerodha first (required every day for live or Zerodha-feed paper)."), 400
    env = dict(os.environ)
    if cfg["MODE"] == "live":
        if not cfg.get("SCALP_ALLOW_LIVE"):
            return jsonify(ok=False, error="Live scalping is off. The backtest loses money with real fills — turn on "
                                           "'Allow live scalping' in Settings → Scalper only if you accept that."), 400
        if request.get_json(force=True).get("confirm") != "YES":
            return jsonify(ok=False, error="Type YES to confirm live scalping."), 400
        env["BOT_CONFIRM_LIVE"] = "YES"
    if os.path.exists(P("STOP_SCALP")):
        os.remove(P("STOP_SCALP"))
    launch("scalp", env)
    return jsonify(ok=True)


@app.post("/api/scalp/stop")
def scalp_stop():
    if not scalp_running():
        return jsonify(ok=True, msg="The scalper was not running.")
    stop_later("scalp", "STOP_SCALP")
    return jsonify(ok=True, msg="Stopping — any open scalp is being exited.")


@app.get("/api/scalp/trades")
def scalp_trades():
    mode = request.args.get("mode")
    rows = read_trades(P("scalp_trades.csv"))
    if mode in ("paper", "live"):
        rows = [r for r in rows if r.get("mode") == mode]
    return jsonify(trades=list(reversed(rows)))


@app.get("/api/scalp/log")
def scalp_log():
    return jsonify(log=today_tail(P("scalp.log"), 200), console=tail(P("scalp_console.log"), 40))


@app.post("/api/scalp/backtest")
def scalp_backtest_run():
    sbt = sbts.setdefault(g.user, dict(JOB))
    if sbt["running"]:
        return jsonify(ok=False, error="A scalp backtest is already running."), 409
    body = request.get_json(force=True)
    inst = body.get("instrument", "NIFTY")
    inst = inst if inst in ("NIFTY", "SENSEX", "BOTH") else "NIFTY"
    lots = max(1, min(int(body.get("lots", 1)), 50))
    strat = body.get("strategy", "both")
    strat = strat if strat in ("ema", "volume", "both", "v2") else "both"
    sbt.update(running=True, output="", started=datetime.now(IST).strftime("%H:%M:%S"), finished=None)
    cwd, env = accounts.user_dir(g.user), dict(os.environ, KITEBOT_SETTINGS=P(SETTINGS))

    def work():
        try:
            cmd = ([sys.executable, os.path.join(BASE, "scalp_v2_backtest.py"), "--quick", "--instrument", inst] if strat == "v2" else
                   [sys.executable, os.path.join(BASE, "scalp_backtest.py"), "--instrument", inst, "--lots", str(lots), "--strategy", strat])
            p = subprocess.run(cmd,
                               cwd=cwd, env=env, capture_output=True, text=True, timeout=900)
            lines = [l for l in p.stdout.splitlines() if "delisted" not in l and "Failed download" not in l]
            sbt["output"] = "\n".join(lines) + (("\n" + p.stderr[-2000:]) if p.returncode else "")
        except Exception as e:
            sbt["output"] = f"Scalp backtest failed: {e}"
        sbt.update(running=False, finished=datetime.now(IST).strftime("%H:%M:%S"))
    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/scalp/backtest")
def scalp_backtest_result():
    return jsonify(**sbts.get(g.user, JOB))


# ------------------------------------------------------------------ strategy lab
lab = {"running": False, "output": "", "started": None, "finished": None}
LAB_RESULTS = os.path.join("reports", "scalp_lab", "results.json")


@app.get("/api/lab")
def lab_get():
    res = read_json(LAB_RESULTS, None)
    if res:
        res.get("walk_forward", {}).pop("rows", None)
    return jsonify(results=res, **lab)


@app.post("/api/lab/run")
def lab_run():
    if lab["running"]:
        return jsonify(ok=False, error="The lab is already running."), 409
    lab.update(running=True, output="", started=datetime.now(IST).strftime("%H:%M:%S"), finished=None)

    def work():
        try:
            p = subprocess.run([sys.executable, "-W", "ignore", "-m", "scalp_lab.experiments"], cwd=BASE,
                               capture_output=True, text=True, timeout=1800)
            lab["output"] = (p.stdout + p.stderr)[-6000:]
        except Exception as e:
            lab["output"] = f"Lab failed: {e!r}"
        lab.update(running=False, finished=datetime.now(IST).strftime("%H:%M:%S"))
    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True)


# ------------------------------------------------------------------ shutdown
@app.post("/api/shutdown")
def shutdown():
    busy = [accounts.name_of(u) for u in accounts.ids() if bot_running(u) or scalp_running(u)]
    if busy:
        return jsonify(ok=False, error="Stop every bot and scalper first. Still running for: " + ", ".join(busy)), 409
    threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    return jsonify(ok=True)


if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}/"
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    threading.Thread(target=auto_refresh_loop, daemon=True).start()
    threading.Thread(target=live_indices, daemon=True).start()   # warm the index cache before the first page load
    print(f"\n  Kite Bot control panel: {url}")
    ip = os.environ.get("KITEBOT_HOST_IP") or lan_ip()  # in Docker, lan_ip() is the container's own IP
    if os.environ.get("KITEBOT_PUBLIC_URL"):
        print(f"  Online at: {PUBLIC_URL}/")
    elif ip:
        print(f"  Phone link (same Wi-Fi): http://{ip}:{PORT}/")
    print("  Keep this window open. Close it to shut everything down.\n")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
