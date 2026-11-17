# Trading Bot

Flask control panel and Zerodha Kite bot for index options, stock analysis, and risk-managed stock-option trades.

## Stock options

The Stock trades page combines daily technicals, chart patterns, and fundamentals for the 15 largest Nifty-50 stocks. Stock-option premiums and lot sizes shown in the analysis are estimates; they are not used to price live orders.

To enable stock-option automation:

1. In Settings, enable **Auto-trade stock options**.
2. Keep the bot in Paper mode while validating signals and exits.
3. Start the main bot. The scanner checks eligible stock setups in the background. The Stock trades page also has one-click **Buy on signal** and **Sell to close** actions routed through that same order manager.
4. Live orders require Live mode, a current Zerodha login, and the existing typed `YES` confirmation when starting the bot.

The stock-option strategy only buys calls or puts; it never opens short options. It takes the strongest fresh stock setup whose analysis checks pass, sizes from the configured lot/funds/premium limits, applies the existing risk manager and daily limits, and permits one stock-option position at a time. It uses a one-ATR stop and the configured risk/reward target on the underlying stock, monitors positions using server-side prices, places a protective exchange stop for live trades, and squares off at the configured time. Stale or unavailable prices, missing instruments, wide option spreads, failed analysis checks, or risk vetoes prevent an entry.

Stock analysis uses Yahoo Finance daily data and cached company fundamentals. Live stock and option entry/exit prices are fetched from Zerodha on the server; paper option prices are modeled and can differ materially from real fills. Analysis and backtests are informational, not guarantees of future results.

## Reference framework

The multi-agent workflow in [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) separates analyst reports, bull/bear debate, trading decisions, and risk review. This application already uses a similar analyst → debate → trader → risk-manager flow for index options; stock-option automation reuses the app's analysis, risk gates, and existing broker/order manager rather than copying the reference implementation.
