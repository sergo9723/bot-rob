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

DEFAULT_TP_PCT = float(os.getenv("DEFAULT_TP_PCT", "0.55"))   # %
DEFAULT_SL_PCT = float(os.getenv("DEFAULT_SL_PCT", "0.35"))   # %

# ✅ NEW (минимальный апгрейд): TP в импульсе
DEFAULT_TP_IMPULSE_PCT = float(os.getenv("DEFAULT_TP_IMPULSE_PCT", "1.2"))  # %

# >>> ADDED: комиссия / фильтры / TP1 / BE / ATR trailing / таймер / reverse
BYBIT_TAKER_FEE_PCT = float(os.getenv("BYBIT_TAKER_FEE_PCT", "0.10"))  # %
DEFAULT_TP1_QTY_PCT = float(os.getenv("DEFAULT_TP1_QTY_PCT", "0.50"))  # 50%
DEFAULT_TP1_PCT = float(os.getenv("DEFAULT_TP1_PCT", "0.30"))          # %
DEFAULT_EARLY_SL_PCT = float(os.getenv("DEFAULT_EARLY_SL_PCT", "0.18"))# %
DEFAULT_BE_OFFSET_PCT = float(os.getenv("DEFAULT_BE_OFFSET_PCT", "0.05"))# %
DEFAULT_ATR_LEN = int(os.getenv("DEFAULT_ATR_LEN", "14"))
DEFAULT_ATR_MULT = float(os.getenv("DEFAULT_ATR_MULT", "1.5"))
DEFAULT_TRAIL_DELAY_SEC = int(os.getenv("DEFAULT_TRAIL_DELAY_SEC", "20"))
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.08"))  # %
AUTO_REVERSE = os.getenv("AUTO_REVERSE", "true").lower() == "true"

# Bybit session (Unified Trading)
session = HTTP(
    testnet=BYBIT_TESTNET,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
)

# Кэш фильтров инструмента
_instrument_cache = {}  # symbol -> dict(filters..., ts)
CACHE_TTL = 60 * 10  # 10 минут

# >>> ADDED: простое состояние (для TP1->BE->trailing)
_position_state = {}  # symbol -> dict(state)


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

    _instrument_cache[symbol] = {
        "qty_step": qty_step,
        "tick_size": tick_size,
        "ts": _now(),
    }
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


# >>> ADDED: bid/ask + spread
def get_bid_ask(symbol: str):
    r = session.get_tickers(category="linear", symbol=symbol)
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit get_tickers error: {r}")
    lst = (r.get("result") or {}).get("list") or []
    if not lst:
        raise RuntimeError("No ticker data")
    item = lst[0]
    bid = Decimal(str(item.get("bid1Price") or item.get("lastPrice")))
    ask = Decimal(str(item.get("ask1Price") or item.get("lastPrice")))
    last = Decimal(str(item.get("lastPrice")))
    return last, bid, ask


def set_leverage(symbol: str, leverage: int):
    """
    110043 = leverage not modified (это НЕ ошибка).
    """
    try:
        r = session.set_leverage(
            category="linear",
            symbol=symbol,
            buyLeverage=str(leverage),
            sellLeverage=str(leverage),
        )
        if r.get("retCode") not in (0, 110043):
            raise RuntimeError(f"Bybit set_leverage error: {r}")
        return
    except Exception as e:
        msg = str(e)
        if ("110043" in msg) or ("leverage not modified" in msg.lower()):
            logging.info("Leverage already set (110043) -> ignore")
            return
        raise


def get_open_position_size(symbol: str) -> float:
    """
    Возвращает суммарный abs(size) по символу.
    """
    r = session.get_positions(category="linear", symbol=symbol)
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit get_positions error: {r}")

    pos_list = (r.get("result") or {}).get("list") or []
    total = 0.0
    for p in pos_list:
        total += abs(float(p.get("size") or 0))
    return total


# >>> ADDED: получить текущую позицию (side/size/avgPrice)
def get_position(symbol: str):
    r = session.get_positions(category="linear", symbol=symbol)
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit get_positions error: {r}")
    pos_list = (r.get("result") or {}).get("list") or []
    for p in pos_list:
        size = Decimal(str(p.get("size") or "0"))
        if abs(size) > 0:
            return {
                "side": str(p.get("side") or ""),      # "Buy"/"Sell"
                "size": size,
                "avgPrice": Decimal(str(p.get("avgPrice") or "0")),
            }
    return None


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


# >>> ADDED: расчёт цены по проценту (универсально)
def price_by_pct(entry_price: Decimal, side: str, pct: float, tick_size: Decimal, direction: str):
    """
    direction: "tp" or "sl" - логика:
      - tp: в сторону прибыли
      - sl: в сторону убытка
    """
    p = Decimal(str(pct)) / Decimal("100")
    if side == "Buy":
        price = entry_price * (Decimal("1") + p) if direction == "tp" else entry_price * (Decimal("1") - p)
    else:
        price = entry_price * (Decimal("1") - p) if direction == "tp" else entry_price * (Decimal("1") + p)
    return round_down_to_step(price, tick_size)


# >>> ADDED: отмена всех ордеров по символу
def cancel_all_orders(symbol: str):
    session.cancel_all_orders(category="linear", symbol=symbol)


# >>> ADDED: лимитный reduceOnly тейк
def place_tp_reduce(symbol: str, side: str, qty: Decimal, price: Decimal):
    close_side = "Sell" if side == "Buy" else "Buy"
    r = session.place_order(
        category="linear",
        symbol=symbol,
        side=close_side,
        orderType="Limit",
        qty=str(qty),
        price=str(price),
        timeInForce="GTC",
        reduceOnly=True,
    )
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit TP reduce order error: {r}")
    return r


# >>> ADDED: установить TP/SL через trading-stop (надёжнее для SL/Trailing)
def set_trading_stop(symbol: str, side: str, tp_price: Decimal | None, sl_price: Decimal | None, trailing_dist: Decimal | None = None):
    """
    trailing_dist: абсолютная дистанция в цене (не %), Bybit ждёт строку.
    """
    args = {
        "category": "linear",
        "symbol": symbol,
        "tpslMode": "Full",
    }
    if tp_price is not None:
        args["takeProfit"] = str(tp_price)
    if sl_price is not None:
        args["stopLoss"] = str(sl_price)
    if trailing_dist is not None and trailing_dist > 0:
        args["trailingStop"] = str(trailing_dist)

    r = session.set_trading_stop(**args)
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit set_trading_stop error: {r}")
    return r


# >>> ADDED: ATR по свечам (из lastPrice) — упрощённый безопасный вариант
def compute_atr_price_distance(symbol: str, tick_size: Decimal) -> Decimal:
    """
    Упрощение: берём recent kline и считаем ATR (Wilder).
    Это нужно для динамического trailing.
    """
    try:
        r = session.get_kline(category="linear", symbol=symbol, interval="1", limit=ATR_LEN + 2)
        if r.get("retCode") != 0:
            raise RuntimeError(f"Bybit get_kline error: {r}")
        kl = (r.get("result") or {}).get("list") or []
        # list обычно в обратном порядке, но нам достаточно пройти как есть
        highs = []
        lows = []
        closes = []
        for row in reversed(kl):
            # формат: [startTime, open, high, low, close, volume, turnover]
            highs.append(Decimal(str(row[2])))
            lows.append(Decimal(str(row[3])))
            closes.append(Decimal(str(row[4])))

        if len(closes) < 3:
            return tick_size

        trs = []
        for i in range(1, len(closes)):
            high = highs[i]
            low = lows[i]
            prev_close = closes[i - 1]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)

        # Wilder ATR:
        atr = trs[0]
        for tr in trs[1:]:
            atr = (atr * Decimal(str(ATR_LEN - 1)) + tr) / Decimal(str(ATR_LEN))

        dist = atr * Decimal(str(DEFAULT_ATR_MULT))
        dist = round_down_to_step(dist, tick_size)
        if dist <= 0:
            dist = tick_size
        return dist
    except Exception as e:
        logging.info("ATR calc fallback due to: %s", str(e))
        return tick_size


# >>> ADDED: основной “менеджер позиции” (TP1 -> BE -> ATR trailing)
def manage_position_after_entry(symbol: str, side: str, entry_price: Decimal, qty: Decimal, tick_size: Decimal, qty_step: Decimal):
    """
    1) ставим TP1 reduceOnly на 50%
    2) ставим Early SL на 100% через set_trading_stop
    3) записываем state для таймера trailing
    """
    tp1_price = price_by_pct(entry_price, side, DEFAULT_TP1_PCT, tick_size, "tp")
    early_sl_price = price_by_pct(entry_price, side, DEFAULT_EARLY_SL_PCT, tick_size, "sl")

    tp1_qty = round_down_to_step(qty * Decimal(str(DEFAULT_TP1_QTY_PCT)), qty_step)
    if tp1_qty <= 0:
        tp1_qty = qty

    # TP1 reduceOnly (частичный тейк)
    place_tp_reduce(symbol, side, tp1_qty, tp1_price)

    # Early SL (на всю позицию)
    set_trading_stop(symbol, side, tp_price=None, sl_price=early_sl_price)

    _position_state[symbol] = {
        "side": side,
        "entry": entry_price,
        "qty": qty,
        "tp1_qty": tp1_qty,
        "tp1_price": tp1_price,
        "tp1_hit": False,
        "trail_enabled": False,
        "trail_enable_at": _now() + int(DEFAULT_TRAIL_DELAY_SEC),
        "last_manage_ts": _now(),
    }

    return {
        "tp1_price": str(tp1_price),
        "tp1_qty": str(tp1_qty),
        "early_sl": str(early_sl_price),
        "trail_enable_at": _position_state[symbol]["trail_enable_at"],
    }


# >>> ADDED: проверка TP1 и переключение SL -> BE + trailing
def update_position_manager(symbol: str):
    """
    Запускается по таймеру (при каждом webhook) — без фоновых потоков.
    """
    st = _position_state.get(symbol)
    if not st:
        return

    pos = get_position(symbol)
    if not pos:
        # позиция закрыта
        _position_state.pop(symbol, None)
        return

    entry = Decimal(str(st["entry"]))
    side = st["side"]
    qty_step, tick_size = get_instrument_filters(symbol)

    # TP1 считаем "исполненным", если size уменьшился хотя бы на ~TP1_QTY_PCT
    # (надёжно без WebSocket)
    size_now = abs(pos["size"])
    qty_initial = Decimal(str(st["qty"]))
    tp1_qty = Decimal(str(st["tp1_qty"]))

    if (not st["tp1_hit"]) and (size_now <= (qty_initial - (tp1_qty * Decimal("0.90")))):
        st["tp1_hit"] = True
        logging.info("TP1 assumed hit for %s. size_now=%s", symbol, str(size_now))

        # отменяем все ордера (чтобы убрать старый TP1/переустановить)
        cancel_all_orders(symbol)

        # BE price (с offset под комиссию)
        be_pct = Decimal(str(DEFAULT_BE_OFFSET_PCT)) + (Decimal(str(BYBIT_TAKER_FEE_PCT)) / Decimal("100"))
        be_price = price_by_pct(entry, side, float(be_pct), tick_size, "tp")  # tp direction = в плюс

        set_trading_stop(symbol, side, tp_price=None, sl_price=be_price)

    # trailing включаем после таймера И после TP1
    if st["tp1_hit"] and (not st["trail_enabled"]) and (_now() >= int(st["trail_enable_at"])):
        # trailing дистанция по ATR
        dist = compute_atr_price_distance(symbol, tick_size)
        set_trading_stop(symbol, side, tp_price=None, sl_price=None, trailing_dist=dist)
        st["trail_enabled"] = True
        logging.info("ATR trailing enabled for %s dist=%s", symbol, str(dist))

    st["last_manage_ts"] = _now()
    _position_state[symbol] = st


def place_market_order_with_tpsl(symbol: str, side: str, usd: float, leverage: int, tp_pct: float, sl_pct: float):
    """
    ОСТАВИЛИ ТВОЙ ВХОД И ТВОЙ TP/SL (как база)
    + ДОБАВИЛИ TP1/BE/ATR/спред/реверс/таймер через отдельные функции.
    """
    set_leverage(symbol, leverage)

    # spread filter (убираем комиссионные входы)
    last, bid, ask = get_bid_ask(symbol)
    spread_pct = (ask - bid) / last * Decimal("100")
    if spread_pct > Decimal(str(MAX_SPREAD_PCT)):
        raise RuntimeError(f"Spread too high: {spread_pct:.4f}% > {MAX_SPREAD_PCT}%")

    price = last
    qty_step, tick_size = get_instrument_filters(symbol)

    notional = Decimal(str(usd)) * Decimal(str(leverage))
    raw_qty = notional / price
    qty = round_down_to_step(raw_qty, qty_step)

    if qty <= 0:
        raise RuntimeError(f"Bad qty computed: raw={raw_qty}, step={qty_step}, qty={qty}")

    # ВХОД (как было)
    r = session.place_order(
        category="linear",
        symbol=symbol,
        side=side,
        orderType="Market",
        qty=str(qty),
        timeInForce="IOC",
        reduceOnly=False,
    )
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit place_order error: {r}")

    # БАЗОВЫЙ TP/SL (оставляем как было, для полной позиции)
    tp_price, sl_price = calc_tp_sl_prices(price, side, tp_pct, sl_pct, tick_size)
    set_trading_stop(symbol, side, tp_price=tp_price, sl_price=sl_price)

    # ДОБАВЛЕННЫЙ менеджер TP1/BE/Trailing (не ломает базу)
    mgr = manage_position_after_entry(symbol, side, price, qty, tick_size, qty_step)

    return {
        "symbol": symbol,
        "side": side,
        "entry_price_used": str(price),
        "qty": str(qty),
        "tp_price": str(tp_price),
        "sl_price": str(sl_price),
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "spread_pct": str(spread_pct),
        "tp1": mgr,
        "raw": r,
    }


@app.get("/")
def home():
    return "OK", 200


@app.get("/health")
def health():
    return jsonify({"ok": True, "testnet": BYBIT_TESTNET}), 200


@app.get("/ready")
def ready():
    """
    ЧЕК-КОД готовности
    """
    try:
        missing = []
        if not TV_WEBHOOK_SECRET:
            missing.append("TV_WEBHOOK_SECRET")
        if not BYBIT_API_KEY:
            missing.append("BYBIT_API_KEY")
        if not BYBIT_API_SECRET:
            missing.append("BYBIT_API_SECRET")

        symbol = DEFAULT_SYMBOL
        price = get_last_price(symbol)
        qty_step, tick_size = get_instrument_filters(symbol)

        return jsonify({
            "ok": True,
            "mode": "TESTNET" if BYBIT_TESTNET else "REAL",
            "missing_env": missing,
            "default_symbol": symbol,
            "last_price": str(price),
            "qty_step": str(qty_step),
            "tick_size": str(tick_size),
            "tp_normal_pct": DEFAULT_TP_PCT,
            "tp_impulse_pct": DEFAULT_TP_IMPULSE_PCT,
            "sl_pct": DEFAULT_SL_PCT,
            "spread_max_pct": MAX_SPREAD_PCT,
            "tp1_pct": DEFAULT_TP1_PCT,
            "tp1_qty_pct": DEFAULT_TP1_QTY_PCT,
            "early_sl_pct": DEFAULT_EARLY_SL_PCT,
            "be_offset_pct": DEFAULT_BE_OFFSET_PCT,
            "atr_len": DEFAULT_ATR_LEN,
            "atr_mult": DEFAULT_ATR_MULT,
            "trail_delay_sec": DEFAULT_TRAIL_DELAY_SEC,
            "auto_reverse": AUTO_REVERSE,
            "bybit_fee_pct": BYBIT_TAKER_FEE_PCT,
        }), 200

    except Exception as e:
        return jsonify({
            "ok": False,
            "mode": "TESTNET" if BYBIT_TESTNET else "REAL",
            "error": str(e),
            "hint": "Check Render logs and verify keys/permissions/symbol"
        }), 500


@app.post("/webhook")
def webhook():
    try:
        logging.info("Webhook headers: %s", dict(request.headers))
        raw = request.get_data(as_text=True)
        logging.info("Webhook raw body: %s", raw)

        if not request.is_json:
            return bad("Expected application/json from TradingView", 415, got_content_type=request.content_type)

        data = request.get_json(silent=True) or {}
        logging.info("Webhook json: %s", data)

        # 1) secret
        secret = str(data.get("secret", ""))
        if (not TV_WEBHOOK_SECRET) or (secret != TV_WEBHOOK_SECRET):
            return bad("Bad secret", 401)

        # 2) symbol
        symbol = str(data.get("symbol", DEFAULT_SYMBOL)).upper().strip()
        if not symbol:
            return bad("Missing symbol", 400)

        # >>> ADDED: обновляем trailing/BE менеджер (таймерный, по каждому вебхуку)
        try:
            update_position_manager(symbol)
        except Exception as e:
            logging.info("Position manager update skipped: %s", str(e))

        # 3) side
        side_raw = str(data.get("side", "")).strip()
        side_l = side_raw.lower()

        if side_l in ("buy", "long"):
            side = "Buy"
        elif side_l in ("sell", "short"):
            side = "Sell"
        else:
            return bad("Bad side. Use BUY/SELL", 400, got=side_raw)

        usd = float(data.get("usd", DEFAULT_USD))
        leverage = int(data.get("leverage", DEFAULT_LEVERAGE))

        # импульсный режим
        impulse = bool(data.get("impulse", False))

        if impulse:
            tp_pct = float(data.get("tp_pct_impulse", DEFAULT_TP_IMPULSE_PCT))
        else:
            tp_pct = float(data.get("tp_pct", DEFAULT_TP_PCT))

        sl_pct = float(data.get("sl_pct", DEFAULT_SL_PCT))

        if usd <= 0:
            return bad("usd must be > 0", 400)
        if leverage < 1 or leverage > 100:
            return bad("leverage out of range", 400)
        if tp_pct <= 0 or sl_pct <= 0:
            return bad("tp_pct and sl_pct must be > 0", 400)

        # >>> ADDED: авто-reverse (если есть позиция и приходит обратный сигнал)
        pos = get_position(symbol)
        if pos:
            pos_side = pos["side"]  # "Buy"/"Sell"
            if AUTO_REVERSE and ((pos_side == "Buy" and side == "Sell") or (pos_side == "Sell" and side == "Buy")):
                cancel_all_orders(symbol)
                close_side = "Sell" if pos_side == "Buy" else "Buy"
                session.place_order(
                    category="linear",
                    symbol=symbol,
                    side=close_side,
                    orderType="Market",
                    qty=str(abs(pos["size"])),
                    timeInForce="IOC",
                    reduceOnly=True,
                )
                # продолжаем — откроем новую
            else:
                return ok("Position already open -> skip", symbol=symbol, open_size=float(abs(pos["size"])), impulse=impulse)

        res = place_market_order_with_tpsl(symbol, side, usd, leverage, tp_pct, sl_pct)
        res["impulse"] = impulse
        return ok("Order placed with TP/SL + TP1/BE/ATR", **res)

    except Exception as e:
        logging.error("WEBHOOK ERROR: %s", str(e))
        logging.error(traceback.format_exc())
        return jsonify({"ok": False, "error": "internal_error", "detail": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
