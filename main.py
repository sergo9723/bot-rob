import os
import time
import logging
import traceback
from decimal import Decimal, ROUND_DOWN

from flask import Flask, request, jsonify
from pybit.unified_trading import HTTP

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# =======================
# ENV
# =======================
TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "true").lower() == "true"

DEFAULT_SYMBOL = os.getenv("DEFAULT_SYMBOL", "XRPUSDT")
DEFAULT_USD = float(os.getenv("DEFAULT_USD", "3.5"))
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "5"))

# === TP / SL ===
DEFAULT_TP_PCT = float(os.getenv("DEFAULT_TP_PCT", "0.55"))
DEFAULT_SL_PCT = float(os.getenv("DEFAULT_SL_PCT", "0.35"))

# === NEW FEATURES ===
DEFAULT_EARLY_SL_PCT = float(os.getenv("DEFAULT_EARLY_SL_PCT", "0.18"))
DEFAULT_TP1_PCT = float(os.getenv("DEFAULT_TP1_PCT", "0.30"))
DEFAULT_TP1_QTY = float(os.getenv("DEFAULT_TP1_QTY", "0.5"))

DEFAULT_BE_OFFSET_PCT = float(os.getenv("DEFAULT_BE_OFFSET_PCT", "0.02"))
DEFAULT_ATR_LEN = int(os.getenv("DEFAULT_ATR_LEN", "14"))
DEFAULT_ATR_MULT = float(os.getenv("DEFAULT_ATR_MULT", "1.5"))

# =======================
# BYBIT SESSION
# =======================
session = HTTP(
    testnet=BYBIT_TESTNET,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
)

# =======================
# HELPERS
# =======================
_instrument_cache = {}
CACHE_TTL = 600


def now():
    return int(time.time())


def get_filters(symbol):
    c = _instrument_cache.get(symbol)
    if c and now() - c["ts"] < CACHE_TTL:
        return c["qty"], c["tick"]

    r = session.get_instruments_info(category="linear", symbol=symbol)
    i = r["result"]["list"][0]
    qty = Decimal(i["lotSizeFilter"]["qtyStep"])
    tick = Decimal(i["priceFilter"]["tickSize"])

    _instrument_cache[symbol] = {"qty": qty, "tick": tick, "ts": now()}
    return qty, tick


def rd(value, step):
    return (value / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step


def last_price(symbol):
    r = session.get_tickers(category="linear", symbol=symbol)
    return Decimal(r["result"]["list"][0]["lastPrice"])


def set_leverage(symbol, lev):
    session.set_leverage(
        category="linear",
        symbol=symbol,
        buyLeverage=str(lev),
        sellLeverage=str(lev),
    )


def open_position(symbol):
    r = session.get_positions(category="linear", symbol=symbol)
    for p in r["result"]["list"]:
        if abs(float(p["size"])) > 0:
            return True
    return False


# =======================
# ORDER HELPERS
# =======================
def market_entry(symbol, side, qty):
    return session.place_order(
        category="linear",
        symbol=symbol,
        side=side,
        orderType="Market",
        qty=str(qty),
        timeInForce="IOC",
        reduceOnly=False
    )


def place_tp(symbol, side, qty, price):
    return session.place_order(
        category="linear",
        symbol=symbol,
        side="Sell" if side == "Buy" else "Buy",
        orderType="Limit",
        qty=str(qty),
        price=str(price),
        timeInForce="GTC",
        reduceOnly=True
    )


def place_sl(symbol, side, qty, price):
    return session.place_order(
        category="linear",
        symbol=symbol,
        side="Sell" if side == "Buy" else "Buy",
        orderType="Market",
        qty=str(qty),
        triggerPrice=str(price),
        triggerDirection=2 if side == "Buy" else 1,
        reduceOnly=True
    )


def cancel_all(symbol):
    session.cancel_all_orders(category="linear", symbol=symbol)


# =======================
# WEBHOOK
# =======================
@app.post("/webhook")
def webhook():
    try:
        data = request.json

        if data.get("secret") != TV_WEBHOOK_SECRET:
            return jsonify({"ok": False, "msg": "bad secret"}), 401

        symbol = data.get("symbol", DEFAULT_SYMBOL)
        side = "Buy" if data.get("side").lower() in ("buy", "long") else "Sell"

        usd = float(data.get("usd", DEFAULT_USD))
        lev = int(data.get("leverage", DEFAULT_LEVERAGE))

        if open_position(symbol):
            return jsonify({"ok": True, "msg": "position exists"})

        set_leverage(symbol, lev)

        price = last_price(symbol)
        qty_step, tick = get_filters(symbol)

        notional = Decimal(str(usd * lev))
        qty = rd(notional / price, qty_step)

        # === ENTRY ===
        market_entry(symbol, side, qty)

        # === TP1 50% ===
        tp1_price = price * (Decimal("1") + Decimal(DEFAULT_TP1_PCT)/100) if side == "Buy" \
            else price * (Decimal("1") - Decimal(DEFAULT_TP1_PCT)/100)
        tp1_price = rd(tp1_price, tick)

        tp1_qty = rd(qty * Decimal(DEFAULT_TP1_QTY), qty_step)
        rest_qty = qty - tp1_qty

        place_tp(symbol, side, tp1_qty, tp1_price)

        # === EARLY SL ===
        early_sl = price * (Decimal("1") - Decimal(DEFAULT_EARLY_SL_PCT)/100) if side == "Buy" \
            else price * (Decimal("1") + Decimal(DEFAULT_EARLY_SL_PCT)/100)
        early_sl = rd(early_sl, tick)

        place_sl(symbol, side, qty, early_sl)

        return jsonify({
            "ok": True,
            "symbol": symbol,
            "side": side,
            "qty": str(qty),
            "tp1_price": str(tp1_price),
            "early_sl": str(early_sl)
        })

    except Exception as e:
        logging.error(traceback.format_exc())
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/")
def home():
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
