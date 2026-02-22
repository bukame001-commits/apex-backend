from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import numpy as np

app = Flask(__name__)
CORS(app)  # Allow all origins

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = np.mean(closes[:period])
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(float(ema), 4)

def calc_vol_spike(volumes):
    if len(volumes) < 20:
        return None
    avg = np.mean(volumes[-20:-1])
    if avg == 0:
        return None
    return round(float(volumes[-1] / avg), 2)

def calc_macd(closes):
    if len(closes) < 26:
        return None, None
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    if ema12 is None or ema26 is None:
        return None, None
    macd_line = ema12 - ema26
    return round(macd_line, 4), round(ema12, 4)

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

@app.route('/stock')
def get_stock():
    symbol = request.args.get('symbol', '').upper()
    if not symbol:
        return jsonify({'error': 'symbol required'}), 400

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period='4y', interval='1wk')

        if hist.empty or len(hist) < 20:
            return jsonify({'error': 'no data'}), 404

        closes = hist['Close'].values.tolist()
        volumes = hist['Volume'].values.tolist()
        price = closes[-1]

        rsi = calc_rsi(closes)
        ema200 = calc_ema(closes, 200)
        ema50 = calc_ema(closes, 50)
        vol_spike = calc_vol_spike(volumes)
        macd, signal = calc_macd(closes)

        # Distance from EMAs
        vs_ema200 = round((price / ema200 - 1) * 100, 2) if ema200 else None
        vs_ema50 = round((price / ema50 - 1) * 100, 2) if ema50 else None

        # 52 week high/low
        year_closes = closes[-52:] if len(closes) >= 52 else closes
        high52 = round(max(year_closes), 4)
        low52 = round(min(year_closes), 4)
        vs_high52 = round((price / high52 - 1) * 100, 2)
        vs_low52 = round((price / low52 - 1) * 100, 2)

        return jsonify({
            'symbol': symbol,
            'price': round(price, 4),
            'rsi': rsi,
            'ema200': ema200,
            'ema50': ema50,
            'vsEma200': vs_ema200,
            'vsEma50': vs_ema50,
            'volSpike': vol_spike,
            'macd': macd,
            'high52': high52,
            'low52': low52,
            'vsHigh52': vs_high52,
            'vsLow52': vs_low52
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/stocks')
def get_stocks_batch():
    symbols = request.args.get('symbols', '')
    if not symbols:
        return jsonify({'error': 'symbols required'}), 400

    symbol_list = [s.strip().upper() for s in symbols.split(',') if s.strip()]
    results = {}

    for symbol in symbol_list[:20]:  # Max 20 per batch
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period='4y', interval='1wk')
            if hist.empty or len(hist) < 20:
                continue
            closes = hist['Close'].values.tolist()
            volumes = hist['Volume'].values.tolist()
            price = closes[-1]
            rsi = calc_rsi(closes)
            ema200 = calc_ema(closes, 200)
            ema50 = calc_ema(closes, 50)
            vol_spike = calc_vol_spike(volumes)
            vs_ema200 = round((price / ema200 - 1) * 100, 2) if ema200 else None
            vs_ema50 = round((price / ema50 - 1) * 100, 2) if ema50 else None
            year_closes = closes[-52:] if len(closes) >= 52 else closes
            high52 = round(max(year_closes), 4)
            low52 = round(min(year_closes), 4)
            results[symbol] = {
                'price': round(price, 4),
                'rsi': rsi,
                'ema200': ema200,
                'ema50': ema50,
                'vsEma200': vs_ema200,
                'vsEma50': vs_ema50,
                'volSpike': vol_spike,
                'high52': high52,
                'low52': low52,
                'vsHigh52': round((price / high52 - 1) * 100, 2),
                'vsLow52': round((price / low52 - 1) * 100, 2)
            }
        except:
            continue

    return jsonify(results)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
