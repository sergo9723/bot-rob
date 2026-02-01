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

# TP/SL по умолчанию (в процентах)
DEFAULT_TP_PCT = float(os.getenv("DEFAULT_TP_PCT", "0.55"))  # 0.55%
DEFAULT_SL_PCT = float(os.getenv("DEFAULT_SL_PCT", "0.35"))  # 0.35%

# Bybit session (Unified Trading)
session = HTTP(
    testnet=BYBIT_TESTNET,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
)

# Кэш фильтров инструмента
_instrument_cache = {}  # symbol -> dict(filters..., ts)
CACHE_TTL = 60 * 10  # 10 минут


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
    """
    Возвращает qtyStep и tickSize как Decimal для корректного округления.
    """
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
    """
    Округление вниз к кратности step: floor(value/step)*step
    """
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
    r = session.set_leverage(
        category="linear",
        symbol=symbol,
        buyLeverage=str(leverage),
        sellLeverage=str(leverage),
    )
    # Иногда Bybit возвращает код типа "не изменилось" — это не критично
    if r.get("retCode") not in (0, 110043):
        raise RuntimeError(f"Bybit set_leverage error: {r}")


def get_position(symbol: str):
    """
    Возвращает (side, size) по oneway.
    side: "Buy"/"Sell"/None
    size: float
    """
    r = session.get_positions(category="linear", symbol=symbol)
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit get_positions error: {r}")

    pos_list = (r.get("result") or {}).get("list") or []
    # Для oneway обычно одна позиция, но мы аккуратно найдём любую с size>0
    best_side = None
    best_size = 0.0
    for p in pos_list:
        size = float(p.get("size") or 0)
        if size > 0 and size > best_size:
            best_size = size
            best_side = p.get("side")  # "Buy" или "Sell"
    return best_side, best_size


def calc_tp_sl_prices(entry_price: Decimal, side: str, tp_pct: float, sl_pct: float, tick_size: Decimal):
    """
    Расчёт TP/SL в процентах и округление по tickSize.
    side: "Buy" / "Sell"
    """
    tp_p = Decimal(str(tp_pct)) / Decimal("100")
    sl_p = Decimal(str(sl_pct)) / Decimal("100")

    if side == "Buy":
        tp = entry_price * (Decimal("1") + tp_p)
        sl = entry_price * (Decimal("1") - sl_p)
    else:
        tp = entry_price * (Decimal("1") - tp_p)
        sl = entry_price * (Decimal("1") + sl_p)

    # Округлим вниз по tick_size (чтобы Bybit точно принял)
    tp = round_down_to_step(tp, tick_size)
    sl = round_down_to_step(sl, tick_size)

    return tp, sl


def compute_qty(symbol: str, usd: float, leverage: int):
    """
    qty = (usd * leverage) / price, округление по qtyStep
    """
    price = get_last_price(symbol)
    qty_step, _tick_size = get_instrument_filters(symbol)

    notional = Decimal(str(usd)) * Decimal(str(leverage))
    raw_qty = notional / price
    qty = round_down_to_step(raw_qty, qty_step)

    if qty <= 0:
        raise RuntimeError(f"Bad qty computed: raw={raw_qty}, step={qty_step}, qty={qty}")

    return price, qty


def close_position_market(symbol: str, pos_side: str, pos_size: float):
    """
    ПУНКТ 3: закрыть позицию рыночным reduceOnly.
    Если pos_side="Buy" (лонг), закрываем Sell.
    Если pos_side="Sell" (шорт), закрываем Buy.
    """
    if pos_size <= 0:
        return {"closed": False, "reason": "size<=0"}

    close_side = "Sell" if pos_side == "Buy" else "Buy"

    r = session.place_order(
        category="linear",
        symbol=symbol,
        side=close_side,
        orderType="Market",
        qty=str(pos_size),
        timeInForce="IOC",
        reduceOnly=True,
    )
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit close position error: {r}")

    return {"closed": True, "close_side": close_side, "size": pos_size, "raw": r}


def open_market_with_tpsl(symbol: str, side: str, usd: float, leverage: int, tp_pct: float, sl_pct: float):
    """
    Открыть Market + TP/SL.
    """
    set_leverage(symbol, leverage)

    price, qty = compute_qty(symbol, usd, leverage)
    _qty_step, tick_size = get_instrument_filters(symbol)
    tp_price, sl_price = calc_tp_sl_prices(price, side, tp_pct, sl_pct, tick_size)

    r = session.place_order(
        category="linear",
        symbol=symbol,
        side=side,               # "Buy" / "Sell"
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
        # 0) Логируем вход
        logging.info("Webhook headers: %s", dict(request.headers))
        raw = request.get_data(as_text=True)
        logging.info("Webhook raw body: %s", raw)

        data = request.get_json(silent=True) or {}
        logging.info("Webhook json: %s", data)

        # 1) secret
        secret = str(data.get("secret", "")).strip()
        if not TV_WEBHOOK_SECRET or secret != TV_WEBHOOK_SECRET:
            return bad("Bad secret", 401)

        # 2) symbol
        symbol = str(data.get("symbol", DEFAULT_SYMBOL)).upper().strip()
        if not symbol:
            return bad("Missing symbol", 400)

        # 3) side (TradingView: BUY/SELL)
        side_raw = str(data.get("side", "")).strip()
        side_low = side_raw.lower()

        # защита: если пришёл плейсхолдер — сразу ошибка, чтобы ты увидел, что алерт не “order fills”
        if "{{" in side_raw or "}}" in side_raw:
            return bad("Side placeholder not resolved. Create alert from Strategy -> Order fills.", 400, got=side_raw)

        if side_low in ("buy", "long"):
            side = "Buy"
        elif side_low in ("sell", "short"):
            side = "Sell"
        else:
            return bad("Bad side. Use BUY/SELL", 400, got=side_raw)

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

        # === ПУНКТ 1 + ПУНКТ 3: одна позиция + закрытие по противоположному сигналу ===
        pos_side, pos_size = get_position(symbol)

        if pos_side is not None and pos_size > 0:
            # Если уже есть позиция в ту же сторону — пропускаем
            if pos_side == side:
                return ok("Position already open in same direction -> skip", symbol=symbol, pos_side=pos_side, pos_size=pos_size)

            # Если позиция в противоположную — закрываем и открываем новую
            close_res = close_position_market(symbol, pos_side, pos_size)
            time.sleep(0.4)  # дать бирже обновить состояние

            open_res = open_market_with_tpsl(symbol, side, usd, leverage, tp_pct, sl_pct)
            return ok("Closed opposite and opened new with TP/SL", closed=close_res, **open_res)

        # Если позиции нет — просто открываем
        res = open_market_with_tpsl(symbol, side, usd, leverage, tp_pct, sl_pct)
        return ok("Order placed with TP/SL", **res)

    except Exception as e:
        logging.error("WEBHOOK ERROR: %s", str(e))
        logging.error(traceback.format_exc())
        return bad("Exception", 500, error=str(e))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
