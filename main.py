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

# БАЗА (если TV не прислал конкретные параметры)
LONG_TP_PCT = float(os.getenv("LONG_TP_PCT", "0.55"))
LONG_SL_PCT = float(os.getenv("LONG_SL_PCT", "0.35"))
SHORT_TP_PCT = float(os.getenv("SHORT_TP_PCT", "0.55"))
SHORT_SL_PCT = float(os.getenv("SHORT_SL_PCT", "0.35"))

# “импульсный TP” отдельно для сторон
LONG_TP_IMPULSE_PCT = float(os.getenv("LONG_TP_IMPULSE_PCT", "1.2"))
SHORT_TP_IMPULSE_PCT = float(os.getenv("SHORT_TP_IMPULSE_PCT", "1.2"))

# Комиссия / фильтры
BYBIT_TAKER_FEE_PCT = float(os.getenv("BYBIT_TAKER_FEE_PCT", "0.10"))  # %
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.08"))            # %

# TP1 / BE / Trailing (отдельно можно подстроить под “лонг как сейчас” и “шорт как раньше”)
TP1_QTY_PCT = float(os.getenv("TP1_QTY_PCT", "0.50"))      # 50%
LONG_TP1_PCT = float(os.getenv("LONG_TP1_PCT", "0.30"))    # %
SHORT_TP1_PCT = float(os.getenv("SHORT_TP1_PCT", "0.30"))  # %

LONG_EARLY_SL_PCT = float(os.getenv("LONG_EARLY_SL_PCT", "0.18"))
SHORT_EARLY_SL_PCT = float(os.getenv("SHORT_EARLY_SL_PCT", "0.18"))

# BE offset (микро-прибыль, чтобы закрытие не съела комиссия)
LONG_BE_OFFSET_PCT = float(os.getenv("LONG_BE_OFFSET_PCT", "0.05"))
SHORT_BE_OFFSET_PCT = float(os.getenv("SHORT_BE_OFFSET_PCT", "0.05"))

# ATR trailing
ATR_LEN = int(os.getenv("ATR_LEN", "14"))
LONG_ATR_MULT = float(os.getenv("LONG_ATR_MULT", "1.5"))
SHORT_ATR_MULT = float(os.getenv("SHORT_ATR_MULT", "1.5"))
TRAIL_DELAY_SEC = int(os.getenv("TRAIL_DELAY_SEC", "20"))

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

# Состояние менеджера позиций (TP1->BE->Trailing)
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


def get_bid_ask(symbol: str):
    r = session.get_tickers(category="linear", symbol=symbol)
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit get_tickers error: {r}")
    lst = (r.get("result") or {}).get("list") or []
    if not lst:
        raise RuntimeError("No ticker data")
    item = lst[0]
    last = Decimal(str(item.get("lastPrice")))
    bid = Decimal(str(item.get("bid1Price") or item.get("lastPrice")))
    ask = Decimal(str(item.get("ask1Price") or item.get("lastPrice")))
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
    except Exception as e:
        msg = str(e)
        if ("110043" in msg) or ("leverage not modified" in msg.lower()):
            logging.info("Leverage already set (110043) -> ignore")
            return
        raise


def get_position(symbol: str):
    """
    Возвращает позицию или None.
    """
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


def cancel_all_orders(symbol: str):
    session.cancel_all_orders(category="linear", symbol=symbol)


def set_trading_stop(symbol: str, tp_price: Decimal | None, sl_price: Decimal | None, trailing_dist: Decimal | None = None):
    """
    Управление стопами на позиции (SL/TP/Trailing) — надежнее, чем пытаться “склеить” в place_order.
    trailing_dist = абсолютная дистанция в цене.
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


def price_by_pct(entry_price: Decimal, side: str, pct: float, tick_size: Decimal, direction: str):
    """
    direction:
      - "tp": в сторону прибыли
      - "sl": в сторону убытка
    """
    p = Decimal(str(pct)) / Decimal("100")
    if side == "Buy":
        price = entry_price * (Decimal("1") + p) if direction == "tp" else entry_price * (Decimal("1") - p)
    else:
        price = entry_price * (Decimal("1") - p) if direction == "tp" else entry_price * (Decimal("1") + p)
    return round_down_to_step(price, tick_size)


def compute_atr_distance(symbol: str, tick_size: Decimal, atr_len: int, atr_mult: float) -> Decimal:
    """
    ATR Wilder по 1m kline (Bybit).
    Возвращает trailing дистанцию (в цене).
    """
    try:
        r = session.get_kline(category="linear", symbol=symbol, interval="1", limit=atr_len + 2)
        if r.get("retCode") != 0:
            raise RuntimeError(f"Bybit get_kline error: {r}")
        kl = (r.get("result") or {}).get("list") or []
        if len(kl) < atr_len:
            return tick_size

        highs, lows, closes = [], [], []
        for row in reversed(kl):
            highs.append(Decimal(str(row[2])))
            lows.append(Decimal(str(row[3])))
            closes.append(Decimal(str(row[4])))

        trs = []
        for i in range(1, len(closes)):
            high = highs[i]
            low = lows[i]
            prev_close = closes[i - 1]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)

        if not trs:
            return tick_size

        # Wilder smoothing
        atr = trs[0]
        for tr in trs[1:]:
            atr = (atr * Decimal(str(atr_len - 1)) + tr) / Decimal(str(atr_len))

        dist = atr * Decimal(str(atr_mult))
        dist = round_down_to_step(dist, tick_size)
        return dist if dist > 0 else tick_size
    except Exception as e:
        logging.info("ATR calc fallback: %s", str(e))
        return tick_size


def place_tp1_reduce_only(symbol: str, pos_side: str, qty: Decimal, price: Decimal):
    """
    TP1 лимиткой reduceOnly.
    pos_side = "Buy"/"Sell" (сторона позиции)
    """
    close_side = "Sell" if pos_side == "Buy" else "Buy"
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
        raise RuntimeError(f"Bybit TP1 reduceOnly error: {r}")
    return r


def close_position_market(symbol: str, pos_side: str, qty: Decimal):
    """
    Закрыть позицию по рынку reduceOnly.
    """
    close_side = "Sell" if pos_side == "Buy" else "Buy"
    r = session.place_order(
        category="linear",
        symbol=symbol,
        side=close_side,
        orderType="Market",
        qty=str(qty),
        timeInForce="IOC",
        reduceOnly=True,
    )
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit close position error: {r}")
    return r


def manage_after_entry(symbol: str, side: str, entry_price: Decimal, qty: Decimal, tick_size: Decimal, qty_step: Decimal, impulse: bool):
    """
    Ставим:
      - TP1 reduceOnly (50%)
      - Early SL (на всю позицию)
      - базовый TP/SL можно оставить через set_trading_stop (если хочешь)
      - записываем state для BE + trailing
    """
    if side == "Buy":
        tp1_pct = LONG_TP1_PCT
        early_sl_pct = LONG_EARLY_SL_PCT
        be_offset = LONG_BE_OFFSET_PCT
        atr_mult = LONG_ATR_MULT
    else:
        tp1_pct = SHORT_TP1_PCT
        early_sl_pct = SHORT_EARLY_SL_PCT
        be_offset = SHORT_BE_OFFSET_PCT
        atr_mult = SHORT_ATR_MULT

    tp1_price = price_by_pct(entry_price, side, tp1_pct, tick_size, "tp")
    early_sl = price_by_pct(entry_price, side, early_sl_pct, tick_size, "sl")

    tp1_qty = round_down_to_step(qty * Decimal(str(TP1_QTY_PCT)), qty_step)
    if tp1_qty <= 0:
        tp1_qty = qty

    # TP1 reduceOnly
    place_tp1_reduce_only(symbol, side, tp1_qty, tp1_price)

    # Early SL на всю позицию
    set_trading_stop(symbol, tp_price=None, sl_price=early_sl, trailing_dist=None)

    _position_state[symbol] = {
        "side": side,
        "entry": str(entry_price),
        "qty": str(qty),
        "tp1_qty": str(tp1_qty),
        "tp1_price": str(tp1_price),
        "tp1_hit": False,
        "be_set": False,
        "trail_enabled": False,
        "trail_enable_at": _now() + int(TRAIL_DELAY_SEC),
        "atr_mult": str(atr_mult),
        "be_offset": str(be_offset),
        "impulse": bool(impulse),
        "last_ts": _now(),
    }

    return {
        "tp1_price": str(tp1_price),
        "tp1_qty": str(tp1_qty),
        "early_sl": str(early_sl),
        "trail_enable_at": int(_position_state[symbol]["trail_enable_at"]),
    }


def update_position_manager(symbol: str):
    """
    Вызывается при каждом webhook.
    Делает:
      - detect TP1 by size shrink
      - SL -> BE
      - trailing after delay and after TP1
    """
    st = _position_state.get(symbol)
    if not st:
        return

    pos = get_position(symbol)
    if not pos:
        _position_state.pop(symbol, None)
        return

    side = st["side"]
    entry = Decimal(st["entry"])
    qty_initial = Decimal(st["qty"])
    tp1_qty = Decimal(st["tp1_qty"])

    qty_step, tick_size = get_instrument_filters(symbol)
    size_now = abs(pos["size"])

    # TP1 “считаем исполненным”, если размер уменьшился примерно на TP1
    if (not st["tp1_hit"]) and (size_now <= (qty_initial - (tp1_qty * Decimal("0.90")))):
        st["tp1_hit"] = True
        logging.info("TP1 assumed hit for %s (size_now=%s)", symbol, str(size_now))

        # Убираем старые лимитки/мусор
        cancel_all_orders(symbol)

        # BE = entry + offset + fee_buffer (в сторону прибыли)
        be_offset = Decimal(st["be_offset"]) / Decimal("100")
        fee_buffer = Decimal(str(BYBIT_TAKER_FEE_PCT)) / Decimal("100") / Decimal("100")  # 0.10% -> 0.0010
        total_pct = (be_offset + fee_buffer) * Decimal("100")  # обратно в %

        be_price = price_by_pct(entry, side, float(total_pct), tick_size, "tp")
        set_trading_stop(symbol, tp_price=None, sl_price=be_price, trailing_dist=None)
        st["be_set"] = True

    # trailing только после TP1 и после таймера
    if st["tp1_hit"] and (not st["trail_enabled"]) and (_now() >= int(st["trail_enable_at"])):
        atr_mult = float(st["atr_mult"])
        dist = compute_atr_distance(symbol, tick_size, ATR_LEN, atr_mult)
        set_trading_stop(symbol, tp_price=None, sl_price=None, trailing_dist=dist)
        st["trail_enabled"] = True
        logging.info("ATR trailing enabled for %s dist=%s", symbol, str(dist))

    st["last_ts"] = _now()
    _position_state[symbol] = st


def calc_qty_from_usd(symbol: str, usd: float, leverage: int, price: Decimal) -> Decimal:
    qty_step, _ = get_instrument_filters(symbol)
    notional = Decimal(str(usd)) * Decimal(str(leverage))
    raw_qty = notional / price
    qty = round_down_to_step(raw_qty, qty_step)
    return qty


def place_entry(symbol: str, side: str, usd: float, leverage: int, impulse: bool):
    """
    Вход + менеджер TP1/BE/trailing.
    """
    set_leverage(symbol, leverage)

    last, bid, ask = get_bid_ask(symbol)
    spread_pct = (ask - bid) / last * Decimal("100")
    if spread_pct > Decimal(str(MAX_SPREAD_PCT)):
        raise RuntimeError(f"Spread too high: {spread_pct:.4f}% > {MAX_SPREAD_PCT}%")

    price = last
    qty_step, tick_size = get_instrument_filters(symbol)

    qty = calc_qty_from_usd(symbol, usd, leverage, price)
    if qty <= 0:
        raise RuntimeError("Bad qty computed")

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

    mgr = manage_after_entry(symbol, side, price, qty, tick_size, qty_step, impulse)

    return {
        "symbol": symbol,
        "side": side,
        "entry_price_used": str(price),
        "qty": str(qty),
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
    try:
        missing = []
        if not TV_WEBHOOK_SECRET:
            missing.append("TV_WEBHOOK_SECRET")
        if not BYBIT_API_KEY:
            missing.append("BYBIT_API_KEY")
        if not BYBIT_API_SECRET:
            missing.append("BYBIT_API_SECRET")

        symbol = DEFAULT_SYMBOL
        qty_step, tick_size = get_instrument_filters(symbol)

        return jsonify({
            "ok": True,
            "mode": "TESTNET" if BYBIT_TESTNET else "REAL",
            "missing_env": missing,
            "default_symbol": symbol,
            "qty_step": str(qty_step),
            "tick_size": str(tick_size),
            "max_spread_pct": MAX_SPREAD_PCT,
            "fee_pct": BYBIT_TAKER_FEE_PCT,
            "tp1_qty_pct": TP1_QTY_PCT,
            "trail_delay_sec": TRAIL_DELAY_SEC,
            "atr_len": ATR_LEN,
            "auto_reverse": AUTO_REVERSE,
            "long": {
                "tp_pct": LONG_TP_PCT,
                "sl_pct": LONG_SL_PCT,
                "tp1_pct": LONG_TP1_PCT,
                "early_sl_pct": LONG_EARLY_SL_PCT,
                "be_offset_pct": LONG_BE_OFFSET_PCT,
                "atr_mult": LONG_ATR_MULT,
                "tp_impulse_pct": LONG_TP_IMPULSE_PCT,
            },
            "short": {
                "tp_pct": SHORT_TP_PCT,
                "sl_pct": SHORT_SL_PCT,
                "tp1_pct": SHORT_TP1_PCT,
                "early_sl_pct": SHORT_EARLY_SL_PCT,
                "be_offset_pct": SHORT_BE_OFFSET_PCT,
                "atr_mult": SHORT_ATR_MULT,
                "tp_impulse_pct": SHORT_TP_IMPULSE_PCT,
            }
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/webhook")
def webhook():
    try:
        if not request.is_json:
            return bad("Expected application/json", 415, got_content_type=request.content_type)

        data = request.get_json(silent=True) or {}
        logging.info("Webhook json: %s", data)

        # secret
        secret = str(data.get("secret", ""))
        if (not TV_WEBHOOK_SECRET) or (secret != TV_WEBHOOK_SECRET):
            return bad("Bad secret", 401)

        symbol = str(data.get("symbol", DEFAULT_SYMBOL)).upper().strip()
        if not symbol:
            return bad("Missing symbol", 400)

        # обновляем менеджер позиции на каждом webhook
        try:
            update_position_manager(symbol)
        except Exception as e:
            logging.info("update_position_manager skipped: %s", str(e))

        side_raw = str(data.get("side", "")).strip().lower()
        if side_raw in ("buy", "long"):
            side = "Buy"
        elif side_raw in ("sell", "short"):
            side = "Sell"
        else:
            return bad("Bad side. Use BUY/SELL", 400, got=side_raw)

        usd = float(data.get("usd", DEFAULT_USD))
        leverage = int(data.get("leverage", DEFAULT_LEVERAGE))
        impulse = bool(data.get("impulse", False))

        if usd <= 0:
            return bad("usd must be > 0", 400)
        if leverage < 1 or leverage > 100:
            return bad("leverage out of range", 400)

        # Если TV прислал конкретные параметры — используем их, иначе берём по стороне из ENV.
        # (это ключ к “лонг как сейчас” + “шорт как раньше”)
        if side == "Buy":
            default_tp = LONG_TP_IMPULSE_PCT if impulse else LONG_TP_PCT
            default_sl = LONG_SL_PCT
        else:
            default_tp = SHORT_TP_IMPULSE_PCT if impulse else SHORT_TP_PCT
            default_sl = SHORT_SL_PCT

        tp_pct = float(data.get("tp_pct", default_tp))
        sl_pct = float(data.get("sl_pct", default_sl))

        # Позиция есть?
        pos = get_position(symbol)
        if pos:
            pos_side = pos["side"]

            # авто-reverse
            if AUTO_REVERSE and ((pos_side == "Buy" and side == "Sell") or (pos_side == "Sell" and side == "Buy")):
                logging.info("AUTO_REVERSE: closing %s and opening %s", pos_side, side)
                cancel_all_orders(symbol)
                close_position_market(symbol, pos_side, abs(pos["size"]))
                _position_state.pop(symbol, None)
                res = place_entry(symbol, side, usd, leverage, impulse)
                return ok("Reversed: closed old & opened new", **res)

            # иначе просто пропускаем
            return ok("Position already open -> skip", symbol=symbol, pos_side=pos_side, size=str(pos["size"]))

        # НОВЫЙ ВХОД
        res = place_entry(symbol, side, usd, leverage, impulse)
        res["tp_pct_used_for_info"] = tp_pct
        res["sl_pct_used_for_info"] = sl_pct
        res["impulse"] = impulse
        return ok("Order placed (TP1+BE+ATR trailing manager)", **res)

    except Exception as e:
        logging.error("WEBHOOK ERROR: %s", str(e))
        logging.error(traceback.format_exc())
        return jsonify({"ok": False, "error": "internal_error", "detail": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
