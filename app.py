
import os
import time
import requests
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
SYMBOL = "XAU/USD"

cache = {}
CACHE_SECONDS = 60

def ema(values, period):
    if len(values) < period:
        return None

    result = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

    for value in values[period:]:
        result = (value - result) * multiplier + result

    return result

def rsi(values, period=14):
    if len(values) <= period:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    return 100 - (100 / (1 + avg_gain / avg_loss))

def get_market_data(interval):
    if not API_KEY:
        raise ValueError("Add your Twelve Data API key first.")

    now = time.time()
    key = interval

    if key in cache and now - cache[key]["time"] < CACHE_SECONDS:
        return cache[key]["data"]

    response = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": SYMBOL,
            "interval": interval,
            "outputsize": 100,
            "apikey": API_KEY,
            "format": "JSON"
        },
        timeout=20
    )

    response.raise_for_status()
    data = response.json()

    if "values" not in data:
        raise ValueError(data.get("message", "Market data unavailable."))

    candles = list(reversed(data["values"]))

    for candle in candles:
        for field in ("open", "high", "low", "close"):
            candle[field] = float(candle[field])

    if len(candles) < 51:
        raise ValueError("Not enough candles for analysis.")

    cache[key] = {"time": now, "data": candles}
    return candles
def analyze(candles):
    closes = [c["close"] for c in candles]
    fast = ema(closes, 20)
    slow = ema(closes, 50)
    momentum = rsi(closes)

    ranges = [
        max(            c["high"] - c["low"],
            abs(c["high"] - candles[i - 1]["close"]),
            abs(c["low"] - candles[i - 1]["close"])
        )
        for i, c in enumerate(candles)
        if i > 0
    ]

    atr = sum(ranges[-14:]) / min(14, len(ranges))
    price = closes[-1]

    recent = candles[-50:]
    support = min(c["low"] for c in recent)
    resistance = max(c["high"] for c in recent)

    trend = "BULLISH" if fast > slow else "BEARISH" if fast < slow else "SIDEWAYS"

    bullish_score = 0
    bearish_score = 0

    if fast > slow:
        bullish_score += 40
    elif fast < slow:
        bearish_score += 40

    if 50 < momentum < 70:
        bullish_score += 30
    elif 30 < momentum < 50:
        bearish_score += 30

    if price > fast:
        bullish_score += 15
    elif price < fast:
        bearish_score += 15

    if abs(fast - slow) > atr * 0.1:
        if fast > slow:
            bullish_score += 15
        elif fast < slow:
            bearish_score += 15

    signal = "WAIT"
    reason = "Technical conditions are not sufficiently aligned."
    setup_score = max(bullish_score, bearish_score)

    if fast > slow and 50 < momentum < 70 and price > fast:
        signal = "BUY"
        reason = "Bullish EMA trend with RSI and price confirmation."
        setup_score = bullish_score

    elif fast < slow and 30 < momentum < 50 and price < fast:
        signal = "SELL"
        reason = "Bearish EMA trend with RSI and price confirmation."
        setup_score = bearish_score

    stop_loss = None
    take_profit = None

    if signal == "BUY":
        stop_loss = price - 1.5 * atr
        take_profit = price + 3 * atr

    elif signal == "SELL":
        stop_loss = price + 1.5 * atr
        take_profit = price - 3 * atr

    return {
        "symbol": SYMBOL,
        "price": round(price, 3),
        "signal": signal,
        "reason": reason,
        "trend": trend,
        "setup_score": setup_score,
        "ema20": round(fast, 3),
        "ema50": round(slow, 3),
        "rsi": round(momentum, 2),
        "atr": round(atr, 3),
        "support": round(support, 3),
        "resistance": round(resistance, 3),
        "entry": round(price, 3) if signal != "WAIT" else None,
        "stop_loss": round(stop_loss, 3) if stop_loss is not None else None,
        "take_profit": round(take_profit, 3) if take_profit is not None else None,
        "news_warning": "Live economic calendar is not connected. Check high-impact news manually.",
        "candles": candles,
        "updated": candles[-1]["datetime"]
    }


    fast = ema(closes, 20)
    slow = ema(closes, 50)
    momentum = rsi(closes)

    ranges = [
        max(
            c["high"] - c["low"],
            abs(c["high"] - candles[i - 1]["close"]),
            abs(c["low"] - candles[i - 1]["close"])        )
        for i, c in enumerate(candles)
        if i > 0
    ]

    atr = sum(ranges[-14:]) / min(14, len(ranges))
    price = closes[-1]

    signal = "WAIT"
    reason = "Trend and RSI do not agree."

    if fast > slow and 50 < momentum < 70:
        signal = "BUY"
        reason = "EMA 20 above EMA 50 and RSI confirms bullish momentum."

    elif fast < slow and 30 < momentum < 50:
        signal = "SELL"
        reason = "EMA 20 below EMA 50 and RSI confirms bearish momentum."

    stop_loss = None
    take_profit = None

    if signal == "BUY":
        stop_loss = price - 1.5 * atr
        take_profit = price + 3 * atr

    elif signal == "SELL":
        stop_loss = price + 1.5 * atr
        take_profit = price - 3 * atr

    return {
        "symbol": SYMBOL,
        "price": round(price, 3),
        "signal": signal,
        "reason": reason,
        "ema20": round(fast, 3),
        "ema50": round(slow, 3),
        "rsi": round(momentum, 2),
        "atr": round(atr, 3),
        "stop_loss": round(stop_loss, 3) if stop_loss else None,
        "take_profit": round(take_profit, 3) if take_profit else None,
        "candles": candles,
        "updated": candles[-1]["datetime"]
    }

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/analysis")
def api_analysis():
    interval = request.args.get("interval", "15min")

    allowed = ["5min", "15min", "30min", "1h"]

    if interval not in allowed:
        return jsonify({"error": "Invalid timeframe."}), 400

    try:
        candles = get_market_data(interval)
        return jsonify(analyze(candles))

    except Exception as error:
        return jsonify({"error": str(error)}), 503
@app.route("/risk")
def risk_page():
    return render_template("risk.html")
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
