import os
import csv
import io
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── Cache for Russell 2000 constituents (refresh every 24h) ──
_russell_cache = None
_russell_cache_time = 0
CACHE_TTL = 86400  # 24 hours


# ── Health check ─────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


# ── Stock OHLCV data via Yahoo Finance ───────────────────────
@app.route('/stocks')
def stocks():
    symbols_param = request.args.get('symbols', '')
    symbols = [s.strip().upper() for s in symbols_param.split(',') if s.strip()][:50]
    if not symbols:
        return jsonify({})

    results = {}
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json'
    }

    for sym in symbols:
        try:
            url = f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1wk&range=4y'
            r = requests.get(url, headers=headers, timeout=12)
            if not r.ok:
                continue
            data = r.json()
            result = data.get('chart', {}).get('result', [None])[0]
            if not result:
                continue

            timestamps = result.get('timestamp', [])
            q = result.get('indicators', {}).get('quote', [{}])[0]
            if not q or len(timestamps) < 35:
                continue

            klines = []
            for i, ts in enumerate(timestamps):
                o = q.get('open', [None] * len(timestamps))[i]
                h = q.get('high', [None] * len(timestamps))[i]
                lo = q.get('low', [None] * len(timestamps))[i]
                c = q.get('close', [None] * len(timestamps))[i]
                v = q.get('volume', [0] * len(timestamps))[i] or 0
                if o is not None and c is not None:
                    klines.append([ts * 1000, o, h, lo, c, v])

            if len(klines) < 35:
                continue

            results[sym] = {
                'klines': klines,
                'marketCap': result.get('meta', {}).get('marketCap'),
                'type': 'stock'
            }
        except Exception as e:
            print(f'{sym} error: {e}')
            continue

    return jsonify(results)


# ── Russell 2000 constituents via iShares IWM ETF ────────────
@app.route('/russell2000')
def russell2000():
    global _russell_cache, _russell_cache_time

    # Return cache if still fresh
    if _russell_cache and (time.time() - _russell_cache_time) < CACHE_TTL:
        return jsonify({
            'tickers': _russell_cache,
            'count': len(_russell_cache),
            'source': 'cache'
        })

    try:
        url = (
            'https://www.ishares.com/us/products/239707/ishares-russell-2000-etf/'
            '1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund'
        )
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.ishares.com/',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
        }
        r = requests.get(url, headers=headers, timeout=20)
        if not r.ok:
            raise Exception(f'iShares returned {r.status_code}')

        tickers = []
        reader = csv.reader(io.StringIO(r.text))
        data_started = False

        for row in reader:
            if not row:
                continue
            ticker = row[0].strip().strip('"')

            # Skip header rows — wait until we see a valid ticker pattern
            if not data_started:
                if ticker and ticker.isupper() and 1 <= len(ticker) <= 5 and ticker.isalpha():
                    data_started = True
                else:
                    continue

            # Skip non-ticker rows (cash, header remnants, etc.)
            if not ticker or ticker in ('Ticker', 'Name', 'CASH', 'USD', 'EUR', '-'):
                continue
            if not (ticker.isupper() and 1 <= len(ticker) <= 5 and ticker.replace('-','').isalpha()):
                continue

            tickers.append(ticker)

        if len(tickers) > 100:
            _russell_cache = tickers
            _russell_cache_time = time.time()
            print(f'Russell 2000: fetched {len(tickers)} tickers from iShares')
            return jsonify({
                'tickers': tickers,
                'count': len(tickers),
                'source': 'ishares'
            })

        raise Exception(f'Only parsed {len(tickers)} tickers — possible format change')

    except Exception as e:
        print(f'Russell 2000 fetch failed: {e}')
        return jsonify({'error': str(e)}), 503


# ── S&P 500 constituents via iShares IVV ETF ─────────────────
@app.route('/sp500')
def sp500():
    try:
        url = (
            'https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/'
            '1467271812596.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund'
        )
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.ishares.com/'
        }
        r = requests.get(url, headers=headers, timeout=20)
        if not r.ok:
            raise Exception(f'{r.status_code}')

        tickers = []
        reader = csv.reader(io.StringIO(r.text))
        started = False
        for row in reader:
            if not row:
                continue
            ticker = row[0].strip().strip('"')
            if not started:
                if ticker.isupper() and 1 <= len(ticker) <= 5 and ticker.isalpha():
                    started = True
                else:
                    continue
            if ticker and ticker.isupper() and ticker.isalpha() and ticker not in ('CASH', 'USD'):
                tickers.append(ticker)

        return jsonify({'tickers': tickers[:505], 'count': len(tickers)})
    except Exception as e:
        return jsonify({'error': str(e)}), 503


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)
