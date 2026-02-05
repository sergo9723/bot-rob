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

# =======================
# ✅ ADDED: FEES / FILTERS / TP1 / BE / ATR TRAIL / REVERSE (ENV)
# =======================
BYBIT_FEE_PCT = float(os.getenv("BYBIT_FEE_PCT", "0.10"))  # 0.10% per side (taker). total ~0.20%

COST_FILTER_ENABLED = os.getenv("COST_FILTER_ENABLED", "true").lower() == "true"
MIN_EDGE_PCT = float(os.getenv("MIN_EDGE_PCT", "0.20"))  # tp must be >= fee_total + this

BAD_ENTRY_FILTER_ENABLED = os.getenv("BAD_ENTRY_FILTER_ENABLED", "true").lower() == "true"
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.06"))  # % (пример: 0.06 = 0.06%)
MAX_BAR_RANGE_ATR = float(os.getenv("MAX_BAR_RANGE_ATR", "2.5"))  # range <= ATR*X

TP1_ENABLED = os.getenv("TP1_ENABLED", "true").lower() == "true"
TP1_PCT = float(os.getenv("TP1_PCT", "0.50"))  # TP1 distance in %
TP1_QTY_PCT = float(os.getenv("TP1_QTY_PCT", "0.50"))  # close 50%
TP1_ORDER_TYPE = os.getenv("TP1_ORDER_TYPE", "Limit")  # Limit recommended

BE_ENABLED = os.getenv("BE_ENABLED", "true").lower() == "true"
BE_ARM_AFTER_TP1 = os.getenv("BE_ARM_AFTER_TP1", "true").lower() == "true"
BE_OFFSET_PCT = float(os.getenv("BE_OFFSET_PCT", "0.00"))  # move SL to entry +/- offset%

ATR_TRAIL_ENABLED = os.getenv("ATR_TRAIL_ENABLED", "true").lower() == "true"
ATR_LEN = int(os.getenv("ATR_LEN", "14"))
ATR_MULT = float(os.getenv("ATR_MULT", "1.2"))
ATR_TRAIL_START_PCT = float(os.getenv("ATR_TRAIL_START_PCT", "0.15"))  # start trailing after +0.15%
ATR_TRAIL_TIMER_BARS = int(os.getenv("ATR_TRAIL_TIMER_BARS", "10"))  # wait N 1m bars before enabling trail updates
ATR_TF = os.getenv("ATR_TF", "1")  # bybit kline interval: "1"

REVERSE_ENABLED = os.getenv("REVERSE_ENABLED", "true").lower() == "true"
REVERSE_ONLY_IF_NOT_IN_LOSS = os.getenv("REVERSE_ONLY_IF_NOT_IN_LOSS", "true").lower() == "true"
REVERSE_MAX_LOSS_PCT = float(os.getenv("REVERSE_MAX_LOSS_PCT", "0.05"))  # do not reverse if unrealized loss > 0.05%

# Bybit session (Unified Trading)
session = HTTP(
    testnet=BYBIT_TESTNET,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
)

# Кэш фильтров инструмента
_instrument_cache = {}  # symbol -> dict(filters..., ts)
CACHE_TTL = 60 * 10  # 10 минут

# =======================
# ✅ ADDED: in-memory trade state (for TP1/BE/ATR trailing)
# =======================
_trade_state = {}  # symbol -> dict(state)


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


# =======================
# ✅ ADDED: bid/ask spread + impulse checks
# =======================
def get_bid_ask(symbol: str):
    r = session.get_tickers(category="linear", symbol=symbol)
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit get_tickers error: {r}")
    lst = (r.get("result") or {}).get("list") or []
    if not lst:
        raise RuntimeError("No ticker data")
    t = lst[0]
    bid = Decimal(str(t.get("bid1Price") or "0"))
    ask = Decimal(str(t.get("ask1Price") or "0"))
    last = Decimal(str(t.get("lastPrice") or "0"))
    return bid, ask, last


def get_klines(symbol: str, interval: str, limit: int = 200):
    r = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit get_kline error: {r}")
    return (r.get("result") or {}).get("list") or []


def calc_atr_from_klines(klines, length: int) -> Decimal:
    """
    klines item format (Bybit): [startTime, open, high, low, close, volume, turnover]
    Returned list is usually reverse-chronological; we normalize.
    """
    if not klines or len(klines) < length + 2:
        return Decimal("0")

    # normalize to chronological
    k = list(reversed(klines))
    highs = [Decimal(str(x[2])) for x in k]
    lows = [Decimal(str(x[3])) for x in k]
    closes = [Decimal(str(x[4])) for x in k]

    trs = []
    for i in range(1, len(k)):
        h = highs[i]
        l = lows[i]
        pc = closes[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)

    if len(trs) < length:
        return Decimal("0")

    # simple ATR (SMA of TR)
    window = trs[-length:]
    atr = sum(window) / Decimal(str(length))
    return atr


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


# =======================
# ✅ ADDED: get position direction + entry price + unrealized PnL%
# =======================
def get_position_info(symbol: str):
    r = session.get_positions(category="linear", symbol=symbol)
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit get_positions error: {r}")

    pos_list = (r.get("result") or {}).get("list") or []
    # Bybit returns long/short entries; we choose the one with size>0
    for p in pos_list:
        size = Decimal(str(p.get("size") or "0"))
        if size > 0:
            side = str(p.get("side") or "")
            entry = Decimal(str(p.get("avgPrice") or "0"))
            mark = Decimal(str(p.get("markPrice") or "0"))
            upnl = Decimal(str(p.get("unrealisedPnl") or "0"))
            value = Decimal(str(p.get("positionValue") or "0"))
            pnl_pct = Decimal("0")
            if value > 0:
                pnl_pct = (upnl / value) * Decimal("100")
            return {
                "side": side,         # "Buy" or "Sell" in our mapping sense; Bybit side might be "Buy"/"Sell"
                "size": size,
                "entry": entry,
                "mark": mark,
                "upnl": upnl,
                "pnl_pct": pnl_pct
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


# =======================
# ✅ ADDED: TP1 price calc + order placement + SL update
# =======================
def calc_tp1_price(entry_price: Decimal, side: str, tp1_pct: float, tick_size: Decimal):
    p = Decimal(str(tp1_pct)) / Decimal("100")
    if side == "Buy":
        tp1 = entry_price * (Decimal("1") + p)
    else:
        tp1 = entry_price * (Decimal("1") - p)
    return round_down_to_step(tp1, tick_size)


def place_reduce_only_tp1(symbol: str, side: str, qty: Decimal, tp1_price: Decimal):
    """
    side is position side "Buy"/"Sell". For reduce-only TP, we must send opposite side.
    """
    close_side = "Sell" if side == "Buy" else "Buy"
    r = session.place_order(
        category="linear",
        symbol=symbol,
        side=close_side,
        orderType=TP1_ORDER_TYPE,
        qty=str(qty),
        price=str(tp1_price) if TP1_ORDER_TYPE.lower() == "limit" else None,
        timeInForce="GTC" if TP1_ORDER_TYPE.lower() == "limit" else "IOC",
        reduceOnly=True,
    )
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit TP1 place_order error: {r}")
    return r


def set_position_sl(symbol: str, new_sl: Decimal):
    r = session.set_trading_stop(
        category="linear",
        symbol=symbol,
        stopLoss=str(new_sl),
    )
    if r.get("retCode") != 0:
        raise RuntimeError(f"Bybit set_trading_stop error: {r}")
    return r


def close_position_market(symbol: str, pos_side: str, qty: Decimal):
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


def passes_cost_filter(tp_pct: float) -> bool:
    if not COST_FILTER_ENABLED:
        return True
    fee_total = BYBIT_FEE_PCT * 2.0
    # require tp >= fee_total + min_edge
    return tp_pct >= (fee_total + MIN_EDGE_PCT)


def passes_bad_entry_filters(symbol: str) -> (bool, dict):
    info = {}
    if not BAD_ENTRY_FILTER_ENABLED:
        return True, info

    bid, ask, last = get_bid_ask(symbol)
    if bid <= 0 or ask <= 0 or last <= 0:
        return True, {"note": "no bid/ask -> skip filter"}

    mid = (bid + ask) / Decimal("2")
    spread_pct = (ask - bid) / mid * Decimal("100")
    info["spread_pct"] = float(spread_pct)

    if float(spread_pct) > MAX_SPREAD_PCT:
        return False, info

    # impulse filter: last 1m candle range vs ATR
    kl = get_klines(symbol, ATR_TF, limit=max(ATR_LEN + 50, 80))
    atr = calc_atr_from_klines(kl, ATR_LEN)
    info["atr"] = float(atr)

    if atr > 0 and kl:
        last_k = kl[0]  # most recent
        hi = Decimal(str(last_k[2]))
        lo = Decimal(str(last_k[3]))
        rng = hi - lo
        info["last_range"] = float(rng)

        if rng > atr * Decimal(str(MAX_BAR_RANGE_ATR)):
            return False, info

    return True, info


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
        side=side,
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


# =======================
# ✅ ADDED: background manager (TP1 fill -> BE, ATR trailing updates)
# =======================
def ensure_trade_manager_running():
    # one lightweight thread, started lazily
    if getattr(ensure_trade_manager_running, "_started", False):
        return
    ensure_trade_manager_running._started = True

    import threading

    def loop():
        while True:
            try:
                for symbol in list(_trade_state.keys()):
                    st = _trade_state.get(symbol) or {}
                    if not st.get("active"):
                        continue

                    pos = get_position_info(symbol)
                    if not pos:
                        # position closed
                        st["active"] = False
                        _trade_state[symbol] = st
                        continue

                    qty_step, tick_size = get_instrument_filters(symbol)

                    # ---- TP1 filled? (we mark TP1 as "armed"; if position size <= initial*(1-TP1_QTY_PCT/2) we assume TP1 got some fill)
                    initial_qty = Decimal(str(st.get("initial_qty") or "0"))
                    cur_qty = pos["size"]

                    if TP1_ENABLED and st.get("tp1_placed") and (not st.get("tp1_done")):
                        # crude but robust: if size reduced by ~TP1_QTY_PCT, we consider TP1 done
                        target_remain = initial_qty * (Decimal("1") - Decimal(str(TP1_QTY_PCT)) * Decimal("0.8"))
                        if cur_qty <= target_remain:
                            st["tp1_done"] = True
                            st["tp1_done_ts"] = _now()
                            _trade_state[symbol] = st
                            logging.info("TP1 likely filled -> arm BE/Trail for %s", symbol)

                    # ---- BE after TP1
                    if BE_ENABLED and BE_ARM_AFTER_TP1 and st.get("tp1_done") and (not st.get("be_done")):
                        entry = pos["entry"]
                        offset = Decimal(str(BE_OFFSET_PCT)) / Decimal("100")
                        if pos["side"] == "Buy":
                            be_sl = entry * (Decimal("1") + offset)
                        else:
                            be_sl = entry * (Decimal("1") - offset)
                        be_sl = round_down_to_step(be_sl, tick_size)

                        set_position_sl(symbol, be_sl)
                        st["be_done"] = True
                        st["last_sl"] = str(be_sl)
                        _trade_state[symbol] = st
                        logging.info("BE set for %s -> %s", symbol, be_sl)

                    # ---- ATR trailing (after timer + after price moved in favor)
                    if ATR_TRAIL_ENABLED:
                        entry_ts = int(st.get("entry_ts") or 0)
                        if entry_ts > 0:
                            minutes_in_trade = max(0, (_now() - entry_ts) // 60)
                        else:
                            minutes_in_trade = 0

                        # timer gate
                        if minutes_in_trade >= ATR_TRAIL_TIMER_BARS:
                            # start gate by profit %
                            mark = pos["mark"]
                            entry = pos["entry"]
                            if entry > 0:
                                if pos["side"] == "Buy":
                                    move_pct = (mark - entry) / entry * Decimal("100")
                                else:
                                    move_pct = (entry - mark) / entry * Decimal("100")
                            else:
                                move_pct = Decimal("0")

                            if move_pct >= Decimal(str(ATR_TRAIL_START_PCT)):
                                kl = get_klines(symbol, ATR_TF, limit=max(ATR_LEN + 50, 80))
                                atr = calc_atr_from_klines(kl, ATR_LEN)
                                if atr > 0:
                                    if pos["side"] == "Buy":
                                        new_sl = mark - atr * Decimal(str(ATR_MULT))
                                    else:
                                        new_sl = mark + atr * Decimal(str(ATR_MULT))
                                    new_sl = round_down_to_step(new_sl, tick_size)

                                    # only tighten (never loosen)
                                    last_sl = Decimal(str(st.get("last_sl") or "0"))
                                    if last_sl <= 0:
                                        # if we don't know last SL, just set
                                        set_position_sl(symbol, new_sl)
                                        st["last_sl"] = str(new_sl)
                                        _trade_state[symbol] = st
                                    else:
                                        if pos["side"] == "Buy" and new_sl > last_sl:
                                            set_position_sl(symbol, new_sl)
                                            st["last_sl"] = str(new_sl)
                                            _trade_state[symbol] = st
                                        if pos["side"] == "Sell" and new_sl < last_sl:
                                            set_position_sl(symbol, new_sl)
                                            st["last_sl"] = str(new_sl)
                                            _trade_state[symbol] = st

            except Exception as e:
                logging.error("TRADE MANAGER ERROR: %s", str(e))
                logging.error(traceback.format_exc())

            time.sleep(5)  # light polling

    t = threading.Thread(target=loop, daemon=True)
    t.start()


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
            # ✅ ADDED: show extra config
            "fee_pct": BYBIT_FEE_PCT,
            "tp1_enabled": TP1_ENABLED,
            "tp1_pct": TP1_PCT,
            "tp1_qty_pct": TP1_QTY_PCT,
            "be_enabled": BE_ENABLED,
            "atr_trail_enabled": ATR_TRAIL_ENABLED,
            "reverse_enabled": REVERSE_ENABLED,
            "filters_enabled": BAD_ENTRY_FILTER_ENABLED,
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

        # ✅ NEW: импульсный режим (TradingView может прислать impulse=true)
        impulse = bool(data.get("impulse", False))

        # TP/SL:
        # - если impulse=true: tp_pct берем либо из tp_pct_impulse, либо из DEFAULT_TP_IMPULSE_PCT
        # - иначе как раньше: tp_pct из tp_pct или DEFAULT_TP_PCT
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

        # =======================
        # ✅ ADDED: cost filter (avoid commission-only trades)
        # =======================
        if not passes_cost_filter(tp_pct):
            return ok("Skip: cost filter (tp too small vs fees)", symbol=symbol, tp_pct=tp_pct, fee_total_pct=BYBIT_FEE_PCT * 2, min_edge_pct=MIN_EDGE_PCT)

        # =======================
        # ✅ ADDED: spread/impulse filter
        # =======================
        ok_entry, info = passes_bad_entry_filters(symbol)
        if not ok_entry:
            return ok("Skip: bad entry filter (spread/impulse)", symbol=symbol, details=info)

        # =======================
        # ✅ ADDED: reverse logic
        # =======================
        pos = get_position_info(symbol)
        if pos and REVERSE_ENABLED:
            # if opposite signal:
            current_side = pos["side"]  # "Buy" or "Sell" in our dict
            if (current_side == "Buy" and side == "Sell") or (current_side == "Sell" and side == "Buy"):
                # optional: don't reverse if deep in loss
                if REVERSE_ONLY_IF_NOT_IN_LOSS:
                    if pos["pnl_pct"] < -Decimal(str(REVERSE_MAX_LOSS_PCT)):
                        return ok("Skip reverse: position in loss beyond limit", symbol=symbol, pnl_pct=float(pos["pnl_pct"]), limit=-REVERSE_MAX_LOSS_PCT)

                # close full and open opposite
                close_position_market(symbol, current_side, pos["size"])
                time.sleep(0.5)  # small delay
                # after close, continue to place new
            else:
                return ok("Position already open same direction -> skip", symbol=symbol, open_size=float(pos["size"]), impulse=impulse)

        else:
            open_size = get_open_position_size(symbol)
            if open_size > 0:
                return ok("Position already open -> skip", symbol=symbol, open_size=open_size, impulse=impulse)

        # =======================
        # Place market + position TP/SL (as before)
        # =======================
        res = place_market_order_with_tpsl(symbol, side, usd, leverage, tp_pct, sl_pct)
        res["impulse"] = impulse

        # =======================
        # ✅ ADDED: place TP1 reduceOnly on exchange
        # =======================
        qty_step, tick_size = get_instrument_filters(symbol)
        entry_price = Decimal(str(res["entry_price_used"]))
        total_qty = Decimal(str(res["qty"]))

        tp1_price = None
        tp1_qty = None
        tp1_raw = None

        if TP1_ENABLED:
            tp1_price = calc_tp1_price(entry_price, side, TP1_PCT, tick_size)
            tp1_qty_raw = total_qty * Decimal(str(TP1_QTY_PCT))
            tp1_qty = round_down_to_step(tp1_qty_raw, qty_step)

            # safety: must be >= step
            if tp1_qty > 0:
                tp1_raw = place_reduce_only_tp1(symbol, side, tp1_qty, tp1_price)

        # =======================
        # ✅ ADDED: register state for BE/ATR trailing manager
        # =======================
        _trade_state[symbol] = {
            "active": True,
            "entry_ts": _now(),
            "entry_price": str(entry_price),
            "side": side,
            "initial_qty": str(total_qty),
            "tp1_placed": bool(tp1_raw is not None),
            "tp1_done": False,
            "be_done": False,
            "last_sl": str(res["sl_price"]),  # start with initial SL
        }
        ensure_trade_manager_running()

        # enrich response
        if tp1_price is not None:
            res["tp1_price"] = str(tp1_price)
        if tp1_qty is not None:
            res["tp1_qty"] = str(tp1_qty)

        return ok("Order placed with TP/SL + TP1 (reduceOnly) + BE/ATRTrail manager", **res)

    except Exception as e:
        logging.error("WEBHOOK ERROR: %s", str(e))
        logging.error(traceback.format_exc())
        return jsonify({"ok": False, "error": "internal_error", "detail": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
