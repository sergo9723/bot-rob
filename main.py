import os
import time
from decimal import Decimal, ROUND_DOWN

from flask import Flask, request, jsonify
from pybit.unified_trading import HTTP

app = Flask(__name__)

# -------------------------
# ENV (Render -> Environment)
# -------------------------
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "true").lower() in ("1", "true", "yes", "y")

# Секрет для TradingView webhook (должен совпадать с тем, что ты вставляешь в Alert message)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "xrp12345")

# По умолчанию
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "5"))     # 5x
DEFAULT_MARGIN_USD = float(os.getenv("DEFAULT_MARGIN_USD", "3.5"))  # 3.5 USDT маржи

# Защита от спама/дублей (сек)
DEDUP_WINDOW_SEC = int(os.getenv("DEDUP_WINDOW_SEC", "5"))

if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    # Render покажет это в логах, чтобы ты понял, что забыл переменные окружения
    print("WARNING: BYBIT_API_KEY/BYBIT_API_SECRET not set in environment variables!")

session = HTTP(
    testnet=BYBIT_TESTNET,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
)

# Кэш информации по инструментам (шаг количества)
_instrument_cache = {}  # symbol -> (qty_step_decimal, ts)
INSTR_CACHE_TTL = 60 * 10  # 10 минут

# Дедупликация
_last_signal = {}  # symbol -> (side, ts)


def _now() -> int:
    return int(time.time())


def _json_error(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


def _get_qty_step(symbol: str) -> Decimal:
    """Получаем шаг количества (qtyStep) для округления qty."""
    cached = _instrument_cache.get(symbol)
    if cached:
        step, ts = cached
        if _now() - ts < INSTR_CACHE_TTL:
            return step

    resp = session.get_instruments_info(
        category="linear",
        symbol=symbol
    )

    if resp.get("retCode") != 0:
        raise RuntimeError(f"get_instruments_info failed: {resp}")

    lst = (resp.get("result") or {}).get("list") or []
    if not lst:
        raise RuntimeError(f"instrument not found for {symbol}")

    lot = (lst[0].get("lotSizeFilter") or {})
    qty_step = lot.get("qtyStep")
    if not qty_step:
        # на всякий случай
        qty_step = "0.1"

    step_dec = Decimal(str(qty_step))
    _instrument_cache[symbol] = (step_dec, _now())
    return step_dec


def _round_qty(qty: Decimal, step: Decimal) -> Decimal:
    """Округляем вниз к шагу."""
    if step <= 0:
        return qty
    return (qty / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step


def _get_last_price(symbol: str) -> Decimal:
    resp = session.get_tickers(category="linear", symbol=symbol)
    if resp.get("retCode") != 0:
        raise RuntimeError(f"get_tickers failed: {resp}")
    lst = (resp.get("result") or {}).get("list") or []
    if not lst:
        raise RuntimeError(f"ticker not found for {symbol}")
    last_price = lst[0].get("lastPrice")
    if not last_price:
        raise RuntimeError(f"no lastPrice for {symbol}")
    return Decimal(str(last_price))


def _set_leverage(symbol: str, leverage: int):
    # Bybit требует строку
    lev = str(int(leverage))
    resp = session.set_leverage(
        category="linear",
        symbol=symbol,
        buyLeverage=lev,
        sellLeverage=lev
    )
    # set_leverage может вернуть retCode=0 даже если уже стоит то же значение
    if resp.get("retCode") != 0:
        raise RuntimeError(f"set_leverage failed: {resp}")


def _place_market_order(symbol: str, side: str, margin_usd: float, leverage: int):
    """
    margin_usd = сколько USDT маржи на сделку
    notional = margin_usd * leverage
    qty = notional / price
    """
    price = _get_last_price(symbol)
    notional = Decimal(str(margin_usd)) * Decimal(str(leverage))
    raw_qty = notional / price

    step = _get_qty_step(symbol)
    qty = _round_qty(raw_qty, step)

    if qty <= 0:
        raise RuntimeError(f"qty computed <= 0 (raw_qty={raw_qty}, step={step})")

    # MARKET ордер
    resp = session.place_order(
        category="linear",
        symbol=symbol,
        side=side,               # "Buy" / "Sell"
        orderType="Market",
        qty=str(qty),
        timeInForce="IOC",
        reduceOnly=False
    )
    if resp.get("retCode") != 0:
        raise RuntimeError(f"place_order failed: {resp}")

    return {
        "symbol": symbol,
        "side": side,
        "price": str(price),
        "margin_usd": margin_usd,
        "leverage": leverage,
        "qty": str(qty),
        "bybit": resp
    }


@app.get("/health")
def health():
    return jsonify({"ok": True, "time": _now(), "testnet": BYBIT_TESTNET})


@app.post("/webhook")
def webhook():
    # TradingView шлёт JSON
    data = request.get_json(silent=True)
    if not data:
        return _json_error("No JSON body")

    # 1) секрет
    secret = str(data.get("secret", ""))
    if secret != WEBHOOK_SECRET:
        return _json_error("Bad secret", 403)

    # 2) symbol
    symbol = str(data.get("symbol", "")).upper().strip()
    if not symbol:
        return _json_error("Missing symbol")

    # 3) side из TradingView
    # В твоём алерте side = "{{strategy.order.action}}"
    # TradingView обычно шлёт BUY / SELL
    raw_side = str(data.get("side", "")).strip().lower()
    if raw_side in ("buy", "long"):
        side = "Buy"
    elif raw_side in ("sell", "short"):
        side = "Sell"
    else:
        return _json_error(f"Bad side: {data.get('side')} (expected BUY/SELL)")

    # 4) деньги и плечо
    margin_usd = float(data.get("usd", DEFAULT_MARGIN_USD))
    leverage = int(data.get("leverage", DEFAULT_LEVERAGE))

    if margin_usd <= 0:
        return _json_error("usd must be > 0")
    if leverage < 1 or leverage > 100:
        return _json_error("leverage out of range")

    # 5) дедуп (если одинаковый сигнал подряд за N секунд)
    last = _last_signal.get(symbol)
    now = _now()
    if last:
        last_side, last_ts = last
        if last_side == side and (now - last_ts) <= DEDUP_WINDOW_SEC:
            return jsonify({"ok": True, "skipped": True, "reason": "duplicate", "symbol": symbol, "side": side})

    try:
        # leverage на символ
        _set_leverage(symbol, leverage)

        result = _place_market_order(symbol, side, margin_usd, leverage)

        _last_signal[symbol] = (side, now)
        return jsonify({"ok": True, "executed": True, "result": result})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    # Локально: python main.py
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
