# ============================================================
#  BOT SETTINGS — defaults. Values saved from the web app (Settings page)
#  go to settings.json and override these; you normally never edit this file.
# ============================================================
import json, os

MODE = "paper"                    # "paper" (simulated orders) or "live" (real money)
PAPER_FEED = "free"               # paper prices: "free" (no keys; Yahoo index + modelled options)
                                  #               "zerodha" (real option prices; needs API key + login)

# ---- Kite Connect credentials (stored only on this computer) ----
API_KEY = "jq7wtgsmp9tj7uju"
API_SECRET = "hm68ui22rwwk2th2r1ufnbtn5pjbvbsw"

# ---- What to trade ----
TRADE_INSTRUMENTS = "NIFTY"       # "NIFTY", "SENSEX" or "BOTH"
BOTH_SAME_SIGNAL = "sensex"       # trading both, same call on both on one candle: "sensex" = Sensex only, skip Nifty;
                                  # "split" = half the lots on each; "off" = first processed takes it (pairing.py)
USE_STOCK_OPTIONS = False         # scan top Nifty stocks for long-option setups while the main bot runs
LOTS = 1                          # most lots per trade; funds decide the actual number (Nifty lot 65, Sensex lot 20)
FUNDS_USE_PCT = 90                # a trade may spend this % of the funds available to trade
PAPER_FUNDS = 30000               # paper mode has no real account: the funds it sizes trades against (₹)
ITM_STRIKES = 1                   # strikes in-the-money (1 = one strike ITM, 2 = two strikes ITM)
MAX_OPEN_POSITIONS = 1            # Nifty & Sensex move together — 1 avoids doubling the same bet
CANDLE_MINUTES = 5
STRATEGIES = ["ema"]              # main bot entry rules, any mix (strategies.py); the first signal takes the position:
                                  #   "ema" = strategy v2 (EMA cross, strategy.py)
                                  #   "fvg" = Fair Value Gap retest (fvg.py, settings below)
                                  #   "ob"  = Order Block retest (orderblock.py, settings below)
                                  #   "vwap", "orb", "lsmss", "supertrend" = strategies 4-7 (paper first, settings below)

# ---- Strategy v2 (see strategy.py) ----
EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND = 50                    # trade only in the direction of this trend EMA
CROSS_LOOKBACK = 3                # EMA cross must be within the last N candles
RSI_PERIOD = 14
RSI_BUY_CE = 55                   # CE needs RSI >= this ...
RSI_BUY_PE = 45                   # PE needs RSI <= this ...
RSI_MAX = 75                      # ... but skip exhausted moves (CE RSI > 75, PE RSI < 25)
MIN_ATR_PCT = 0.05                # skip when 5-min ATR < 0.05% of index (dead market)

# ---- Risk : reward (on the INDEX level) ----
RR = 2.0                          # target = 2 x risk  -> 1:2
BREAKEVEN_AT_R = 1.0              # at 1:1, move stop to COST: entry + charges & slippage (0 = off)
BE_LOCK_PCT = 25                  # ... and lock this % of the risk: entry 180, stop 150 → stop 187.5 at 210 (0 = just cost)
TRAIL_STEP_POINTS = 3             # after 1:1, every 3 points of further move raises the stop 3 points (0 = off)
COST_STOP_ON_PREMIUM = False      # also exit if the OPTION falls back to cost (stricter; tested worse)
TRAIL_START_R = 0                 # from +R x this, trail the stop (0 = off; tested: mixed results)
TRAIL_R = 1.0                     # ... this many R behind the best price (0 = off)
TIME_STOP_MIN = 0                 # exit if not +0.5R within N minutes (0 = off)

VIX_ADJUST_EXITS = True           # scale stop & target by sqrt(India VIX / VIX_BASE): wider when fearful, tighter when calm
VIX_BASE = 14.0                   # "normal" India VIX: at this level stops are unchanged
VIX_MULT_MIN = 0.85               # never tighter than 0.85 x the ATR/swing stop
VIX_MULT_MAX = 1.3                # never wider than 1.3 x

# ---- Entry filters ----
HTF_FILTER = True                 # 15-min EMA-20 must agree with the trade (tested: helps a little)
ADX_MIN = 0                       # skip when ADX(14) is below this (0 = off; 20 = fewer, choppy-free trades)
VOLUME_FILTER = True              # signal candle needs above-normal volume (indices have none: VOLUME_SYMBOL is the proxy)
VOLUME_SYMBOL = "NIFTYBEES.NS"    # Nifty ETF; used for Sensex too (same big stocks)
VOLUME_MIN_RVOL = 1.2             # candle volume per minute >= this x the average of the previous VOLUME_AVG_N candles
VOLUME_AVG_N = 20

# ---- Fair Value Gap strategy (fvg.py; used when "fvg" is in STRATEGIES) ----
# The EMA-cross settings above, VOLUME_FILTER and VIX_ADJUST_EXITS do not apply to FVG trades; MIN_ATR_PCT,
# the hard risk limits, timings and the agent team do.
FVG_ALLOW_LIVE = False            # the FVG strategy refuses live mode until this is on (not yet proven in a backtest)
FVG_ENTRY_MODE = "balanced"       # "aggressive" = touch of the zone, "balanced" = midpoint rejection,
                                  # "conservative" = confirmation candle after a touch
FVG_DISP_BODY_ATR = 1.0           # displacement (middle) candle body >= this x ATR(14)
FVG_DISP_BODY_PCT = 0.6           # ... and >= this share of its own high-low range
FVG_MIN_SIZE_ATR = 0.2            # gap size limits, in ATR
FVG_MAX_SIZE_ATR = 2.0
FVG_VOLUME_FILTER = True          # displacement candle volume (NIFTYBEES proxy) >= FVG_VOLUME_MULT x its average
FVG_VOLUME_MULT = 1.5
FVG_VOLUME_AVG_N = 20
FVG_TREND_MODE = "prefer"         # "require" = EMA9/21/50 must agree, "prefer" = adds to the score, "off"
FVG_EMA_FAST = 9
FVG_EMA_SLOW = 21
FVG_EMA_TREND = 50
FVG_SWING_N = 2                   # swing high/low = candle with 2 lower highs / higher lows on each side
FVG_STRUCTURE_LOOKBACK = 40       # candles searched for swings (BOS / CHoCH / sweep)
FVG_SWEEP_LOOKBACK = 6            # a liquidity sweep must be within this many candles before the gap
FVG_REQUIRE_BOS = False
FVG_REQUIRE_SWEEP = False
FVG_MIN_SCORE = 50                # displacement 25 + volume 15 + trend 25 + BOS 20 + sweep 15 = 100
FVG_MAX_AGE = 24                  # a zone is tradeable for this many candles (24 x 5 min = 2 h)
FVG_MAX_TOUCHES = 2               # separate visits allowed before a zone is spent
FVG_MAX_ZONES = 10                # live zones kept per index
FVG_REJECTION_WICK_PCT = 0.3      # balanced: a rejection wick of >= 30% of the candle's range also counts
FVG_CONFIRM_WINDOW = 3            # conservative: confirmation within this many candles of the last touch
FVG_MAX_CHASE_ATR = 0.5           # conservative: no entry more than 0.5 x ATR beyond the zone
FVG_SL_METHOD = "fvg"             # "fvg" = beyond the far boundary, "swing" = setup's swing low/high, "atr"
FVG_SL_BUFFER_ATR = 0.1           # extra room beyond the boundary / swing, in ATR
FVG_SL_ATR_MULT = 1.0             # "atr" stop distance
FVG_MIN_RISK_ATR = 0.3            # a tighter stop is widened to this (noise)
FVG_MAX_RISK_ATR = 2.0            # a wider stop skips the trade
FVG_RR = 2.0                      # target = 2 x risk (1, 1.5, 2 or 3)
FVG_BREAKEVEN_AT_R = 1.0          # stop to cost at +1R (0 = off)
FVG_TRAIL_START_R = 0             # trailing stop from +R x this (0 = off)
FVG_TRAIL_R = 1.0
FVG_PARTIAL_AT_R = 0              # book FVG_PARTIAL_PCT of the position at +R x this (0 = off; needs >= 2 lots)
FVG_PARTIAL_PCT = 50
FVG_EXIT_ON_OPPOSITE = True       # exit when an opposite FVG signal fires on the same index
FVG_MAX_HOLD_MIN = 0              # exit after this many minutes regardless (0 = off; square-off still applies)

# ---- Agents ----
USE_CRUDE_AGENT = True
CRUDE_BLOCK_PCT = 1.0             # Brent up >= 1% blocks CE; down >= 1% blocks PE
USE_NEWS_AGENT = True
NEWS_BLOCK_SCORE = 0.4            # sentiment <= -0.4 blocks CE; >= +0.4 blocks PE
NEWS_BLOCK_ON_EVENT = False       # True = no new trades on RBI/Fed/Budget/result days
USE_GLOBAL_AGENT = True           # GIFT Nifty, US / Europe / Asia indices, US futures, dollar, rupee, US yields
GLOBAL_BLOCK_SCORE = 0.6          # global score <= -0.6 blocks CE; >= +0.6 blocks PE (only strong worldwide moves)
USE_TECHNICAL_AGENT = True        # RSI / MACD / Bollinger read, feeds the debate
USE_SENTIMENT_AGENT = True        # Reddit chatter, India VIX, heavyweight breadth
USE_FUNDAMENTAL_AGENT = True      # earnings health of Nifty heavyweights (refreshes every 12 h)
USE_TRADER_AGENT = True           # debate + trader may overrule a strategy signal (HOLD)
TRADER_BLOCK_MARGIN = 0.35        # HOLD when the debate is this far against the signal (0-1)
AGENT_AUTO_REFRESH = True         # control panel keeps agents fresh even while the bot is stopped

# ---- Scalper (scalper.py): same entry rules + agents + ITM options, on fast candles, fixed point exits ----
SCALP_STRATEGY = "v2"             # "v2" = Scalper V2 (scalp_v2.py), "ema" = main bot's EMA-cross rules,
                                  # "volume" = volume-spike breakout. V2 did NOT pass its backtest either — paper only.
SCALP_VOL_SYMBOL = "NIFTYBEES.NS" # volume proxy (indices have no volume); used for Sensex too
SCALP_VOL_MULT = 1.5              # volume must be this many times its recent average
SCALP_VOL_AVG_N = 20              # candles in that average
SCALP_VOL_BREAKOUT_N = 5          # close must break the high/low of this many previous candles
SCALP_POINTS_ON = "premium"      # "premium" = points on the option price, "index" = points on the index
SCALP_SL_POINTS = 15              # stop-loss (option premium points)
SCALP_TARGET_POINTS = 30          # target (1:2)
SCALP_BE_AT_POINTS = 15           # at +15 (1R) move the stop to cost (entry + charges); 0 = off
SCALP_LOTS = 1
SCALP_CANDLE_MINUTES = 1          # signal candles (the main bot uses 5)
SCALP_MIN_ATR_PCT = 0.02          # 1-min candles move less, so the dead-market filter is lower
SCALP_MAX_HOLD_MIN = 30           # exit a scalp that has gone nowhere after 30 minutes (0 = off)
SCALP_COOLDOWN_MIN = 5            # wait after each exit before a new entry (V2 also needs a NEW setup after a stop)
SCALP_MAX_TRADES_PER_DAY = 6
SCALP_MAX_DAILY_LOSS = 2500       # checked BEFORE each entry against the trade's loss at the stop (≈2 x 15-pt stops)
SCALP_MAX_CONSECUTIVE_LOSSES = 2  # 2 losses in a row = done for the day; no agent can override
SCALP_NO_ENTRY_BEFORE = "09:35"
SCALP_NO_ENTRY_AFTER = "14:30"
SCALP_AVOID_OVERLAP = True        # no scalp on an index while the main bot holds a position there
# ---- Scalper V2 (scalp_v2.py; tested by scalp_v2_backtest.py → reports/scalper_v2/) ----
SCALP_V2_STRICT = False           # True = every V2 rule mandatory (backtest: ~1 trade in 18 days)
SCALP_REGIME_FILTER = True        # trade only STRONG up/down trends (weak ones need a very high score)
SCALP_ADX_MIN = 20
SCALP_ATR_MIN_PCT = 0.025         # 1-min ATR as % of the index; below = dead market
SCALP_EMA_SEPARATION_MIN = 0.03   # |EMA9 - EMA21| as % of price
SCALP_CHOP_FILTER = True          # choppiness index > 61.8 or >= 3 EMA crosses in 20 candles = RANGE
SCALP_ALLOW_WEAK_TREND = True
SCALP_WEAK_TREND_SCORE_MIN = 85
SCALP_EMA_SLOPE_LOOKBACK = 3
SCALP_PULLBACK_ENTRY = True       # pullback to EMA9/21 that holds EMA21, then a break of the pullback high/low
SCALP_PULLBACK_LOOKBACK = 3
SCALP_BREAKOUT_ENTRY = True       # volume breakout, entered on the NEXT candle if it holds (false-breakout guard)
SCALP_BREAKOUT_CONFIRM = True
SCALP_BREAKOUT_LOOKBACK = 5
SCALP_MAX_VWAP_DISTANCE_PCT = 0.12
SCALP_CE_RSI_MIN, SCALP_CE_RSI_MAX = 52, 68
SCALP_PE_RSI_MIN, SCALP_PE_RSI_MAX = 32, 48
SCALP_USE_MACD = True
SCALP_MIN_BODY_PCT = 0.03
SCALP_MAX_OPPOSITE_WICK_RATIO = 0.40
SCALP_MAX_SIGNAL_ATR = 1.5        # skip a signal candle bigger than 1.5 x ATR (chasing)
SCALP_OPENING_RANGE_FILTER = True
SCALP_MAX_OPENING_RANGE_PCT = 0.35
SCALP_CROSS_INDEX_FILTER = True   # Nifty checks Bank Nifty, Sensex checks Nifty: skip if strongly opposite
SCALP_VIX_FILTER = True           # skip bottom-10% VIX with low ATR, and VIX above its 95th percentile
TRADE_SCORE_MIN = 75
SCALP_CE_ENABLED = True
SCALP_PE_ENABLED = True
SCALP_SIDE_ADAPT = True           # a side whose last 8 closed scalps have PF < 0.8 needs +10 score
SCALP_MAX_SPREAD_PCT = 1.0        # option bid/ask spread limit (live quotes only)
SCALP_MIN_OPTION_VOLUME = 0       # absolute floors (0 = off); relative check below always applies
SCALP_MIN_OPTION_OI = 0
SCALP_MIN_REL_LIQUIDITY = 0.25    # option volume/OI must be >= 25% of the median of nearby strikes
SCALP_DELTA_MIN = 0.55            # preferred |delta| band for the traded option
SCALP_DELTA_MAX = 0.70
SCALP_DYNAMIC_SL = False          # True = premium stop from 1-min ATR x delta x 0.8 (5–12 pts); False = SCALP_SL_POINTS
SCALP_MIN_SL_POINTS = 5
SCALP_MAX_SL_POINTS = 12
SCALP_ATR_SL_MULT = 0.8
SCALP_RR = 2.0                    # V2 target = 2 x the actual stop
SCALP_BE_AT_R = 1.0               # V2: stop to cost after 1 x risk in profit (0 = off); 1.0/1.2/1.5 tested the same
SCALP_STRUCTURAL_COOLDOWN = True  # after a stop, same side needs a reset through EMA9 and a fresh setup
SCALP_ALLOW_LIVE = False          # the scalper refuses live mode unless this is switched on (backtest: loses with real fills)

# ---- HARD RISK LIMITS ----
MAX_DAILY_LOSS = 3000
MAX_TRADES_PER_DAY = 3
MAX_CONSECUTIVE_LOSSES = 2
MAX_PREMIUM_PER_TRADE = 40000     # ITM options cost more — total premium cap per trade

# ---- Timing (IST) ----
NO_ENTRY_BEFORE = "09:30"
NO_ENTRY_AFTER = "13:30"          # afternoons were where v1 bled (square-off losses)
SQUARE_OFF_AT = "15:10"

POLL_SECONDS = 5
TICK_STREAM = True                # live / Zerodha-paper: use Zerodha WebSocket ticks for index prices (REST polling as fallback)

# ---- Order Block strategy (orderblock.py; used when "ob" is in STRATEGIES) ----
# The EMA-cross settings, VOLUME_FILTER and VIX_ADJUST_EXITS do not apply to order block trades; MIN_ATR_PCT,
# the hard risk limits, timings and the agent team do.
OB_ALLOW_LIVE = False             # refuses live mode until this is on (backtest and paper trade it first)
OB_HTF_MINUTES = 15               # higher timeframe, built from completed candles only
OB_HTF_EMA = 20                   # trend = close and slope against this EMA of higher-timeframe closes
OB_REQUIRE_HTF = True
OB_SWING_LOOKBACK = 3             # candles each side of a swing (3-5); known only once they have closed
OB_STRUCTURE_LOOKBACK = 60        # candles searched for swings
OB_REQUIRE_SWEEP = True           # a liquidity sweep must come first
OB_SWEEP_MIN_WICK_PCT = 0.4       # the sweep candle's rejection wick, as a share of its range
OB_SWEEP_MAX_AGE = 12             # candles a sweep stays usable
OB_REQUIRE_MSS = True             # the displacement must close through the last confirmed swing
OB_DISPLACEMENT_ATR = 1.0         # displacement body >= this x ATR(14)
OB_DISPLACEMENT_BODY_PCT = 0.55   # ... and >= this share of its range
OB_ZONE_MODE = "OPEN_TO_WICK"     # "OPEN_TO_WICK", "FULL_CANDLE" or "BODY"
OB_SEARCH_BACK = 8                # candles searched back for the order block candle
OB_MAX_ZONE_ATR = 1.5             # skip zones wider than this x ATR
OB_MAX_AGE = 36                   # candles a zone stays tradeable (also ends with the session)
OB_MAX_ZONES = 10
OB_REQUIRE_FVG = False            # True = strict OB + FVG; False = OB with strong displacement and MSS
OB_FVG_MIN_OVERLAP_PCT = 10.0     # FVG / zone overlap needed, as a % of the narrower of the two
OB_REQUIRE_VOLUME = False
OB_VOLUME_MULT = 1.2              # displacement volume >= this x the average (VOLUME_SYMBOL proxy)
OB_VOLUME_AVG_N = 20
OB_REQUIRE_VWAP = False           # close on the trade side of today's VWAP
OB_REJECTION_WICK_PCT = 0.3       # rejection candle's wick into the zone, as a share of its range
OB_MAX_DISTANCE_ATR = 1.0         # skip if the entry has run this x ATR beyond the zone
OB_SL_METHOD = "wider"            # "ob" = beyond the zone, "sweep" = beyond the sweep, "wider" = whichever is further
OB_SL_BUFFER_ATR = 0.1
OB_MAX_RISK_ATR = 2.5             # skip if the stop is wider than this x ATR
OB_TARGET_MODE = "rr"             # "rr" = OB_RR x risk, "swing" = nearest opposing swing, "liquidity" = nearest paying OB_MIN_RR
OB_RR = 2.0                       # 2 or 3
OB_MIN_RR = 2.0                   # skip any setup whose target pays less than this
OB_BREAKEVEN_AT_R = 1.0           # stop to cost at this x risk; 0 = off
OB_TRAIL_START_R = 0              # 1.5 = trail from 1.5R; 0 = off
OB_TRAIL_R = 1.0
OB_PARTIAL_AT_R = 0               # 1 = book part at 1R; 0 = off
OB_PARTIAL_PCT = 50
OB_PREMIUM_EXITS = True           # also exit on option-price stop and target, set from the fill price
OB_OPTION_DELTA = 0.6             # assumed delta of the ITM option for those levels
OB_MAX_TRADES_PER_DAY = 2         # order block signals per index per day; MAX_TRADES_PER_DAY still caps the whole bot
OB_MIN_SCORE = 75                 # quality checklist out of 100; not a probability of winning
OB_SCORE_SWEEP = 20
OB_SCORE_MSS = 20
OB_SCORE_DISPLACEMENT = 15
OB_SCORE_OB = 15
OB_SCORE_FVG = 15
OB_SCORE_HTF = 10
OB_SCORE_VOLUME = 5

# ---- Strategies 4-7. Starting values, NOT tested optimal parameters. Each refuses live mode until its
# *_ALLOW_LIVE switch is on, and is off unless picked in STRATEGIES. EMA-cross settings, VOLUME_FILTER and
# VIX_ADJUST_EXITS don't apply to them; MIN_ATR_PCT, the hard risk limits, timings and agents do.
# Every value here must match the module's CFG_DEFAULTS (tests/test_strategy_registry.py checks it).
# ---- VWAP + Volume strategy (vwap_volume.py; used when "vwap" is in STRATEGIES)
VWAP_ALLOW_LIVE = False
VWAP_EMA_FAST = 20
VWAP_EMA_SLOW = 50
VWAP_RSI_PERIOD = 14
VWAP_ATR_PERIOD = 14
VWAP_RSI_CE_MIN = 50
VWAP_RSI_CE_MAX = 70
VWAP_RSI_PE_MIN = 30
VWAP_RSI_PE_MAX = 50
VWAP_VOLUME_MULT = 1.5
VWAP_VOLUME_AVG_N = 20
VWAP_REQUIRE_VOLUME = True
VWAP_VOLUME_BASIS = 'rolling'
VWAP_VOLUME_SLOT_DAYS = 5
VWAP_SLOPE_BARS = 6
VWAP_MIN_SLOPE_ATR = 0.1
VWAP_CROSS_LOOKBACK = 12
VWAP_MAX_CROSSES = 3
VWAP_PULLBACK_BARS = 4
VWAP_TOUCH_ATR = 0.25
VWAP_MIN_BODY_PCT = 0.5
VWAP_MAX_EXT_ATR = 1.5
VWAP_MIN_SESSION_BARS = 3
VWAP_HTF_MINUTES = 15
VWAP_HTF_EMA = 20
VWAP_REQUIRE_HTF = True
VWAP_SL_BUFFER_ATR = 0.1
VWAP_MIN_RISK_ATR = 0.5
VWAP_MAX_RISK_ATR = 2.0
VWAP_RR = 2.0
VWAP_MIN_RR = 2.0
VWAP_BREAKEVEN_AT_R = 1.0
VWAP_BE_LOCK_PCT = 0
VWAP_TRAIL_STEP_POINTS = 0
VWAP_TRAIL_START_R = 1.5
VWAP_TRAIL_R = 1.0
VWAP_EXIT_ON_INVALIDATION = True
VWAP_INVALIDATION_BUFFER_ATR = 0.1
VWAP_MAX_SIGNALS_PER_DAY = 2
VWAP_COOLDOWN_BARS = 6

# ---- Opening Range Breakout + Retest (orb_retest.py; "orb")
ORB_ALLOW_LIVE = False
ORB_RANGE_START = '09:15'
ORB_RANGE_END = '09:30'
ORB_VOLUME_MULT = 1.5
ORB_VOLUME_AVG_N = 20
ORB_REQUIRE_VOLUME = True
ORB_VOLUME_BASIS = 'rolling'
ORB_VOLUME_SLOT_DAYS = 5
ORB_REQUIRE_RETEST = True
ORB_REQUIRE_HTF = True
ORB_HTF_MINUTES = 15
ORB_HTF_EMA = 20
ORB_MIN_BODY_PCT = 0.5
ORB_BREAK_BUFFER_ATR = 0.05
ORB_MIN_RANGE_ATR = 0.5
ORB_MAX_RANGE_ATR = 4.0
ORB_MAX_RANGE_PCT = 0.8
ORB_RETEST_BARS = 6
ORB_RETEST_TOL_ATR = 0.2
ORB_CONFIRM_BARS = 3
ORB_FAIL_BUFFER_ATR = 0.0
ORB_MAX_CHASE_ATR = 1.5
ORB_NO_ENTRY_AFTER = '12:00'
ORB_SL_BUFFER_ATR = 0.1
ORB_MIN_RISK_ATR = 0.5
ORB_MAX_RISK_ATR = 2.0
ORB_RR = 2.0
ORB_MIN_RR = 2.0
ORB_BREAKEVEN_AT_R = 1.0
ORB_BE_LOCK_PCT = 0
ORB_TRAIL_STEP_POINTS = 0
ORB_TRAIL_START_R = 1.5
ORB_TRAIL_R = 1.0
ORB_EXIT_ON_STRUCTURE_FAIL = True
ORB_MAX_SIGNALS_PER_DAY = 1

# ---- Liquidity Sweep + Market Structure Shift (liquidity_mss.py; "lsmss")
LSM_ALLOW_LIVE = False
LSM_SWING_N = 2
LSM_STRUCTURE_LOOKBACK = 60
LSM_EQUAL_TOL_ATR = 0.1
LSM_REQUIRE_EQUAL = False
LSM_SWEEP_MIN_PEN_ATR = 0.05
LSM_SWEEP_MAX_PEN_ATR = 1.5
LSM_SWEEP_MIN_WICK_PCT = 0.4
LSM_MSS_BARS = 6
LSM_DISP_ATR = 1.0
LSM_DISP_BODY_PCT = 0.55
LSM_FVG_MIN_ATR = 0.1
LSM_ENTRY_MODE = 'balanced'
LSM_REJECTION_WICK_PCT = 0.3
LSM_MAX_CHASE_ATR = 0.5
LSM_ZONE_MAX_AGE = 24
LSM_REQUIRE_HTF = True
LSM_HTF_MODE = 'not_against'
LSM_HTF_MINUTES = 15
LSM_HTF_SWING_N = 2
LSM_SL_BUFFER_ATR = 0.1
LSM_MIN_RISK_ATR = 0.3
LSM_MAX_RISK_ATR = 3.0
LSM_RR = 2.0
LSM_MIN_RR = 2.0
LSM_BREAKEVEN_AT_R = 1.0
LSM_BE_LOCK_PCT = 0
LSM_TRAIL_STEP_POINTS = 0
LSM_TRAIL_START_R = 1.5
LSM_TRAIL_R = 1.0
LSM_EXIT_ON_INVALIDATION = True
LSM_MAX_SIGNALS_PER_DAY = 2

# ---- Supertrend + ADX (supertrend_adx.py; "supertrend")
# v3 (15 Nov 2026, research/select_st_90d.py): multiplier 2, ADX >= 20 and rising over 3 candles, confirmation
# candle closes beyond the previous one, no trailing, no exit on a Supertrend flip.
ST_ALLOW_LIVE = False
ST_ATR_PERIOD = 10
ST_MULT = 2.0
ST_ADX_PERIOD = 14
ST_ADX_MIN = 20
ST_ADX_RISING_BARS = 3
ST_DI_GAP = 0.0
ST_EMA_TREND = 50
ST_EMA_PULLBACK = 20
ST_RSI_PERIOD = 14
ST_RSI_CE_MIN = 50
ST_RSI_CE_MAX = 70
ST_RSI_PE_MIN = 30
ST_RSI_PE_MAX = 50
ST_PULLBACK_BARS = 4
ST_TOUCH_ATR = 0.3
ST_MIN_BODY_PCT = 0.5
ST_REQUIRE_HTF = False
ST_HTF_MINUTES = 15
ST_HTF_EMA = 20
ST_SL_MODE = 'atr'
ST_SL_ATR_MULT = 1.5
ST_SL_BUFFER_ATR = 0.1
ST_MIN_RISK_ATR = 0.5
ST_MAX_RISK_ATR = 2.5
ST_RR = 2.0
ST_MIN_RR = 2.0
ST_BREAKEVEN_AT_R = 1.0
ST_BE_LOCK_PCT = 25
ST_TRAIL_STEP_POINTS = 0
ST_TRAIL_START_R = 0
ST_TRAIL_R = 1.0
ST_EXIT_ON_FLIP = False
ST_MAX_SIGNALS_PER_DAY = 2
ST_COOLDOWN_BARS = 6
ST_RSI_FILTER = False
ST_EMA_FILTER = False
ST_VWAP_FILTER = False
ST_CONFIRM = 'break'
ST_ENTRY_FROM = ''
ST_ENTRY_UNTIL = ''

# ---- Trend Continuation, opening range + VWAP (trend_continuation.py; "tc"). Paper first.
TC_ALLOW_LIVE = False
TC_OR_START = '09:15'
TC_OR_END = '09:45'
TC_ENTRY_FROM = '10:15'
TC_ENTRY_UNTIL = '12:00'
TC_SL_ATR = 2.0
TC_RR = 2.0
TC_MIN_RR = 2.0
TC_BREAKEVEN_AT_R = 1.0
TC_BE_LOCK_PCT = 25
TC_TRAIL_STEP_POINTS = 0
TC_TRAIL_START_R = 0
TC_TRAIL_R = 1.0
TC_EXIT_ON_FAIL = False
TC_MIN_BREAK_ATR = 0.4

# ---- Instrument specs (lot size & expiry are read live from Kite; these are backtest fallbacks) ----
INSTRUMENT_SPECS = {
    "NIFTY":  dict(index="NSE:NIFTY 50", token=256265, exchange="NFO", name="NIFTY",  step=50,  lot=65, yahoo="^NSEI",  expiry_weekday=1),
    "SENSEX": dict(index="BSE:SENSEX",   token=265,    exchange="BFO", name="SENSEX", step=100, lot=20, yahoo="^BSESN", expiry_weekday=3),
}

# ---- WhatsApp alert when a trade is entered (notify.py, via CallMeBot) ----
WHATSAPP_ALERTS = False
WHATSAPP_NUMBER = ""              # with country code, e.g. "919030953685"
WHATSAPP_APIKEY = ""              # CallMeBot key for that number (see notify.py)
TELEGRAM_ALERTS = False
SKIP_ALERTS = True                # also alert when a main-bot signal fires but is skipped (with the reason)
HEADS_UP_ALERTS = False           # also alert when a main-bot signal looks likely on the next candle(s) (heads_up.py)
TELEGRAM_USER = ""                # Telegram @username, authorised once at api2.callmebot.com/txt/login.php

# ---- apply settings saved from the web app ----
# Each panel user has their own settings.json (users/<id>/): the panel passes its path as _settings_path,
# a bot it starts gets it in KITEBOT_SETTINGS. Run by hand, the settings.json next to this file applies.
_SETTINGS = (globals().get("_settings_path") or os.environ.get("KITEBOT_SETTINGS")
             or os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json"))
if os.path.exists(_SETTINGS):
    with open(_SETTINGS) as _f:
        for _k, _v in json.load(_f).items():
            if _k.isupper() and _k != "INSTRUMENT_SPECS":
                globals()[_k] = _v
# settings saved when only one strategy could be chosen
if "STRATEGY" in globals():
    if not (os.path.exists(_SETTINGS) and "STRATEGIES" in json.load(open(_SETTINGS))):
        STRATEGIES = [STRATEGY]
    del STRATEGY
# ---- unsafe timings are corrected here so the bot, backtest and Settings page all agree
def _mins(v):
    h, m = map(int, str(v).split(":"))
    return h * 60 + m


TIME_WARNINGS = []
if _mins(SQUARE_OFF_AT) > _mins("15:20"):
    TIME_WARNINGS.append(f"Square-off {SQUARE_OFF_AT} is too late (Zerodha auto-squares F&O at 15:26 for a fee, market shuts 15:30) "
                         "— using 15:20. Save Settings to fix it.")
    SQUARE_OFF_AT = "15:20"
if _mins(NO_ENTRY_AFTER) >= _mins(SQUARE_OFF_AT):
    TIME_WARNINGS.append(f"No-entry time {NO_ENTRY_AFTER} is at/after square-off — using 13:30. Save Settings to fix it.")
    NO_ENTRY_AFTER = "13:30"

# old v1 settings that no longer apply
for _old in ("STOP_LOSS_PCT", "TARGET_PCT"):
    globals().pop(_old, None)


def instruments():
    if TRADE_INSTRUMENTS == "BOTH":
        return ["NIFTY", "SENSEX"]
    return [TRADE_INSTRUMENTS] if TRADE_INSTRUMENTS in INSTRUMENT_SPECS else ["NIFTY"]
