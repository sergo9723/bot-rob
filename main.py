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
# ENV (Render -> Environment)
# =======================
TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "true").lower() == "true"

DEFAULT_SYMBOL = os.getenv("DEFAULT_SYMBOL", "XRPUSDT")
DEFAULT_USD = float(os.getenv("DEFAULT_USD", "3.5"))
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "5"))

DEFAULT_TP_PCT = float(os.getenv("DEFAULT_TP_PCT", "0.55"))  # %
DEFAULT_SL_PCT = float(os.getenv("DEFAULT_SL_PCT", "0.35"))  # %

# Bybit session (Unified Trading)
session = HTTP(
    testnet=BYBIT_TESTNET,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
)

# Cache
_instrument_cache = {}
CACHE_TTL = 60 * 10  # 10 minutes


def ok(msg, **extra):
    data = {"ok": True, "msg": msg}
    data.update(extra)
    return jsonify(data), 200


def bad(msg, code=400, **extra):
    data = {"ok": False, "msg": msg}
    data.update(extra)
    return jsonify(data), code


def _now() -> int:
    return int(time.time())


def get_instrument_filters(symbol: str):
    cached = _instrument_cache.get(symbol)
    if cached and (_now() - cached["ts"] < CACHE_TTL):
        return cached["qty_step"], cached["tick_size"]

    r = session.get_instruments_info(category="linear", symbol=symbol)
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit get_instruments_info error: {r}")

    lst = (r.get("result") or {}).get("list") or []
    if not lst:
        raise RuntimeError(f"Instrument not found: {symbol}")

    item = lst[0]
    lot = item.get("lotSizeFilter") or {}
    pf = item.get("priceFilter") or {}

    qty_step = Decimal(str(lot.get("qtyStep", "0.1")))
    tick_size = Decimal(str(pf.get("tickSize", "0.0001")))

    _instrument_cache[symbol] = {"qty_step": qty_step, "tick_size": tick_size, "ts": _now()}
    return qty_step, tick_size


def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step


def get_last_price(symbol: str) -> Decimal:
    r = session.get_tickers(category="linear", symbol=symbol)
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit get_tickers error: {r}")
    lst = (r.get("result") or {}).get("list") or []
    if not lst:
        raise RuntimeError("No ticker data")
    return Decimal(str(lst[0].get("lastPrice")))


def set_leverage(symbol: str, leverage: int):
    """
    FIX: Bybit 110043 = leverage not modified (already set).
    pybit may raise InvalidRequestError, so we must catch it.
    """
    try:
        r = session.set_leverage(
            category="linear",
            symbol=symbol,
            buyLeverage=str(leverage),
            sellLeverage=str(leverage),
        )

        # If pybit returns a dict, still handle it.
        if isinstance(r, dict) and r.get("retCode") not in (0, 110043):
            raise RuntimeError(f"Bybit set_leverage error: {r}")

    except Exception as e:
        # pybit exception text usually contains ErrCode
        s = str(e)
        if "ErrCode: 110043" in s or "110043" in s:
            logging.info("Leverage already set (%sx) -> continue. (%s)", leverage, s)
            return
        raise


def get_open_position_size(symbol: str) -> float:
    r = session.get_positions(category="linear", symbol=symbol)
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit get_positions error: {r}")

    pos_list = (r.get("result") or {}).get("list") or []
    total = 0.0
    for p in pos_list:
        total += abs(float(p.get("size") or 0))
    return total


def calc_tp_sl_prices(entry_price: Decimal, side: str, tp_pct: float, sl_pct: float, tick_size: Decimal):
    tp_p = Decimal(str(tp_pct)) / Decimal("100")
    sl_p = Decimal(str(sl_pct)) / Decimal("100")

    if side == "Buy":
        tp = entry_price * (Decimal("1") + tp_p)
        sl = entry_price * (Decimal("1") - sl_p)
    else:
        tp = entry_price * (Decimal("1") - tp_p)
        sl = entry_price * (Decimal("1") + sl_p)

    tp = round_down_to_step(tp, tick_size)
    sl = round_down_to_step(sl, tick_size)
    return tp, sl


def place_market_order_with_tpsl(symbol: str, side: str, usd: float, leverage: int, tp_pct: float, sl_pct: float):
    set_leverage(symbol, leverage)

    price = get_last_price(symbol)
    qty_step, tick_size = get_instrument_filters(symbol)

    notional = Decimal(str(usd)) * Decimal(str(leverage))
    raw_qty = notional / price
    qty = round_down_to_step(raw_qty, qty_step)

    if qty <= 0:
        raise RuntimeError(f"Bad qty computed: raw={raw_qty}, step={qty_step}, qty={qty}")

    tp_price, sl_price = calc_tp_sl_prices(price, side, tp_pct, sl_pct, tick_size)

    r = session.place_order(
        category="linear",
        symbol=symbol,
        side=side,  # "Buy"/"Sell"
        orderType="Market",
        qty=str(qty),
        timeInForce="IOC",
        reduceOnly=False,
        takeProfit=str(tp_price),
        stopLoss=str(sl_price),
    )

    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit place_order error: {r}")

    return {
        "symbol": symbol,
        "side": side,
        "entry_price_used": str(price),
        "qty": str(qty),
        "tp_price": str(tp_price),
        "sl_price": str(sl_price),
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "raw": r,
    }


@app.get("/")
def home():
    return "OK", 200


@app.get("/health")
def health():
    return jsonify({"ok": True, "testnet": BYBIT_TESTNET}), 200


@app.post("/webhook")
def webhook():
    try:
        logging.info("Webhook headers: %s", dict(request.headers))
        raw = request.get_data(as_text=True)
        logging.info("Webhook raw body: %s", raw)

        data = request.get_json(silent=True) or {}
        logging.info("Webhook json: %s", data)

        # FIX: if TradingView sent plain text, not JSON
        if not data:
            return bad(
                "Expected JSON body. TradingView alert 'Message' must be JSON.",
                400,
                hint="In TradingView Alert: Condition=Any alert() function call, Message can be empty (alert() sends JSON).",
                got_preview=raw[:200],
            )

        # 1) secret
        secret = str(data.get("secret", "")).strip()
        if not TV_WEBHOOK_SECRET or secret != TV_WEBHOOK_SECRET:
            return bad("Bad secret", 401)

        # 2) symbol
        symbol = str(data.get("symbol", DEFAULT_SYMBOL)).upper().strip()
        if not symbol:
            return bad("Missing symbol", 400)

        # 3) side
        side_raw = str(data.get("side", "")).lower().strip()
        if side_raw in ("buy", "long"):
            side = "Buy"
        elif side_raw in ("sell", "short"):
            side = "Sell"
        else:
            return bad("Bad side. Use buy/sell", 400, got=side_raw)

        usd = float(data.get("usd", DEFAULT_USD))
        leverage = int(data.get("leverage", DEFAULT_LEVERAGE))
        tp_pct = float(data.get("tp_pct", DEFAULT_TP_PCT))
        sl_pct = float(data.get("sl_pct", DEFAULT_SL_PCT))

        if usd <= 0:
            return bad("usd must be > 0", 400)
        if leverage < 1 or leverage > 100:
            return bad("leverage out of range", 400)
        if tp_pct <= 0 or sl_pct <= 0:
            return bad("tp_pct and sl_pct must be > 0", 400)

        # One position per symbol
        open_size = get_open_position_size(symbol)
        if open_size > 0:
            return ok("Position already open -> skip", symbol=symbol, open_size=open_size)

        res = place_market_order_with_tpsl(symbol, side, usd, leverage, tp_pct, sl_pct)
        return ok("Order placed with TP/SL", **res)

    except Exception as e:
        logging.error("WEBHOOK ERROR: %s", str(e))
        logging.error(traceback.format_exc())
        return bad("Exception", 500, error=str(e))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
