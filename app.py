from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import numpy as np

app = Flask(__name__)
CORS(app)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json'
}

def get_yahoo_data(symbol):
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + symbol + '?interval=1wk&range=4y'
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        data = res.json()
        result = data["chart"]["result"][0]
        closes = [x for x in result["indicators"]["quote"][0]["close"] if x]
        volumes = [x for x in result["indicators"]["quote"][0]["volume"] if x]
        return closes, volumes
    except:
        return None, None

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes[-(period+1):])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains)
    avg_loss = np.mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = float(np.mean(closes[:period]))
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 4)

def calc_vol_spike(volumes):
    if len(volumes) < 20:
        return None
    avg = float(np.mean(volumes[-20:-1]))
    if avg == 0:
        return None
    return round(float(volumes[-1]) / avg, 2)

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/stocks")
def get_stocks_batch():
    symbols = request.args.get("symbols", "")
    if not symbols:
        return jsonify({"error": "symbols required"}), 400

    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()][:20]
    results = {}

    for symbol in symbol_list:
        closes, volumes = get_yahoo_data(symbol)
        if not closes or len(closes) < 20:
            continue
        try:
            price = closes[-1]
            rsi = calc_rsi(closes)
            ema200 = calc_ema(closes, min(200, len(closes)))
            ema50 = calc_ema(closes, min(50, len(closes)))
            vol_spike = calc_vol_spike(volumes) if volumes else None
            vs_ema200 = round((price / ema200 - 1) * 100, 2) if ema200 else None
            vs_ema50 = round((price / ema50 - 1) * 100, 2) if ema50 else None
            year = closes[-52:] if len(closes) >= 52 else closes
            high52 = round(max(year), 4)
            low52 = round(min(year), 4)
            results[symbol] = {
                "price": round(price, 4),
                "rsi": rsi,
                "ema200": ema200,
                "ema50": ema50,
                "vsEma200": vs_ema200,
                "vsEma50": vs_ema50,
                "volSpike": vol_spike,
                "high52": high52,
                "low52": low52,
                "vsHigh52": round((price / high52 - 1) * 100, 2),
                "vsLow52": round((price / low52 - 1) * 100, 2)
            }
        except:
            continue

    return jsonify(results)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
