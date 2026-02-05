import os
import time
import logging
import traceback
from decimal import Decimal, ROUND_DOWN

from flask import Flask, request, jsonify
from pybit.unified_trading import HTTP

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# =====================================================
# ENV
# =====================================================
TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "true").lower() == "true"

DEFAULT_SYMBOL = "XRPUSDT"
DEFAULT_USD = float(os.getenv("DEFAULT_USD", "3.5"))
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "5"))

# === COMMISSION (Bybit taker ≈ 0.1%) ===
COMMISSION_PCT = Decimal("0.10")

# === TP / SL базовые ===
TP_PCT = Decimal("0.55")
EARLY_SL_PCT = Decimal("0.18")

# === TP1 ===
TP1_PCT = Decimal("0.30")
TP1_QTY_PCT = Decimal("0.50")

# === BE ===
BE_OFFSET_PCT = Decimal("0.05")  # перекрывает комиссию

# === ATR trailing ===
ATR_LEN = 14
ATR_MULT = Decimal("1.5")
ATR_TRAIL_DELAY_SEC = 20  # таймер перед включением trailing

# === Spread filter ===
MAX_SPREAD_PCT = Decimal("0.08")  # защита от плохих входов

# =====================================================
# BYBIT SESSION
# =====================================================
session = HTTP(
    testnet=BYBIT_TESTNET,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
)

# =====================================================
# HELPERS
# =====================================================
_instrument_cache = {}
_position_state = {}  # symbol -> state


def now():
    return int(time.time())


def get_filters(symbol):
    c = _instrument_cache.get(symbol)
    if c and now() - c["ts"] < 600:
        return c["qty"], c["tick"]

    r = session.get_instruments_info(category="linear", symbol=symbol)
    i = r["result"]["list"][0]
    qty = Decimal(i["lotSizeFilter"]["qtyStep"])
    tick = Decimal(i["priceFilter"]["tickSize"])

    _instrument_cache[symbol] = {"qty": qty, "tick": tick, "ts": now()}
    return qty, tick


def rd(val, step):
    return (val / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step


def get_ticker(symbol):
    r = session.get_tickers(category="linear", symbol=symbol)
    t = r["result"]["list"][0]
    return Decimal(t["lastPrice"]), Decimal(t["bid1Price"]), Decimal(t["ask1Price"])


def spread_ok(symbol):
    last, bid, ask = get_ticker(symbol)
    spread_pct = (ask - bid) / last * 100
    return spread_pct <= MAX_SPREAD_PCT


def set_leverage(symbol, lev):
    try:
        session.set_leverage(
            category="linear",
            symbol=symbol,
            buyLeverage=str(lev),
            sellLeverage=str(lev),
        )
    except Exception:
        pass


def get_position(symbol):
    r = session.get_positions(category="linear", symbol=symbol)
    for p in r["result"]["list"]:
        if abs(Decimal(p["size"])) > 0:
            return p
    return None


def cancel_all(symbol):
    session.cancel_all_orders(category="linear", symbol=symbol)


# =====================================================
# ORDER LOGIC
# =====================================================
def place_market(symbol, side, qty):
    return session.place_order(
        category="linear",
        symbol=symbol,
        side=side,
        orderType="Market",
        qty=str(qty),
        timeInForce="IOC",
        reduceOnly=False,
    )


def place_limit(symbol, side, qty, price):
    return session.place_order(
        category="linear",
        symbol=symbol,
        side=side,
        orderType="Limit",
        qty=str(qty),
        price=str(price),
        timeInForce="GTC",
        reduceOnly=True,
    )


def place_sl(symbol, side, qty, trigger):
    return session.place_order(
        category="linear",
        symbol=symbol,
        side=side,
        orderType="Market",
        qty=str(qty),
        triggerPrice=str(trigger),
        triggerDirection=1 if side == "Sell" else 2,
        reduceOnly=True,
    )


# =====================================================
# WEBHOOK
# =====================================================
@app.post("/webhook")
def webhook():
    try:
        data = request.json
        if data.get("secret") != TV_WEBHOOK_SECRET:
            return jsonify({"ok": False}), 401

        symbol = data.get("symbol", DEFAULT_SYMBOL)
        side = "Buy" if data["side"].lower() == "buy" else "Sell"

        if not spread_ok(symbol):
            return jsonify({"ok": True, "skip": "bad spread"})

        pos = get_position(symbol)

        # === AUTO REVERSE ===
        if pos:
            if (pos["side"] == "Buy" and side == "Sell") or (pos["side"] == "Sell" and side == "Buy"):
                cancel_all(symbol)
                session.place_order(
                    category="linear",
                    symbol=symbol,
                    side="Sell" if pos["side"] == "Buy" else "Buy",
                    orderType="Market",
                    qty=pos["size"],
                    reduceOnly=True,
                )
            else:
                return jsonify({"ok": True, "skip": "same direction"})

        set_leverage(symbol, DEFAULT_LEVERAGE)

        last, _, _ = get_ticker(symbol)
        qty_step, tick = get_filters(symbol)

        notional = Decimal(DEFAULT_USD * DEFAULT_LEVERAGE)
        qty = rd(notional / last, qty_step)

        # === ENTRY ===
        place_market(symbol, side, qty)

        entry_price = last

        # === TP1 ===
        tp1_price = entry_price * (1 + TP1_PCT/100) if side == "Buy" else entry_price * (1 - TP1_PCT/100)
        tp1_price = rd(tp1_price, tick)
        tp1_qty = rd(qty * TP1_QTY_PCT, qty_step)

        place_limit(symbol, "Sell" if side == "Buy" else "Buy", tp1_qty, tp1_price)

        # === EARLY SL ===
        early_sl = entry_price * (1 - EARLY_SL_PCT/100) if side == "Buy" else entry_price * (1 + EARLY_SL_PCT/100)
        early_sl = rd(early_sl, tick)

        place_sl(symbol, "Sell" if side == "Buy" else "Buy", qty, early_sl)

        # === SAVE STATE ===
        _position_state[symbol] = {
            "side": side,
            "entry": entry_price,
            "qty": qty,
            "tp1_hit": False,
            "trail_start": now() + ATR_TRAIL_DELAY_SEC,
        }

        return jsonify({"ok": True})

    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/")
def home():
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
