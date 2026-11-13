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
CACHE_SECONDS = 300

failure_cache = {}
FAILURE_CACHE_SECONDS = 60
RATE_LIMIT_UNTIL = 0
NEWS_URL = "https://showmytrades.com/api/public/calendar/v1"
NEWS_CACHE_SECONDS = 3600
NEWS_CACHE_FILE = os.path.join(os.path.dirname(__file__), "news_cache.json")

news_cache = {"time": 0, "events": None}
EAT = timezone(timedelta(hours=3))

ALLOWED_INTERVALS = [
    "1min", "5min", "15min", "30min",
    "1h", "2h", "4h", "1day"
]

# Restore saved news calendar.
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

            news_cache["time"] = current_time
            news_cache["events"] = events

            try:
                with open(NEWS_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump({
                        "time": current_time,
                        "events": events
                    }, f)
            except OSError as error:
                print("NEWS CACHE SAVE WARNING:", error)

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
        "warning": (
            "Using cached calendar data. Verify event times independently."
            if stale else None
        ),
        "checked_at": now.astimezone(EAT).strftime("%Y-%m-%d %H:%M:%S")
    }


# ---------------- INDICATORS ----------------

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

    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0

    if avg_gain == 0:
        return 0.0

    return 100 - (100 / (1 + avg_gain / avg_loss))


def macd(values):
    if len(values) < 35:
        return None

    fast_values = []
    slow_values = []

    multiplier_fast = 2 / 13
    multiplier_slow = 2 / 27

    fast = sum(values[:12]) / 12
    slow = sum(values[:26]) / 26

    for i, value in enumerate(values):
        if i >= 12:
            fast = (value - fast) * multiplier_fast + fast
        if i >= 26:
            slow = (value - slow) * multiplier_slow + slow

        if i >= 25:
            fast_values.append(fast)
            slow_values.append(slow)

    line_values = [
        f - s for f, s in zip(fast_values, slow_values)
    ]

    if len(line_values) < 9:
        return None

    signal = ema(line_values, 9)
    line = line_values[-1]

    return {
        "line": round(line, 3),
        "signal": round(signal, 3),
        "histogram": round(line - signal, 3),
        "trend": "BULLISH" if line > signal else "BEARISH"
    }


def atr_value(candles, period=14):
    if len(candles) < period + 1:
        return None

    ranges = []

    for i in range(1, len(candles)):
        candle = candles[i]
        previous_close = candles[i - 1]["close"]

        true_range = max(
            candle["high"] - candle["low"],
            abs(candle["high"] - previous_close),
            abs(candle["low"] - previous_close)
        )
        ranges.append(true_range)

    return sum(ranges[-period:]) / period


def bollinger(values, period=20, deviations=2):
    if len(values) < period:
        return None

    recent = values[-period:]
    middle = sum(recent) / period

    variance = sum((x - middle) ** 2 for x in recent) / period
    standard_deviation = variance ** 0.5

    return {
        "upper": round(middle + deviations * standard_deviation, 3),
        "middle": round(middle, 3),
        "lower": round(middle - deviations * standard_deviation, 3)
    }


def calculate_indicators(candles):
    closes = [c["close"] for c in candles]

    fast = ema(closes, 20)
    medium = ema(closes, 50)
    long = ema(closes, 200)
    momentum = rsi(closes, 14)
    volatility = atr_value(candles, 14)
    macd_data = macd(closes)
    bands = bollinger(closes)

    price = closes[-1]

    volatility_percent = (
        volatility / price * 100
        if volatility is not None and price else None
    )

    if volatility_percent is None:
        volatility_status = "UNKNOWN"
    elif volatility_percent < 0.10:
        volatility_status = "LOW"
    elif volatility_percent < 0.25:
        volatility_status = "MODERATE"
    else:
        volatility_status = "HIGH"

    if fast is not None and medium is not None and long is not None:
        if fast > medium > long:
            trend = "BULLISH"
        elif fast < medium < long:
            trend = "BEARISH"
        else:
            trend = "MIXED"
    else:
        trend = "INSUFFICIENT DATA"

    return {
        "ema20": round(fast, 3) if fast is not None else None,
        "ema50": round(medium, 3) if medium is not None else None,
        "ema200": round(long, 3) if long is not None else None,
        "rsi": round(momentum, 2) if momentum is not None else None,
        "macd": macd_data,
        "bollinger": bands,
        "atr": round(volatility, 3) if volatility is not None else None,
        "volatility_percent": (
            round(volatility_percent, 4)
            if volatility_percent is not None else None
        ),
        "volatility_status": volatility_status,
        "trend": trend
    }


# ---------------- MARKET DATA ----------------


def get_market_data(interval):
    global RATE_LIMIT_UNTIL

    if not API_KEY:
        raise ValueError("Twelve Data API key is not configured.")

    now = time.time()
    key = interval

    # Return successful cached market data.
    if key in cache and now - cache[key]["time"] < CACHE_SECONDS:
        return cache[key]["data"]

    # Avoid repeating recently failed requests.
    if key in failure_cache:
        failed = failure_cache[key]
        if now - failed["time"] < FAILURE_CACHE_SECONDS:
            raise ValueError(failed["error"])

    # Shared cooldown after an API rate-limit response.
    if now < RATE_LIMIT_UNTIL:
        message = "Twelve Data rate limit reached. Requests temporarily paused."
        failure_cache[key] = {
            "time": now,
            "error": message
        }
        raise ValueError(message)

    try:
        response = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": SYMBOL,
                "interval": interval,
                "outputsize": 250,
                "apikey": API_KEY,
                "format": "JSON"
            },
            timeout=20
        )

        if response.status_code == 429:
            RATE_LIMIT_UNTIL = time.time() + 60
            raise ValueError(
                "Twelve Data rate limit reached. Requests paused for 60 seconds."
            )

        if response.status_code >= 400:
            raise ValueError(
                f"Twelve Data request failed (HTTP {response.status_code})."
            )

        data = response.json()

        if not isinstance(data, dict) or "values" not in data:
            raise ValueError(
                "Twelve Data returned no market candles. Check API access and quota."
            )

        candles = list(reversed(data["values"]))

        for candle in candles:
            for field in ("open", "high", "low", "close"):
                candle[field] = float(candle[field])

        if len(candles) < 51:
            raise ValueError("Not enough candles for analysis.")

        cache[key] = {
            "time": time.time(),
            "data": candles
        }

        failure_cache.pop(key, None)

        return candles

    except ValueError as error:
        failure_cache[key] = {
            "time": time.time(),
            "error": str(error)
        }
        raise

    except requests.RequestException:
        message = "Unable to connect to Twelve Data. Check your internet connection."
        failure_cache[key] = {
            "time": time.time(),
            "error": message
        }
        raise ValueError(message)

    except Exception:
        message = "Market data could not be processed."
        failure_cache[key] = {
            "time": time.time(),
            "error": message
        }
        raise ValueError(message)



# ---------------- ICT SMART MONEY ENGINE ----------------

def detect_ict_structure(candles, swing_size=3):
    if len(candles) < (swing_size * 2 + 5):
        raise ValueError("Not enough candles for ICT structure.")

    highs = []
    lows = []

    for i in range(swing_size, len(candles) - swing_size):
        high = candles[i]["high"]
        low = candles[i]["low"]

        if all(high > candles[j]["high"]
               for j in range(i-swing_size, i+swing_size+1) if j != i):
            highs.append({"index": i, "price": high})

        if all(low < candles[j]["low"]
               for j in range(i-swing_size, i+swing_size+1) if j != i):
            lows.append({"index": i, "price": low})

    confirmed_highs = [
        x for x in highs
        if x["index"] + swing_size < len(candles)-1
    ]
    confirmed_lows = [
        x for x in lows
        if x["index"] + swing_size < len(candles)-1
    ]

    last = candles[-1]
    previous = candles[-2]

    latest_high = confirmed_highs[-1] if confirmed_highs else None
    latest_low = confirmed_lows[-1] if confirmed_lows else None

    prior_bias = "NEUTRAL"

    if len(confirmed_highs) >= 2 and len(confirmed_lows) >= 2:
        higher_high = confirmed_highs[-1]["price"] > confirmed_highs[-2]["price"]
        higher_low = confirmed_lows[-1]["price"] > confirmed_lows[-2]["price"]
        lower_high = confirmed_highs[-1]["price"] < confirmed_highs[-2]["price"]
        lower_low = confirmed_lows[-1]["price"] < confirmed_lows[-2]["price"]

        if higher_high and higher_low:
            prior_bias = "BULLISH"
        elif lower_high and lower_low:
            prior_bias = "BEARISH"

    bos = None
    choch = None
    mss = None
    bias = prior_bias
    broken_level = None

    bullish_break = (
        latest_high
        and last["close"] > latest_high["price"]
        and previous["close"] <= latest_high["price"]
    )

    bearish_break = (
        latest_low
        and last["close"] < latest_low["price"]
        and previous["close"] >= latest_low["price"]
    )

    if bullish_break:
        broken_level = latest_high["price"]

        if prior_bias == "BEARISH":
            choch = "BULLISH CHoCH"
        else:
            bos = "BULLISH BOS"

        bias = "BULLISH"

    elif bearish_break:
        broken_level = latest_low["price"]

        if prior_bias == "BULLISH":
            choch = "BEARISH CHoCH"
        else:
            bos = "BEARISH BOS"

        bias = "BEARISH"

    # MSS requires a countertrend structure break with displacement.
    if choch:
        body = abs(last["close"] - last["open"])
        previous_bodies = [
            abs(c["close"] - c["open"])
            for c in candles[-21:-1]
        ]

        average_body = (
            sum(previous_bodies) / len(previous_bodies)
            if previous_bodies else 0
        )

        candle_range = last["high"] - last["low"]

        bullish_displacement = (
            last["close"] > last["open"]
            and candle_range > 0
            and (last["high"] - last["close"]) / candle_range <= 0.25
        )

        bearish_displacement = (
            last["close"] < last["open"]
            and candle_range > 0
            and (last["close"] - last["low"]) / candle_range <= 0.25
        )

        if average_body > 0 and body >= average_body * 1.5:
            if choch == "BULLISH CHoCH" and bullish_displacement:
                mss = "BULLISH MSS"
            elif choch == "BEARISH CHoCH" and bearish_displacement:
                mss = "BEARISH MSS"

    return {
        "bias": bias,
        "prior_bias": prior_bias,
        "bos": bos,
        "choch": choch,
        "mss": mss,
        "broken_level": round(broken_level, 3) if broken_level is not None else None,
        "latest_swing_high": latest_high["price"] if latest_high else None,
        "latest_swing_low": latest_low["price"] if latest_low else None,
        "swing_high_count": len(confirmed_highs),
        "swing_low_count": len(confirmed_lows)
    }


def detect_ict_fvg(candles):
    gaps = []

    for i in range(2, len(candles)):
        first = candles[i-2]
        middle = candles[i-1]
        third = candles[i]

        if third["low"] > first["high"]:
            gaps.append({
                "type": "BULLISH FVG",
                "low": round(first["high"], 3),
                "high": round(third["low"], 3),
                "midpoint": round((first["high"] + third["low"]) / 2, 3),
                "index": i,
                "status": "OPEN"
            })

        elif third["high"] < first["low"]:
            gaps.append({
                "type": "BEARISH FVG",
                "low": round(third["high"], 3),
                "high": round(first["low"], 3),
                "midpoint": round((third["high"] + first["low"]) / 2, 3),
                "index": i,
                "status": "OPEN"
            })

    # Determine whether later candles have filled each gap.
    for gap in gaps:
        for candle in candles[gap["index"]+1:]:
            if candle["low"] <= gap["low"] and candle["high"] >= gap["high"]:
                gap["status"] = "FILLED"
                break

    return gaps[-20:]

def detect_ict_order_blocks(candles, lookback=5):
    """
    Detect order blocks from confirmed BOS/CHoCH events
    with displacement. Uses OHLC candle data only.
    """
    blocks = []

    for i in range(11, len(candles)):
        history = candles[:i + 1]

        try:
            structure = detect_ict_structure(history)
        except ValueError:
            continue

        event = structure.get("bos") or structure.get("choch")

        if not event:
            continue

        bullish = event.startswith("BULLISH")
        bearish = event.startswith("BEARISH")

        if not bullish and not bearish:
            continue

        impulse = candles[i]
        body = abs(impulse["close"] - impulse["open"])

        previous_bodies = [
            abs(c["close"] - c["open"])
            for c in candles[max(0, i - 20):i]
        ]

        if not previous_bodies:
            continue

        average_body = sum(previous_bodies) / len(previous_bodies)
        candle_range = impulse["high"] - impulse["low"]

        if average_body <= 0 or candle_range <= 0:
            continue

        displacement = (
            body >= average_body * 1.5
            and (
                (bullish and impulse["close"] > impulse["open"]
                 and (impulse["high"] - impulse["close"]) / candle_range <= 0.25)
                or
                (bearish and impulse["close"] < impulse["open"]
                 and (impulse["close"] - impulse["low"]) / candle_range <= 0.25)
            )
        )

        if not displacement:
            continue

        origin = None

        for j in range(i - 1, max(-1, i - lookback - 1), -1):
            candle = candles[j]

            if bullish and candle["close"] < candle["open"]:
                origin = (j, candle)
                break

            if bearish and candle["close"] > candle["open"]:
                origin = (j, candle)
                break

        if origin is None:
            continue

        origin_index, ob = origin
        zone_low = ob["low"]
        zone_high = ob["high"]

        status = "FRESH"

        for later in candles[i + 1:]:
            if bullish and later["close"] < zone_low:
                status = "BROKEN"
                break

            if bearish and later["close"] > zone_high:
                status = "BROKEN"
                break

            if later["low"] <= zone_high and later["high"] >= zone_low:
                status = "MITIGATED"

        blocks.append({
            "type": "BULLISH ORDER BLOCK" if bullish else "BEARISH ORDER BLOCK",
            "status": status,
            "high": round(zone_high, 3),
            "low": round(zone_low, 3),
            "midpoint": round((zone_high + zone_low) / 2, 3),
            "datetime": ob["datetime"],
            "confirmation": event,
            "confirmation_datetime": impulse["datetime"]
        })

    return blocks[-10:]


def detect_ict_liquidity(candles, structure):
    if not candles or not structure:
        return []

    last = candles[-1]
    sweeps = []

    swing_high = structure.get("latest_swing_high")
    swing_low = structure.get("latest_swing_low")

    # Buy-side liquidity sweep
    if swing_high is not None:
        if last["high"] > swing_high and last["close"] < swing_high:
            sweeps.append({
                "type": "BSL SWEEP",
                "level": round(swing_high, 3),
                "wick_price": round(last["high"], 3),
                "close": round(last["close"], 3),
                "datetime": last["datetime"]
            })

    # Sell-side liquidity sweep
    if swing_low is not None:
        if last["low"] < swing_low and last["close"] > swing_low:
            sweeps.append({
                "type": "SSL SWEEP",
                "level": round(swing_low, 3),
                "wick_price": round(last["low"], 3),
                "close": round(last["close"], 3),
                "datetime": last["datetime"]
            })

    return sweeps
def analyze_ict(candles):
    structure = detect_ict_structure(candles)
    gaps = detect_ict_fvg(candles)
    sweeps = detect_ict_liquidity(candles, structure)
    order_blocks = detect_ict_order_blocks(candles)

    return {
        "structure": structure,
        "fair_value_gaps": gaps,
        "latest_fvg": gaps[-1] if gaps else None,
        "liquidity_sweeps": sweeps,
        "latest_liquidity_sweep": sweeps[-1] if sweeps else None,
        "order_blocks": order_blocks,
        "latest_order_block": order_blocks[-1] if order_blocks else None,
        "checked_at": candles[-1]["datetime"] if candles else None,
        "warning": "ICT detections are technical evidence, not guaranteed predictions."
    }

# ---------------- AI REVERSAL DETECTOR ----------------

def detect_reversal(candles, indicators):
    if len(candles) < 60:
        return {
            "signal": "NO CONFIRMATION",
            "score": 0,
            "reasons": ["Not enough candle history."]
        }

    recent = candles[-30:]
    previous = candles[-60:-30]

    closes = [c["close"] for c in candles]
    price = closes[-1]
    rsi_now = indicators.get("rsi")

    bullish = 0
    bearish = 0
    bullish_reasons = []
    bearish_reasons = []

    # EMA crossover / momentum shift
    ema20 = indicators.get("ema20")
    ema50 = indicators.get("ema50")

    if ema20 is not None and ema50 is not None:
        if ema20 > ema50:
            bullish += 20
            bullish_reasons.append("EMA20 is above EMA50.")
        elif ema20 < ema50:
            bearish += 20
            bearish_reasons.append("EMA20 is below EMA50.")

    # RSI momentum
    if rsi_now is not None:
        if rsi_now > 50:
            bullish += 15
            bullish_reasons.append("RSI is above 50.")
        elif rsi_now < 50:
            bearish += 15
            bearish_reasons.append("RSI is below 50.")

    # RSI divergence using confirmed swing comparisons
    recent_low = min(recent[:-3], key=lambda c: c["low"])
    previous_low = min(previous, key=lambda c: c["low"])
    recent_high = max(recent[:-3], key=lambda c: c["high"])
    previous_high = max(previous, key=lambda c: c["high"])

    if recent_low["low"] < previous_low["low"] and rsi_now is not None:
        bullish += 20
        bullish_reasons.append("Possible bullish divergence: price made a lower low.")

    if recent_high["high"] > previous_high["high"] and rsi_now is not None:
        bearish += 20
        bearish_reasons.append("Possible bearish divergence: price made a higher high.")

    # Candle body momentum
    last = candles[-1]
    body = last["close"] - last["open"]

    if body > 0:
        bullish += 15
        bullish_reasons.append("Latest candle closed bullish.")
    elif body < 0:
        bearish += 15
        bearish_reasons.append("Latest candle closed bearish.")

    # Price recovery / rejection
    recent_support = min(c["low"] for c in recent)
    recent_resistance = max(c["high"] for c in recent)
    previous_support = min(c["low"] for c in previous)
    previous_resistance = max(c["high"] for c in previous)

    if price > previous_support and price > recent_support:
        bullish += 10
        bullish_reasons.append("Price is holding above recent support.")

    if price < previous_resistance and price < recent_resistance:
        bearish += 10
        bearish_reasons.append("Price is below recent resistance.")

    # Compare recent versus previous price movement
    old_price = candles[-6]["close"]

    if price > old_price:
        bullish += 20
        bullish_reasons.append("Recent price momentum is upward.")
    elif price < old_price:
        bearish += 20
        bearish_reasons.append("Recent price momentum is downward.")

    if bullish >= 65 and bullish > bearish + 10:
        signal = "BULLISH REVERSAL"
        score = bullish
        reasons = bullish_reasons
    elif bearish >= 65 and bearish > bullish + 10:
        signal = "BEARISH REVERSAL"
        score = bearish
        reasons = bearish_reasons
    else:
        signal = "NO CONFIRMATION"
        score = max(bullish, bearish)
        reasons = ["Reversal conditions are not sufficiently aligned."]

    return {
        "signal": signal,
        "score": min(score, 100),
        "bullish_score": min(bullish, 100),
        "bearish_score": min(bearish, 100),
        "reasons": reasons,
        "price": round(price, 3),
        "rsi": rsi_now,
        "checked_at": candles[-1]["datetime"],
        "warning": "Technical evidence only. Reversals are not guaranteed."
    }


# ---------------- MARKET ANALYSIS ----------------

def analyze(candles):
    closes = [c["close"] for c in candles]
    price = closes[-1]

    indicators = calculate_indicators(candles)
    reversal = detect_reversal(candles, indicators)
    ict = analyze_ict(candles)

    fast = indicators["ema20"]
    slow = indicators["ema50"]
    long = indicators["ema200"]
    momentum = indicators["rsi"]
    atr = indicators["atr"]

    recent = candles[-50:]
    support = min(c["low"] for c in recent)
    resistance = max(c["high"] for c in recent)

    bullish_score = 0
    bearish_score = 0

    if fast is not None and slow is not None:
        if fast > slow:
            bullish_score += 25
        elif fast < slow:
            bearish_score += 25

    if long is not None:
        if price > long:
            bullish_score += 20
        elif price < long:
            bearish_score += 20

    if momentum is not None:
        if 50 < momentum < 70:
            bullish_score += 20
        elif 30 < momentum < 50:
            bearish_score += 20
        elif momentum >= 70:
            bullish_score += 5
        elif momentum <= 30:
            bearish_score += 5

    if fast is not None:
        if price > fast:
            bullish_score += 15
        elif price < fast:
            bearish_score += 15

    macd_data = indicators["macd"]

    if macd_data:
        if macd_data["histogram"] > 0:
            bullish_score += 20
        elif macd_data["histogram"] < 0:
            bearish_score += 20

    signal = "WAIT"
    reason = "Technical conditions are not sufficiently aligned."
    setup_score = max(bullish_score, bearish_score)

    if (
        fast is not None and slow is not None
        and momentum is not None
        and price > fast > slow
        and 50 < momentum < 70
        and (long is None or price > long)
        and (macd_data is None or macd_data["histogram"] > 0)
    ):
        signal = "BUY"
        reason = "Bullish EMA, price, RSI and momentum confirmation."
        setup_score = bullish_score

    elif (
        fast is not None and slow is not None
        and momentum is not None
        and price < fast < slow
        and 30 < momentum < 50
        and (long is None or price < long)
        and (macd_data is None or macd_data["histogram"] < 0)
    ):
        signal = "SELL"
        reason = "Bearish EMA, price, RSI and momentum confirmation."
        setup_score = bearish_score

    stop_loss = None
    take_profit = None
    tp1 = None
    tp2 = None
    tp3 = None

    if atr is not None and signal == "BUY":
        stop_loss = price - 1.5 * atr
        tp1 = price + 1.5 * atr
        tp2 = price + 2.25 * atr
        tp3 = price + 3 * atr
        take_profit = tp3

    elif atr is not None and signal == "SELL":
        stop_loss = price + 1.5 * atr
        tp1 = price - 1.5 * atr
        tp2 = price - 2.25 * atr
        tp3 = price - 3 * atr
        take_profit = tp3

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

    # Keep the original fail-safe news rule.
    if news["status"] != "CONNECTED":
        signal = "WAIT"
        reason = "Economic calendar unavailable. Trading signals paused."
        stop_loss = None
        take_profit = None
        tp1 = None
        tp2 = None
        tp3 = None
        setup_score = 0

    elif news_blocked:
        signal = "WAIT"
        reason = "High-impact USD news window. New trades paused."
        stop_loss = None
        take_profit = None
        tp1 = None
        tp2 = None
        tp3 = None

    news_warning = (
        "HIGH IMPACT USD NEWS: " + news["blocked"]["event"]
        if news_blocked
        else "Calendar disconnected: " + str(news_error)
        if news_error
        else "Economic calendar connected. No active high-impact USD news window."
    )

    result = {
        "symbol": SYMBOL,
        "price": round(price, 3),
        "signal": signal,
        "reason": reason,
        "trend": indicators["trend"],
        "setup_score": setup_score,
        "strength": {
            "bullish": bullish_score,
            "bearish": bearish_score,
            "pressure": (
                "BULLISH" if bullish_score > bearish_score
                else "BEARISH" if bearish_score > bullish_score
                else "BALANCED"
            ),
            "difference": abs(bullish_score - bearish_score),
            "warning": "Technical evidence scores, not probabilities or guaranteed predictions."
        },
        "ema20": indicators["ema20"],
        "ema50": indicators["ema50"],
        "ema200": indicators["ema200"],
        "rsi": indicators["rsi"],
        "macd": indicators["macd"],
        "bollinger": indicators["bollinger"],
        "atr": indicators["atr"],
        "volatility_percent": indicators["volatility_percent"],
        "volatility_status": indicators["volatility_status"],
        "reversal": reversal,
        "ict": ict,
        "support": round(support, 3),
        "resistance": round(resistance, 3),
        "entry": round(price, 3) if signal != "WAIT" else None,
        "stop_loss": round(stop_loss, 3) if stop_loss is not None else None,
        "take_profit": round(take_profit, 3) if take_profit is not None else None,
        "tp1": round(tp1, 3) if tp1 is not None else None,
        "tp2": round(tp2, 3) if tp2 is not None else None,
        "tp3": round(tp3, 3) if tp3 is not None else None,
        "news_warning": news_warning,
        "news_status": news["status"],
        "news_blocked": news_blocked,
        "news_events": news["events"],
        "news_checked_at": news.get("checked_at"),
        "candles": candles,
        "updated": candles[-1]["datetime"]
    }

    return result


# ---------------- MULTI-TIMEFRAME ANALYSIS ----------------



# ---------------- TRADING SESSION CLOCK ----------------

@app.route("/api/sessions")
def api_sessions():
    now = datetime.now(timezone.utc)

    definitions = [
        {"name": "ASIAN", "zone": "Asia/Tokyo", "open": 9, "close": 18},
        {"name": "LONDON", "zone": "Europe/London", "open": 8, "close": 17},
        {"name": "NEW YORK", "zone": "America/New_York", "open": 8, "close": 17},
    ]

    sessions = []
    upcoming = []

    for item in definitions:
        tz = ZoneInfo(item["zone"])
        local_today = now.astimezone(tz).date()
        intervals = []

        for offset in range(-1, 10):
            day = local_today + timedelta(days=offset)

            # Forex sessions are normally closed on Saturday and Sunday.
            if day.weekday() >= 5:
                continue

            start = datetime.combine(
                day, datetime.min.time(), tzinfo=tz
            ) + timedelta(hours=item["open"])

            end = datetime.combine(
                day, datetime.min.time(), tzinfo=tz
            ) + timedelta(hours=item["close"])

            intervals.append((start, end))

        active = next(
            ((start, end) for start, end in intervals
             if start <= now < end),
            None
        )

        future_intervals = [
            (start, end) for start, end in intervals if start > now
        ]

        next_interval = min(future_intervals, key=lambda x: x[0]) if future_intervals else None
        next_start = next_interval[0] if next_interval else None

        if active:
            status = "OPEN"
            display_start, display_end = active
        else:
            status = "CLOSED"
            display_start, display_end = next_interval if next_interval else (None, None)

        end_eat = display_end.astimezone(EAT) if display_end else None
        start_eat = display_start

        if next_start:
            upcoming.append({
                "name": item["name"],
                "start": next_start
            })

        sessions.append({
            "name": item["name"],
            "status": status,
            "open_time_eat": (
                start_eat.astimezone(EAT).strftime("%Y-%m-%d %H:%M")
                if start_eat else None
            ),
            "close_time_eat": (
                end_eat.strftime("%Y-%m-%d %H:%M")
                if end_eat else None
            ),
            "next_open_eat": (
                next_start.astimezone(EAT).strftime("%Y-%m-%d %H:%M")
                if next_start else None
            ),
            "closes_at_eat": (
                end_eat.strftime("%Y-%m-%d %H:%M")
                if end_eat else None
            ),
            "timezone": item["zone"]
        })

    upcoming.sort(key=lambda x: x["start"])

    next_session = None
    if upcoming:
        item = upcoming[0]
        seconds = max(0, int((item["start"] - now).total_seconds()))
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60

        next_session = {
            "name": item["name"],
            "opens_at_eat": item["start"].astimezone(EAT).strftime(
                "%Y-%m-%d %H:%M"
            ),
            "countdown": f"{hours}h {minutes}m",
            "seconds": seconds
        }

    return jsonify({
        "sessions": sessions,
        "next_session": next_session,
        "checked_at_eat": datetime.now(EAT).strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": "Africa/Nairobi",
        "note": "Session hours are conventional forex-market hours. Holidays may affect trading."
    })

@app.route("/api/multi-timeframe")
def api_multi_timeframe():
    timeframes = [
        "5min", "15min", "30min",
        "1h", "4h", "1day"
    ]

    results = []
    bullish = 0
    bearish = 0
    errors = []

    for interval in timeframes:
        try:
            candles = get_market_data(interval)
            indicators = calculate_indicators(candles)

            trend = indicators["trend"]

            if trend == "BULLISH":
                bullish += 1
            elif trend == "BEARISH":
                bearish += 1

            results.append({
                "timeframe": interval,
                "trend": trend,
                "price": round(candles[-1]["close"], 3),
                "ema20": indicators["ema20"],
                "ema50": indicators["ema50"],
                "ema200": indicators["ema200"],
                "rsi": indicators["rsi"],
                "volatility": indicators["volatility_status"]
            })

        except Exception as error:
            errors.append({
                "timeframe": interval,
                "error": str(error)
            })

            results.append({
                "timeframe": interval,
                "trend": "UNAVAILABLE"
            })

    if bullish > bearish and bullish >= 3:
        overall = "BULLISH"
    elif bearish > bullish and bearish >= 3:
        overall = "BEARISH"
    else:
        overall = "MIXED"

    return jsonify({
        "symbol": SYMBOL,
        "overall_trend": overall,
        "bullish_timeframes": bullish,
        "bearish_timeframes": bearish,
        "results": results,
        "errors": errors,
        "checked_at": datetime.now(EAT).strftime("%Y-%m-%d %H:%M:%S"),
        "note": "Timeframe agreement is a technical comparison, not a guarantee."
    })


# ---------------- WEBSITE ROUTES ----------------

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/analysis")
def api_analysis():
    interval = request.args.get("interval", "15min")

    if interval not in ALLOWED_INTERVALS:
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
