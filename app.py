import os
import csv
import io
import time
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── Telegram config (set via environment variables on Render) ─
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID', '')

# ── Volume alert config ───────────────────────────────────────
VOLUME_SPIKE_MULTIPLIER = 2.5   # alert if current vol > 2.5x 20-bar average
SCAN_INTERVAL_SECONDS   = 3600  # scan every hour
ALERT_COOLDOWN_SECONDS  = 14400 # don't re-alert same coin within 4 hours
_last_alert_time = {}           # symbol -> last alert timestamp
_monitor_running  = False

# ── Cache for Russell 2000 constituents (refresh every 24h) ──
_russell_cache = None
_russell_cache_time = 0
CACHE_TTL = 86400  # 24 hours

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json'
}


# ── Send Telegram message ────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print('[ALERT] Telegram not configured — skipping notification')
        return False
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
        payload = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'HTML'
        }
        r = requests.post(url, json=payload, timeout=10)
        if r.ok:
            print(f'[ALERT] Telegram sent OK')
            return True
        else:
            print(f'[ALERT] Telegram error: {r.status_code} {r.text}')
            return False
    except Exception as e:
        print(f'[ALERT] Telegram exception: {e}')
        return False


# ── Volume spike detection ────────────────────────────────────
def detect_volume_spike(symbol, klines):
    """Returns spike ratio if volume spike detected, else None."""
    if not klines or len(klines) < 22:
        return None
    try:
        vols = [float(k[5]) for k in klines]
        avg_vol = sum(vols[-21:-1]) / 20   # 20-bar average excluding current
        curr_vol = vols[-1]
        if avg_vol > 0:
            ratio = curr_vol / avg_vol
            if ratio >= VOLUME_SPIKE_MULTIPLIER:
                return round(ratio, 2)
    except Exception:
        pass
    return None


# ── Background monitor scan ───────────────────────────────────
MONITOR_COINS = [
    'BTC','ETH','BNB','XRP','SOL','ADA','DOGE','TRX','DOT','LTC',
    'SHIB','AVAX','LINK','ATOM','UNI','XLM','BCH','ALGO','ICP','HBAR',
    'APT','ARB','NEAR','GRT','AAVE','INJ','OP','SUI','SEI','TIA',
    'RUNE','PENDLE','JUP','WIF','BONK','FLOKI','PEPE','TON','KAS','TAO',
    'IMX','RNDR','FIL','THETA','EGLD','MINA','SAND','MANA','AXS','CHZ',
    'ENJ','GALA','GMT','GMX','DYDX','LDO','CRV','COMP','SNX','STX',
    'BLUR','KAVA','ROSE','ANKR','ZIL','ETC','VET','1INCH','CAKE','FTM',
    'MAGIC','PYTH','JTO','STRK','METIS','API3','MASK','ALICE','GLMR','KSM',
    'ZRX','BAT','OMG','BNT','STORJ','KNC','WAVES','QTUM','ICX','LSK',
    'JASMY','HIGH','LQTY','RPL','SSV','RDNT','GNS','HOOK','ACE','XAI',
    'MANTA','ALT','PIXEL','PORTAL','ZETA','DYM','TNSR','REZ','BB','NOT',
    'IO','ZK','WLD','ETHFI','EIGEN','HMSTR','CATI','NEIRO','SCR','DOGS',
    'PNUT','ACT','GOAT','MOODENG','TURBO','BRETT','ENA','SAGA','BOME','MEW',
]


def run_monitor_scan():
    """Scan all monitored coins and send Telegram alerts for volume spikes."""
    print(f'[MONITOR] Starting volume scan for {len(MONITOR_COINS)} coins...')
    alerts = []
    now = time.time()

    def check_coin(sym):
        # Skip if alerted recently
        if now - _last_alert_time.get(sym, 0) < ALERT_COOLDOWN_SECONDS:
            return None
        try:
            # Try KuCoin first (reliable from cloud IPs)
            kucoin_url = f'https://api.kucoin.com/api/v1/market/candles?symbol={sym}-USDT&type=1day&startAt={int(now)-86400*30}&endAt={int(now)}'
            r = requests.get(kucoin_url, headers=HEADERS, timeout=8)
            if r.ok:
                data = r.json().get('data', [])
                if len(data) >= 22:
                    # KuCoin newest first: [time, open, close, high, low, vol, turnover]
                    klines = [[int(c[0])*1000, float(c[1]), float(c[3]), float(c[4]), float(c[2]), float(c[5])] for c in reversed(data)]
                    spike = detect_volume_spike(sym, klines)
                    if spike:
                        price = klines[-1][4]
                        return {'sym': sym, 'spike': spike, 'price': price, 'source': 'KuCoin'}

            # Try OKX fallback
            okx_url = f'https://www.okx.com/api/v5/market/history-candles?instId={sym}-USDT&bar=1D&limit=30'
            r2 = requests.get(okx_url, headers=HEADERS, timeout=8)
            if r2.ok:
                data2 = r2.json().get('data', [])
                if len(data2) >= 22:
                    klines2 = [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] for c in reversed(data2)]
                    spike = detect_volume_spike(sym, klines2)
                    if spike:
                        price = klines2[-1][4]
                        return {'sym': sym, 'spike': spike, 'price': price, 'source': 'OKX'}
        except Exception as e:
            print(f'[MONITOR] {sym} error: {e}')
        return None

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(check_coin, MONITOR_COINS))

    for result in results:
        if result:
            alerts.append(result)
            _last_alert_time[result['sym']] = now

    if alerts:
        # Sort by spike ratio descending
        alerts.sort(key=lambda x: x['spike'], reverse=True)
        lines = [f'🚨 <b>APEX SCANNER — VOLUME SPIKE ALERT</b>']
        lines.append(f'⏰ {time.strftime("%Y-%m-%d %H:%M UTC")}')
        lines.append('')
        for a in alerts[:10]:  # max 10 per message
            lines.append(f'<b>{a["sym"]}</b>  {a["spike"]}x avg volume')
            lines.append(f'   Price: ${a["price"]:,.4f}  |  Source: {a["source"]}')
            lines.append('')
        send_telegram('\n'.join(lines))
        print(f'[MONITOR] Sent alert for {len(alerts)} coins: {[a["sym"] for a in alerts]}')
    else:
        print(f'[MONITOR] Scan complete — no volume spikes detected')


def monitor_loop():
    """Background thread — runs forever, scanning every SCAN_INTERVAL_SECONDS."""
    global _monitor_running
    _monitor_running = True
    print(f'[MONITOR] Background monitor started — scanning every {SCAN_INTERVAL_SECONDS//60} minutes')
    # Wait 60s after startup before first scan (let server fully initialize)
    time.sleep(60)
    while True:
        try:
            run_monitor_scan()
        except Exception as e:
            print(f'[MONITOR] Scan error: {e}')
        time.sleep(SCAN_INTERVAL_SECONDS)


# Start background monitor thread on server startup
_monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
_monitor_thread.start()


# ── Health check ─────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'monitor': _monitor_running})


# ── Fetch single stock from Yahoo Finance ─────────────────────
def parse_klines_from_rows(rows, interval):
    """Convert OHLCV rows (list of lists) into kline format [[ts_ms, o, h, l, c, v], ...]"""
    import datetime
    klines = []
    for row in rows:
        try:
            if len(row) < 5:
                continue
            # Parse date string to timestamp
            date_str = str(row[0]).strip()
            try:
                dt = datetime.datetime.strptime(date_str, '%Y-%m-%d')
            except ValueError:
                try:
                    dt = datetime.datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    continue
            ts_ms = int(dt.timestamp() * 1000)
            o = float(row[1]) if row[1] and str(row[1]).strip() not in ('', 'null') else None
            h = float(row[2]) if row[2] and str(row[2]).strip() not in ('', 'null') else None
            l = float(row[3]) if row[3] and str(row[3]).strip() not in ('', 'null') else None
            c = float(row[4]) if row[4] and str(row[4]).strip() not in ('', 'null') else None
            v = float(row[5]) if len(row) > 5 and row[5] and str(row[5]).strip() not in ('', 'null') else 0
            if o and h and l and c:
                klines.append([ts_ms, o, h, l, c, v])
        except Exception:
            continue
    return klines


def fetch_stooq(sym, interval='1wk'):
    """Fetch OHLCV from Stooq as fallback. Returns klines list or None."""
    try:
        # Stooq interval codes: d=daily, w=weekly, m=monthly
        interval_map = {'1wk': 'w', '1d': 'd', '60m': 'd'}  # No hourly on Stooq free
        stooq_interval = interval_map.get(interval, 'd')
        # Stooq ticker format: lowercase + .us suffix
        stooq_sym = sym.lower().replace('-', '.') + '.us'
        url = f'https://stooq.com/q/d/l/?s={stooq_sym}&i={stooq_interval}'
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,*/*',
            'Referer': 'https://stooq.com/'
        }
        r = requests.get(url, headers=headers, timeout=10)
        if not r.ok or 'No data' in r.text or len(r.text) < 50:
            return None
        # Parse CSV — header: Date,Open,High,Low,Close,Volume
        lines = r.text.strip().splitlines()
        if len(lines) < 3:
            return None
        rows = []
        for line in lines[1:]:  # skip header
            parts = line.strip().split(',')
            if len(parts) >= 5:
                rows.append(parts)
        # Stooq returns oldest first — reverse to get newest last (match Yahoo format)
        rows.reverse()
        klines = parse_klines_from_rows(rows, interval)
        return klines if len(klines) >= 20 else None
    except Exception as e:
        print(f'Stooq {sym} error: {e}')
        return None


def fetch_one_stock(sym, interval='1wk'):
    try:
        # Normalise interval names (frontend may send 1w, Yahoo needs 1wk)
        interval_map = {'1w': '1wk', '1week': '1wk', '4h': '60m', '4hour': '60m'}
        interval = interval_map.get(interval, interval)
        range_map = {'1wk': '4y', '1d': '2y', '60m': '730d'}
        range_val = range_map.get(interval, '4y')

        # ── 1. Try Yahoo Finance first ──
        yahoo_klines = None
        market_cap = None
        try:
            url = f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval={interval}&range={range_val}'
            r = requests.get(url, headers=HEADERS, timeout=8)
            if r.ok:
                data = r.json()
                result = data.get('chart', {}).get('result', [None])[0]
                if result:
                    timestamps = result.get('timestamp', [])
                    q = result.get('indicators', {}).get('quote', [{}])[0]
                    market_cap = result.get('meta', {}).get('marketCap')
                    if q and len(timestamps) >= 20:
                        klines = []
                        opens   = q.get('open',   [None]*len(timestamps))
                        highs   = q.get('high',   [None]*len(timestamps))
                        lows    = q.get('low',    [None]*len(timestamps))
                        closes  = q.get('close',  [None]*len(timestamps))
                        volumes = q.get('volume', [0]*len(timestamps))
                        for i, ts in enumerate(timestamps):
                            if opens[i] is not None and closes[i] is not None:
                                klines.append([ts * 1000, opens[i], highs[i], lows[i], closes[i], volumes[i] or 0])
                        if len(klines) >= 20:
                            yahoo_klines = klines
        except Exception as e:
            print(f'{sym} Yahoo error: {e}')

        if yahoo_klines:
            return sym, {'klines': yahoo_klines, 'marketCap': market_cap, 'type': 'stock'}

        # ── 2. Stooq fallback — only fires when Yahoo fails ──
        # Skip Stooq for 60m (intraday) — it doesn't have reliable hourly data
        if interval != '60m':
            print(f'{sym}: Yahoo failed, trying Stooq...')
            stooq_klines = fetch_stooq(sym, interval)
            if stooq_klines:
                print(f'{sym}: Stooq success ({len(stooq_klines)} bars)')
                return sym, {'klines': stooq_klines, 'marketCap': None, 'type': 'stock'}

        return sym, None
    except Exception as e:
        print(f'{sym} error: {e}')
        return sym, None


# ── Crypto pairs — fetch real list from multiple exchanges ───
@app.route('/binance-pairs')
def binance_pairs():
    EXCLUDE = {'USDT','USDC','BUSD','TUSD','USDD','USDP','FDUSD','DAI','FRAX',
               'LUSD','PYUSD','GUSD','SUSD','USDB','USDX','EURC','WBTC','WETH',
               'WBNB','STETH','WSTETH','CBETH','RETH','BETH','BTCB','HBTC'}
    seen = set()
    symbols = []

    # 1. Try Binance
    try:
        r = requests.get('https://api.binance.com/api/v3/exchangeInfo', headers=HEADERS, timeout=10)
        if r.ok:
            data = r.json()
            for s in data.get('symbols', []):
                if (s.get('quoteAsset') == 'USDT' and
                    s.get('status') == 'TRADING' and
                    s.get('isSpotTradingAllowed', False)):
                    base = s['baseAsset']
                    if base not in seen and base not in EXCLUDE:
                        seen.add(base)
                        symbols.append(base)
            if symbols:
                print(f'Binance pairs: {len(symbols)}')
                return jsonify({'symbols': symbols, 'count': len(symbols), 'source': 'binance'})
    except Exception as e:
        print(f'Binance exchangeInfo error: {e}')

    # 2. Try KuCoin
    try:
        r2 = requests.get('https://api.kucoin.com/api/v2/symbols', headers=HEADERS, timeout=10)
        if r2.ok:
            data2 = r2.json()
            for s in data2.get('data', []):
                if s.get('quoteCurrency') == 'USDT' and s.get('enableTrading', False):
                    base = s['baseCurrency']
                    if base not in seen and base not in EXCLUDE:
                        seen.add(base)
                        symbols.append(base)
            if symbols:
                print(f'KuCoin pairs: {len(symbols)}')
                return jsonify({'symbols': symbols, 'count': len(symbols), 'source': 'kucoin'})
    except Exception as e:
        print(f'KuCoin symbols error: {e}')

    # 3. Try OKX
    try:
        r3 = requests.get('https://www.okx.com/api/v5/public/instruments?instType=SPOT', headers=HEADERS, timeout=10)
        if r3.ok:
            data3 = r3.json()
            for s in data3.get('data', []):
                if s.get('quoteCcy') == 'USDT' and s.get('state') == 'live':
                    base = s['baseCcy']
                    if base not in seen and base not in EXCLUDE:
                        seen.add(base)
                        symbols.append(base)
            if symbols:
                print(f'OKX pairs: {len(symbols)}')
                return jsonify({'symbols': symbols, 'count': len(symbols), 'source': 'okx'})
    except Exception as e:
        print(f'OKX instruments error: {e}')

    return jsonify({'symbols': [], 'count': 0, 'source': 'none'})


# ── Crypto OHLCV data — fetch from Binance server-side ──────
@app.route('/crypto')
def crypto():
    symbols_param = request.args.get('symbols', '')
    symbols = [s.strip().upper() for s in symbols_param.split(',') if s.strip()][:200]
    if not symbols:
        return jsonify({})

    interval = request.args.get('interval', '1d')
    limit = int(request.args.get('limit', '210'))

    # Normalize interval for Binance
    interval_map = {'1w': '1W', '1week': '1W', '4h': '4h', '1d': '1d'}
    binance_interval = interval_map.get(interval, interval)

    def fetch_one_crypto(sym):
        try:
            # 1. Try Binance Futures
            try:
                url = f'https://fapi.binance.com/fapi/v1/klines?symbol={sym}USDT&interval={binance_interval}&limit={limit}'
                r = requests.get(url, headers=HEADERS, timeout=6)
                if r.ok:
                    data = r.json()
                    if isinstance(data, list) and len(data) >= 20:
                        klines = [[c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] for c in data]
                        return sym, {'klines': klines, 'type': 'crypto'}
            except Exception: pass

            # 2. Try Binance Spot
            try:
                url2 = f'https://api.binance.com/api/v3/klines?symbol={sym}USDT&interval={binance_interval}&limit={limit}'
                r2 = requests.get(url2, headers=HEADERS, timeout=6)
                if r2.ok:
                    data2 = r2.json()
                    if isinstance(data2, list) and len(data2) >= 20:
                        klines = [[c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] for c in data2]
                        return sym, {'klines': klines, 'type': 'crypto'}
            except Exception: pass

            # 3. KuCoin — not geo-blocked on cloud IPs, 700+ USDT pairs
            try:
                kucoin_interval = {'1W': '1week', '1d': '1day', '4h': '4hour'}.get(binance_interval, '1day')
                import time
                end_ts = int(time.time())
                start_ts = end_ts - (limit * {'1week': 604800, '1day': 86400, '4hour': 14400}.get(kucoin_interval, 86400))
                url3 = f'https://api.kucoin.com/api/v1/market/candles?symbol={sym}-USDT&type={kucoin_interval}&startAt={start_ts}&endAt={end_ts}'
                r3 = requests.get(url3, headers=HEADERS, timeout=8)
                if r3.ok:
                    data3 = r3.json()
                    candles = data3.get('data', [])
                    if candles and len(candles) >= 20:
                        # KuCoin returns newest first: [time, open, close, high, low, volume, turnover]
                        klines = []
                        for c in reversed(candles):
                            try:
                                klines.append([int(c[0])*1000, float(c[1]), float(c[3]), float(c[4]), float(c[2]), float(c[5])])
                            except Exception: continue
                        if len(klines) >= 20:
                            return sym, {'klines': klines, 'type': 'crypto'}
            except Exception as e:
                print(f'KuCoin {sym} error: {e}')

            # 4. OKX — another exchange that works from cloud IPs
            try:
                okx_bar = {'1W': '1W', '1d': '1D', '4h': '4H'}.get(binance_interval, '1D')
                url4 = f'https://www.okx.com/api/v5/market/history-candles?instId={sym}-USDT&bar={okx_bar}&limit={min(limit, 300)}'
                r4 = requests.get(url4, headers=HEADERS, timeout=8)
                if r4.ok:
                    data4 = r4.json()
                    candles4 = data4.get('data', [])
                    if candles4 and len(candles4) >= 20:
                        # OKX: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
                        klines = []
                        for c in reversed(candles4):
                            try:
                                klines.append([int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
                            except Exception: continue
                        if len(klines) >= 20:
                            return sym, {'klines': klines, 'type': 'crypto'}
            except Exception as e:
                print(f'OKX {sym} error: {e}')

            return sym, None
        except Exception as e:
            print(f'Crypto {sym} error: {e}')
            return sym, None

    results = {}
    with ThreadPoolExecutor(max_workers=40) as executor:
        futures = {executor.submit(fetch_one_crypto, sym): sym for sym in symbols}
        for future in as_completed(futures):
            sym, data = future.result()
            if data:
                results[sym] = data

    return jsonify(results)


# ── Stock OHLCV data — parallel fetch via thread pool ────────
@app.route('/stocks')
def stocks():
    symbols_param = request.args.get('symbols', '')
    symbols = [s.strip().upper() for s in symbols_param.split(',') if s.strip()][:200]
    if not symbols:
        return jsonify({})

    interval = request.args.get('interval', '1wk')
    results = {}
    with ThreadPoolExecutor(max_workers=40) as executor:
        futures = {executor.submit(fetch_one_stock, sym, interval): sym for sym in symbols}
        for future in as_completed(futures):
            sym, data = future.result()
            if data:
                results[sym] = data

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
        seen = set()
        reader = csv.reader(io.StringIO(r.text))
        data_started = False
        consecutive_invalid = 0

        for row in reader:
            if not row:
                continue
            ticker = row[0].strip().strip('"').upper()

            # Skip header rows until we hit real ticker data
            if not data_started:
                if ticker and 1 <= len(ticker) <= 5 and ticker.replace('-','').replace('.','').isalpha():
                    data_started = True
                else:
                    continue

            # Stop if we hit the cash/futures/footer section
            # iShares CSVs end equity section with rows like "Cash", "-", empty tickers
            if not ticker or ticker in ('TICKER','NAME','CASH','USD','EUR','GBP','-','','TOTAL'):
                consecutive_invalid += 1
                if consecutive_invalid > 10:
                    break  # End of equity section
                continue
            consecutive_invalid = 0

            # Accept standard US equity tickers: 1-5 alpha chars, optional hyphen for class shares
            # Covers: AAPL, BRK-B, BF-B, etc.
            clean = ticker.replace('-','').replace('.','')
            if not (1 <= len(ticker) <= 6 and clean.isalpha() and clean.isupper()):
                continue

            # Skip known non-equity rows
            if ticker in ('CASH','USD','EUR','GBP','CHF','JPY','XTSLA','PUT','CALL'):
                continue

            if ticker not in seen:
                seen.add(ticker)
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


# ── Monitor control routes ───────────────────────────────────
@app.route('/monitor/config', methods=['POST'])
def monitor_config():
    """Accept token/chat_id from frontend and store for this session."""
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    data = request.get_json(silent=True) or {}
    token   = data.get('token', '').strip()
    chat_id = data.get('chat_id', '').strip()
    if token and chat_id:
        TELEGRAM_BOT_TOKEN = token
        TELEGRAM_CHAT_ID   = chat_id
        print(f'[MONITOR] Telegram configured via frontend — chat_id: {chat_id}')
        return jsonify({'ok': True, 'message': 'Telegram configured for this session'})
    return jsonify({'ok': False, 'message': 'Missing token or chat_id'}), 400


@app.route('/monitor/status')
def monitor_status():
    now = time.time()
    recent = [sym for sym, t in _last_alert_time.items() if now - t < ALERT_COOLDOWN_SECONDS]
    return jsonify({
        'running': _monitor_running,
        'scan_interval_minutes': SCAN_INTERVAL_SECONDS // 60,
        'alert_cooldown_hours': ALERT_COOLDOWN_SECONDS // 3600,
        'spike_threshold': VOLUME_SPIKE_MULTIPLIER,
        'coins_monitored': len(MONITOR_COINS),
        'recent_alerts': len(recent),
        'telegram_configured': bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
    })


@app.route('/monitor/test')
def monitor_test():
    """Send test message — accepts token/chat_id as query params for one-off test."""
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    # Allow passing token/chat_id directly in URL for test
    token   = request.args.get('token',   TELEGRAM_BOT_TOKEN).strip()
    chat_id = request.args.get('chat_id', TELEGRAM_CHAT_ID).strip()
    if token and chat_id:
        # Temporarily set for this request
        old_token, old_cid = TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID = token, chat_id
        ok = send_telegram(
            f'✅ <b>APEX SCANNER</b> — Test notification\n'
            f'Alerts are working correctly!\n'
            f'Monitoring <b>{len(MONITOR_COINS)}</b> coins for volume spikes (2.5× threshold).'
        )
        TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID = old_token, old_cid
        return jsonify({'sent': ok})
    return jsonify({'sent': False, 'error': 'No Telegram token configured'})


@app.route('/monitor/scan-now')
def monitor_scan_now():
    """Trigger an immediate scan. Accepts token/chat_id to set config first."""
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    token   = request.args.get('token',   '').strip()
    chat_id = request.args.get('chat_id', '').strip()
    if token and chat_id:
        TELEGRAM_BOT_TOKEN = token
        TELEGRAM_CHAT_ID   = chat_id
    threading.Thread(target=run_monitor_scan, daemon=True).start()
    return jsonify({'status': 'scan started', 'coins': len(MONITOR_COINS)})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)
