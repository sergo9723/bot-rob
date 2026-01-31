from flask import Flask, request, jsonify
import requests
import time
import hmac
import hashlib

app = Flask(__name__)

# ===== НАСТРОЙКИ =====
API_KEY = "duCsrGgCF3ilnAQJDV"
API_SECRET = "F8X5iARZIPTiTlaxX05oNxRqr38MBSD897c5"

BASE_URL = "https://api.bybit.com"

SYMBOL = "XRPUSDT"
LEVERAGE = 5
USDT_PER_TRADE = 3.5


# ===== ПОДПИСЬ =====
def sign(params):
    query = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    return hmac.new(
        API_SECRET.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()


# ===== УСТАНОВКА ПЛЕЧА =====
def set_leverage():
    endpoint = "/v5/position/set-leverage"
    params = {
        "category": "linear",
        "symbol": SYMBOL,
        "buyLeverage": LEVERAGE,
        "sellLeverage": LEVERAGE,
        "timestamp": int(time.time() * 1000)
    }
    params["sign"] = sign(params)
    requests.post(BASE_URL + endpoint, json=params, headers={"X-BAPI-API-KEY": API_KEY})


# ===== ОТКРЫТИЕ СДЕЛКИ =====
def open_trade(side):
    endpoint = "/v5/order/create"

    price = requests.get(
        BASE_URL + "/v5/market/tickers",
        params={"category": "linear", "symbol": SYMBOL}
    ).json()["result"]["list"][0]["lastPrice"]

    qty = round((USDT_PER_TRADE * LEVERAGE) / float(price), 1)

    params = {
        "category": "linear",
        "symbol": SYMBOL,
        "side": side,
        "orderType": "Market",
        "qty": qty,
        "timeInForce": "GoodTillCancel",
        "timestamp": int(time.time() * 1000)
    }

    params["sign"] = sign(params)

    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "Content-Type": "application/json"
    }

    return requests.post(BASE_URL + endpoint, json=params, headers=headers).json()


# ===== WEBHOOK =====
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    signal = data.get("signal")

    set_leverage()

    if signal == "LONG":
        open_trade("Buy")
        return jsonify({"status": "LONG opened"})

    if signal == "SHORT":
        open_trade("Sell")
        return jsonify({"status": "SHORT opened"})

    return jsonify({"error": "Unknown signal"})


@app.route("/")
def home():
    return "Bybit webhook bot is running"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
