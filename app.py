
import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
SYMBOL = "XAU/USD"

cache = {}
CACHE_SECONDS = 60
NEWS_URL = "https://showmytrades.com/api/public/calendar/v1"
NEWS_CACHE_SECONDS = 3600
NEWS_CACHE_FILE = os.path.join(os.path.dirname(__file__), "news_cache.json")

news_cache = {"time": 0, "events": None}
EAT = timezone(timedelta(hours=3))

# Restore previously saved calendar data when available.
try:
    with open(NEWS_CACHE_FILE, "r", encoding="utf-8") as f:
        saved = json.load(f)
        if isinstance(saved, dict) and isinstance(saved.get("events"), list):
            news_cache["time"] = float(saved.get("time", 0))
            news_cache["events"] = saved["events"]
except (OSError, ValueError, TypeError):
    pass


def get_economic_news():
    now = datetime.now(timezone.utc)
    current_time = time.time()
    stale = False

    if (
        news_cache["events"] is not None
        and current_time - news_cache["time"] < NEWS_CACHE_SECONDS
    ):
        events = news_cache["events"]

    else:
        try:
            response = requests.get(
                NEWS_URL,
                params={
                    "currency": "USD",
                    "impact": "high,medium,low",
                    "days": 7
                },
                headers={"User-Agent": "GOLD-EDGE-AI/1.0"},
                timeout=20
            )
            response.raise_for_status()
            data = response.json()

            if not isinstance(data, dict) or not isinstance(data.get("events"), list):
                raise ValueError("Invalid economic calendar response.")

            events = []

            for item in data["events"]:
                if not isinstance(item, dict):
                    continue

                if str(item.get("currency", "")).upper() != "USD":
                    continue

                impact = str(item.get("impact", "")).title()

                if impact not in ("High", "Medium", "Low"):
                    continue

                try:
                    event_time = datetime.fromisoformat(
                        str(item["time"]).replace("Z", "+00:00")
                    )

                    if event_time.tzinfo is None:
                        event_time = event_time.replace(tzinfo=timezone.utc)

                    event_time = event_time.astimezone(timezone.utc)

                    events.append({
                        "event": str(item.get("title", "USD event")),
                        "currency": "USD",
                        "impact": impact,
                        "time_utc": event_time.isoformat(),
                        "time_eat": event_time.astimezone(EAT).strftime("%Y-%m-%d %H:%M"),
                        "forecast": item.get("forecast", ""),
                        "previous": item.get("previous", ""),
                        "actual": item.get("actual", "")
                    })

                except (ValueError, TypeError, KeyError):
                    continue

            if not events:
                raise ValueError("Calendar returned no valid USD events.")

            # Save successfully downloaded events for future fallback.
            news_cache["time"] = current_time
            news_cache["events"] = events

            try:
                with open(NEWS_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump({
                        "time": current_time,
                        "events": events
                    }, f)
            except OSError as cache_error:
                print("NEWS CACHE SAVE WARNING:", cache_error)

        except Exception as error:
            print("NEWS FEED WARNING:", error)

            if news_cache["events"] is None:
                raise

            events = news_cache["events"]
            stale = True

    upcoming = []
    blocked = None

    for event in events:
        event_time = datetime.fromisoformat(event["time_utc"])
        minutes = (event_time - now).total_seconds() / 60

        if -60 <= minutes <= 7 * 24 * 60:
            upcoming.append(event)

        if event["impact"] == "High" and -15 <= minutes <= 30:
            blocked = event

    upcoming.sort(key=lambda e: e["time_utc"])

    return {
        "status": "STALE" if stale else "CONNECTED",
        "events": upcoming[:20],
        "blocked": blocked,
        "warning": "Using cached calendar data. Verify event times independently." if stale else None,
        "checked_at": now.astimezone(EAT).strftime("%Y-%m-%d %H:%M:%S")
    }


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

    news_error = None
    try:
        news = get_economic_news()
    except Exception as error:
        news = {
            "status": "DISCONNECTED",
            "events": [],
            "blocked": None
        }
        news_error = str(error)

    news_blocked = news["blocked"] is not None

    # Fail safely: do not issue a trade signal without a working calendar.
    if news["status"] != "CONNECTED":
        signal = "WAIT"
        reason = "Economic calendar unavailable. Trading signals paused."
        stop_loss = None
        take_profit = None
        setup_score = 0

    elif news_blocked:
        signal = "WAIT"
        reason = "High-impact USD news window. New trades paused."
        stop_loss = None
        take_profit = None

    news_warning = (
        "HIGH IMPACT USD NEWS: " + news["blocked"]["event"]
        if news_blocked
        else "Calendar disconnected: " + str(news_error)
        if news_error
        else "Economic calendar connected. No active high-impact USD news window."
    )

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
        "news_warning": news_warning,
        "news_status": news["status"],
        "news_blocked": news_blocked,
        "news_events": news["events"],
        "news_checked_at": news.get("checked_at"),
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


@app.route("/api/news")
def api_news():
    try:
        return jsonify(get_economic_news())
    except Exception as error:
        return jsonify({
            "status": "DISCONNECTED",
            "error": str(error),
            "events": []
        }), 503
@app.route("/risk")
def risk_page():
    return render_template("risk.html")
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
