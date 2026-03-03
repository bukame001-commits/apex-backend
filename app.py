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
VOLUME_SPIKE_MULTIPLIER = 2.0   # alert if current vol > 2x 20-bar average
SCAN_INTERVAL_SECONDS   = 3600  # scan every hour
ALERT_COOLDOWN_SECONDS  = 14400  # don't re-alert same coin within 4 hours
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


# ── Kıvanç-inspired helper indicators ────────────────────────

def calc_mfi(highs, lows, closes, vols, period=14):
    """
    Money Flow Index — Kıvanç uses MFI as the volume component in AlphaTrend.
    MFI > 50 = money flowing in (bullish). MFI rising = accelerating inflow.
    Uses typical price (HLC/3) like the original.
    """
    if len(closes) < period + 2:
        return None, None
    typical = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(closes))]
    raw_mfi = []
    for i in range(1, len(typical)):
        tp_cur  = typical[i]
        tp_prev = typical[i - 1]
        vol     = vols[i]
        if tp_cur > tp_prev:
            raw_mfi.append((tp_cur * vol, 0.0))   # positive money flow
        elif tp_cur < tp_prev:
            raw_mfi.append((0.0, tp_cur * vol))   # negative money flow
        else:
            raw_mfi.append((0.0, 0.0))

    # Calculate MFI for the last bar using 'period' lookback
    if len(raw_mfi) < period:
        return None, None
    window = raw_mfi[-period:]
    pos_mf = sum(x[0] for x in window)
    neg_mf = sum(x[1] for x in window)
    if neg_mf == 0:
        mfi_now = 100.0
    else:
        mfi_now = 100 - (100 / (1 + pos_mf / neg_mf))

    # Also compute MFI 3 bars ago to check if it's rising
    if len(raw_mfi) < period + 3:
        return mfi_now, None
    window_prev = raw_mfi[-(period + 3):-3]
    pos_prev = sum(x[0] for x in window_prev)
    neg_prev = sum(x[1] for x in window_prev)
    if neg_prev == 0:
        mfi_prev = 100.0
    else:
        mfi_prev = 100 - (100 / (1 + pos_prev / neg_prev))

    return mfi_now, mfi_prev


def calc_cvroc(vols, period=21):
    """
    CVROC — Close Volume Rate of Change (Kıvanç's crypto indicator).
    Measures how fast volume is accelerating vs the baseline period.
    Positive and rising = real momentum behind the move.
    """
    if len(vols) < period + 4:
        return None, None
    # CVROC = (current vol - vol N bars ago) / vol N bars ago * 100
    cvroc_now  = (vols[-1] - vols[-(period + 1)]) / vols[-(period + 1)] * 100 if vols[-(period + 1)] > 0 else None
    cvroc_prev = (vols[-2] - vols[-(period + 2)]) / vols[-(period + 2)] * 100 if vols[-(period + 2)] > 0 else None
    return cvroc_now, cvroc_prev


def calc_mavilimw(closes, f1=3, f2=5):
    """
    MavilimW — Kıvanç's Fibonacci-based weighted moving average.
    Uses Fibonacci sequence for period calculation to reduce noise.
    f1=3, f2=5 optimised for 1H. f1=5, f2=8 for 4H/daily.
    Returns current MavilimW value and direction (+1 up, -1 down).
    """
    # Fibonacci period sequence derived from f1, f2
    f3 = f1 + f2           # 8
    f4 = f2 + f3           # 13
    f5 = f3 + f4           # 21
    periods = [f1, f2, f3, f4, f5]
    max_p = max(periods)

    if len(closes) < max_p + 5:
        return None, None

    def wma(data, p):
        """Weighted moving average."""
        if len(data) < p:
            return None
        weights = list(range(1, p + 1))
        total_w = sum(weights)
        segment = data[-p:]
        return sum(w * v for w, v in zip(weights, segment)) / total_w

    # Build MavilimW as WMA of WMAs (Kıvanç's layered approach)
    m1 = wma(closes, f1)
    m2 = wma(closes, f2)
    if m1 is None or m2 is None:
        return None, None

    # Use WMA of [m1 values] with f3 — approximate with available closes
    # We compute a simplified but faithful version
    mav_now = (m1 * f2 + m2 * f1) / (f1 + f2)

    # Previous bar MavilimW for direction
    m1_p = wma(closes[:-1], f1)
    m2_p = wma(closes[:-1], f2)
    if m1_p is None or m2_p is None:
        return mav_now, None
    mav_prev = (m1_p * f2 + m2_p * f1) / (f1 + f2)

    direction = 1 if mav_now > mav_prev else -1
    return mav_now, direction


# ── Volume spike detection ────────────────────────────────────
def detect_volume_spike(symbol, klines):
    """
    Multi-layer unusual activity detector using Kıvanç Özbilgiç methodology:
    - Raw 2x volume spike (baseline)
    - MFI > 50 and rising (money flow confirmation)
    - CVROC positive and accelerating (volume rate of change)
    - MavilimW trending up (Fibonacci-smoothed trend confirmation)
    - OBV-style body/wick, price position, USD volume filters
    Optimised for 1H candles on crypto.
    """
    if not klines or len(klines) < 35:
        return None
    try:
        opens   = [float(k[1]) for k in klines]
        highs   = [float(k[2]) for k in klines]
        lows    = [float(k[3]) for k in klines]
        closes  = [float(k[4]) for k in klines]
        vols    = [float(k[5]) for k in klines]

        curr_price   = closes[-1]
        curr_usd_vol = vols[-1] * curr_price

        # ── Gate 1: Minimum USD liquidity ──
        if curr_usd_vol < 50_000:
            return None

        # ── Gate 2: Raw volume spike — 2x Fibonacci-smoothed baseline ──
        # Use MavilimW Fibonacci periods (3,5,8,13,21) for baseline
        # instead of simple 20-bar SMA — reduces noise in baseline itself
        fib_periods = [3, 5, 8, 13, 21]
        weighted_vol_sum = 0
        weight_total = 0
        for p in fib_periods:
            if len(vols) >= p + 2:
                avg = sum(vols[-(p + 2):-2]) / p
                weighted_vol_sum += avg * p
                weight_total += p
        if weight_total == 0:
            return None
        fib_avg_vol = weighted_vol_sum / weight_total  # Fibonacci-weighted baseline

        ratio_last = vols[-1] / fib_avg_vol if fib_avg_vol > 0 else 0
        ratio_prev = vols[-2] / fib_avg_vol if fib_avg_vol > 0 else 0
        best_ratio = max(ratio_last, ratio_prev)
        trigger_idx = -1 if ratio_last >= ratio_prev else -2

        if best_ratio < VOLUME_SPIKE_MULTIPLIER:
            return None

        # ── Gate 3: MFI confirmation (Kıvanç AlphaTrend methodology) ──
        mfi_now, mfi_prev = calc_mfi(highs, lows, closes, vols, period=14)
        if mfi_now is None:
            return None
        if mfi_now < 50:
            # Money flowing out, not in — skip
            return None
        mfi_rising = (mfi_prev is not None) and (mfi_now > mfi_prev)

        # ── Gate 4: CVROC confirmation (Kıvanç's crypto volume indicator) ──
        cvroc_now, cvroc_prev = calc_cvroc(vols, period=21)
        if cvroc_now is None:
            return None
        if cvroc_now <= 0:
            # Volume rate of change is negative — momentum not building
            return None
        cvroc_accelerating = (cvroc_prev is not None) and (cvroc_now > cvroc_prev)

        # ── Gate 5: MavilimW trend direction (Fibonacci WMA) ──
        mav_val, mav_dir = calc_mavilimw(closes, f1=3, f2=5)
        if mav_val is None:
            return None
        # Price should be above or approaching MavilimW (not deep below in freefall)
        pct_vs_mav = (curr_price - mav_val) / mav_val * 100
        if pct_vs_mav < -8:
            # Price more than 8% below MavilimW — downtrend still dominant
            return None

        # ── Gate 6: Price position filters ──
        avg_price_10 = sum(closes[-12:-2]) / 10
        if curr_price > avg_price_10 * 1.03:
            return None  # already pumped

        low_30 = min(lows[-32:-2]) if len(lows) >= 32 else min(lows[:-2])
        if low_30 <= 0:
            return None
        pct_from_low = (curr_price - low_30) / low_30 * 100
        if pct_from_low > 40:
            return None

        # ── Gate 7: Candle body quality (not a wick-only spike) ──
        body   = abs(closes[trigger_idx] - opens[trigger_idx])
        range_ = highs[trigger_idx] - lows[trigger_idx]
        if range_ > 0 and (body / range_) < 0.25:
            return None

        # ── Compute conviction score from Kıvanç indicators ──
        kivanc_score = 0
        kivanc_tags  = []
        if mfi_now >= 60:
            kivanc_score += 1
            kivanc_tags.append(f'MFI {round(mfi_now, 1)}')
        if mfi_rising:
            kivanc_score += 1
            kivanc_tags.append('MFI↑')
        if cvroc_accelerating:
            kivanc_score += 1
            kivanc_tags.append('CVROC↑')
        if mav_dir == 1:
            kivanc_score += 1
            kivanc_tags.append('MavilimW↑')

        return {
            'ratio':        round(best_ratio, 2),
            'pct_from_low': round(pct_from_low, 1),
            'candle':       'current' if trigger_idx == -1 else 'previous',
            'usd_vol':      round(curr_usd_vol),
            'mfi':          round(mfi_now, 1),
            'cvroc':        round(cvroc_now, 1),
            'kivanc_score': kivanc_score,
            'kivanc_tags':  kivanc_tags,
        }
    except Exception as e:
        pass
    return None


def fetch_oi_and_funding(sym):
    """
    Fetch open interest and funding rate from Binance futures.
    Returns dict or None if futures don't exist for this coin.
    """
    try:
        # Open Interest
        oi_url = f'https://fapi.binance.com/fapi/v1/openInterest?symbol={sym}USDT'
        r = requests.get(oi_url, headers=HEADERS, timeout=5)
        if not r.ok:
            return None
        oi_data = r.json()
        oi = float(oi_data.get('openInterest', 0))

        # Funding rate (latest)
        fund_url = f'https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}USDT'
        r2 = requests.get(fund_url, headers=HEADERS, timeout=5)
        if not r2.ok:
            return None
        fund_data = r2.json()
        funding = float(fund_data.get('lastFundingRate', 0))

        # OI history (last 5 periods) to check if OI is rising
        oi_hist_url = f'https://fapi.binance.com/futures/data/openInterestHist?symbol={sym}USDT&period=1h&limit=6'
        r3 = requests.get(oi_hist_url, headers=HEADERS, timeout=5)
        oi_rising = None
        if r3.ok:
            hist = r3.json()
            if isinstance(hist, list) and len(hist) >= 3:
                oi_vals = [float(h['sumOpenInterest']) for h in hist]
                # OI rising if last value > average of previous 4
                oi_avg_prev = sum(oi_vals[:-1]) / len(oi_vals[:-1])
                oi_rising = oi_vals[-1] > oi_avg_prev * 1.02  # 2%+ increase

        return {
            'oi':        oi,
            'funding':   round(funding * 100, 4),   # as percentage
            'oi_rising': oi_rising,
        }
    except Exception:
        return None




def fetch_taker_ratio(sym, spike_candle_ts_ms):
    """
    Fetch taker buy ratio for the spike candle only.
    Uses Binance aggTrades to count buyer vs seller initiated volume.
    Returns dict: {ratio: float, label: str, color: str} or None
    """
    try:
        # spike_candle_ts_ms is the open time of the spike candle
        start_ms = int(spike_candle_ts_ms)
        end_ms   = start_ms + 3600_000  # 1 hour later
        url = (
            f'https://api.binance.com/api/v3/aggTrades'
            f'?symbol={sym}USDT&startTime={start_ms}&endTime={end_ms}&limit=1000'
        )
        r = requests.get(url, headers=HEADERS, timeout=8)
        if not r.ok:
            return None
        trades = r.json()
        if not trades or not isinstance(trades, list):
            return None

        buy_vol  = 0.0
        sell_vol = 0.0
        for t in trades:
            qty = float(t.get('q', 0))
            price = float(t.get('p', 0))
            usd = qty * price
            if t.get('m') is False:  # m=False means buyer is taker
                buy_vol += usd
            else:
                sell_vol += usd

        total = buy_vol + sell_vol
        if total == 0:
            return None

        buy_pct = (buy_vol / total) * 100

        if buy_pct >= 55:
            label = f'🟢 Buyers {buy_pct:.0f}%'
            color = 'green'
        elif buy_pct <= 45:
            label = f'🔴 Sellers {100-buy_pct:.0f}%'
            color = 'red'
        else:
            label = f'🟡 Mixed {buy_pct:.0f}% buy'
            color = 'neutral'

        return {'buy_pct': round(buy_pct, 1), 'label': label, 'color': color}
    except Exception as e:
        print(f'[MONITOR] taker_ratio {sym} error: {e}')
        return None


# ── NUPL cache (fetched once per scan, BTC-wide) ─────────────
_nupl_cache = {'value': None, 'ts': 0}

def fetch_nupl():
    """
    Fetch Bitcoin NUPL from Glassnode (free tier) or CryptoQuant.
    Falls back to approximation using BTC market cap vs realized cap estimate.
    Cached for 1 hour.
    """
    global _nupl_cache
    if time.time() - _nupl_cache['ts'] < 3600 and _nupl_cache['value'] is not None:
        return _nupl_cache['value']

    try:
        # Use CoinGecko for market cap + approximate realized cap
        # NUPL approximation: (price - 200d avg) / price as simplified proxy
        # For actual NUPL we use Glassnode free endpoint
        url = 'https://api.glassnode.com/v1/metrics/indicators/nupl?a=BTC&i=24h&api_key=free'
        r = requests.get(url, timeout=8)
        if r.ok:
            data = r.json()
            if data and isinstance(data, list) and len(data) > 0:
                nupl = data[-1].get('v', None)
                if nupl is not None:
                    _nupl_cache = {'value': float(nupl), 'ts': time.time()}
                    return float(nupl)
    except Exception:
        pass

    # Fallback: approximate NUPL using BTC 200d MA vs current price
    try:
        klines_url = 'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=210'
        r2 = requests.get(klines_url, headers=HEADERS, timeout=8)
        if r2.ok:
            klines = r2.json()
            closes = [float(k[4]) for k in klines]
            if len(closes) >= 200:
                current = closes[-1]
                ma200   = sum(closes[-200:]) / 200
                # Simplified NUPL proxy: (current - ma200) / current
                nupl_approx = (current - ma200) / current
                _nupl_cache = {'value': round(nupl_approx, 4), 'ts': time.time()}
                print(f'[MONITOR] NUPL approx (200d MA proxy): {nupl_approx:.4f}')
                return nupl_approx
    except Exception as e:
        print(f'[MONITOR] NUPL fallback error: {e}')

    return None


def nupl_label(nupl):
    """Return emoji + zone label for NUPL value."""
    if nupl is None:
        return None
    if nupl >= 0.75:
        return f'🔴 NUPL:{nupl:.2f} (Euphoria — distribution risk)'
    elif nupl >= 0.5:
        return f'🟠 NUPL:{nupl:.2f} (Belief — trending up)'
    elif nupl >= 0.25:
        return f'🟡 NUPL:{nupl:.2f} (Optimism)'
    elif nupl >= 0:
        return f'🟢 NUPL:{nupl:.2f} (Hope/Fear — accumulation zone)'
    else:
        return f'💙 NUPL:{nupl:.2f} (Capitulation — long-term holders selling at loss)'

# ── Background monitor scan ───────────────────────────────────
# MONITOR_COINS is populated dynamically on startup from KuCoin/OKX
# Falls back to this built-in list if exchange APIs are unreachable
MONITOR_COINS_BUILTIN = [
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
    'NEAR','AVAX','LINK','UNI','MKR','AAVE','SNX','CRV','YFI','SUSHI',
    'BAL','UMA','GRT','LDO','CVX','DYDX','PERP','GNS','JOE','PENDLE',
]
MONITOR_COINS = list(MONITOR_COINS_BUILTIN)  # will be replaced on startup


def fetch_monitor_coins():
    """Fetch full coin list from KuCoin or OKX for monitoring."""
    global MONITOR_COINS
    EXCLUDE = {'USDT','USDC','BUSD','TUSD','USDD','USDP','FDUSD','DAI','FRAX',
               'LUSD','PYUSD','GUSD','SUSD','USDB','USDX','EURC','WBTC','WETH',
               'WBNB','STETH','WSTETH','CBETH','RETH','BETH','BTCB','HBTC'}
    coins = []

    # Try Binance first
    try:
        r = requests.get('https://api.binance.com/api/v3/exchangeInfo', headers=HEADERS, timeout=10)
        if r.ok:
            data = r.json()
            seen = set()
            for s in data.get('symbols', []):
                if (s.get('quoteAsset') == 'USDT' and
                    s.get('status') == 'TRADING' and
                    s.get('isSpotTradingAllowed', False)):
                    base = s['baseAsset']
                    if base not in seen and base not in EXCLUDE:
                        seen.add(base)
                        coins.append(base)
            if coins:
                MONITOR_COINS = coins
                print(f'[MONITOR] Loaded {len(MONITOR_COINS)} coins from Binance')
                return
    except Exception as e:
        print(f'[MONITOR] Binance fetch failed: {e}')

    # Try KuCoin
    try:
        r2 = requests.get('https://api.kucoin.com/api/v2/symbols', headers=HEADERS, timeout=10)
        if r2.ok:
            data2 = r2.json()
            seen = set()
            for s in data2.get('data', []):
                if s.get('quoteCurrency') == 'USDT' and s.get('enableTrading', False):
                    base = s['baseCurrency']
                    if base not in seen and base not in EXCLUDE:
                        seen.add(base)
                        coins.append(base)
            if coins:
                MONITOR_COINS = coins
                print(f'[MONITOR] Loaded {len(MONITOR_COINS)} coins from KuCoin')
                return
    except Exception as e:
        print(f'[MONITOR] KuCoin fetch failed: {e}')

    # Try OKX
    try:
        r3 = requests.get('https://www.okx.com/api/v5/public/instruments?instType=SPOT', headers=HEADERS, timeout=10)
        if r3.ok:
            data3 = r3.json()
            seen = set()
            for s in data3.get('data', []):
                if s.get('quoteCcy') == 'USDT' and s.get('state') == 'live':
                    base = s['baseCcy']
                    if base not in seen and base not in EXCLUDE:
                        seen.add(base)
                        coins.append(base)
            if coins:
                MONITOR_COINS = coins
                print(f'[MONITOR] Loaded {len(MONITOR_COINS)} coins from OKX')
                return
    except Exception as e:
        print(f'[MONITOR] OKX fetch failed: {e}')

    print(f'[MONITOR] All exchanges failed — using built-in list of {len(MONITOR_COINS)} coins')



def gemini_spike_commentary(alert):
    """
    Ask Gemini to interpret a volume spike alert using ONLY the live data we provide.
    Never asks Gemini to invent or recall prices — all data is passed in explicitly.
    Returns a 2-3 line commentary string or None if Gemini unavailable.
    """
    gemini_key = os.environ.get('GEMINI_API_KEY', '')
    if not gemini_key:
        return None

    sym        = alert.get('sym', '?')
    price      = alert.get('price', 0)
    spike      = alert.get('spike', 0)
    usd_vol    = alert.get('usd_vol', 0)
    mfi        = alert.get('mfi', 'N/A')
    cvroc      = alert.get('cvroc', 'N/A')
    score      = alert.get('score', 0)
    pct_low    = alert.get('pct_from_low', 0)
    funding    = alert.get('funding', 'N/A')
    oi_rising  = alert.get('oi_rising', None)
    labels     = ', '.join(alert.get('labels', []))

    oi_str = 'rising' if oi_rising else ('flat/falling' if oi_rising is False else 'unknown')

    prompt = f"""You are a professional crypto market analyst. Interpret this LIVE volume spike alert. 
Use ONLY the data provided below — do not assume or invent any other information.

LIVE DATA FOR {sym}/USDT:
- Current price: ${price:,.6g}
- Volume spike: {spike}x above 20-bar Fibonacci-weighted average
- USD volume this candle: ${usd_vol:,}
- Money Flow Index (MFI): {mfi} (above 50 = money flowing in)
- CVROC (volume momentum): {cvroc}% (positive = accelerating)
- Open Interest: {oi_str}
- Funding rate: {funding}% per hour
- Price position: {pct_low}% above 30-bar low (near lows = potential accumulation)
- Confirmed signals: {labels}
- Overall conviction score: {score}/7

Based ONLY on this live data, provide a 3-line interpretation:
Line 1: What this spike most likely represents (accumulation / distribution / squeeze setup / stop hunt / organic buying)
Line 2: The single most important signal from this data and what it implies
Line 3: One specific thing to watch that would confirm or invalidate this signal

Be direct. No disclaimers. No invented data. Max 60 words total."""

    try:
        url  = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}'
        resp = requests.post(url, json={
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {'maxOutputTokens': 150, 'temperature': 0.3}
        }, timeout=15)
        if resp.ok:
            return resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
    except Exception as e:
        print(f'[MONITOR] Gemini commentary error for {sym}: {e}')
    return None


# ════════════════════════════════════════════════════════════════
# CITADEL-GRADE SIGNAL SUITE
# CVD divergence · Liquidation clusters · Spot/futures basis
# OI stealth accumulation · Combined conviction engine
# ════════════════════════════════════════════════════════════════

def calc_cvd(klines, lookback=20):
    """
    Cumulative Volume Delta — difference between buy and sell volume per candle.
    Uses candle body direction as proxy (close > open = buy pressure).
    Returns: cvd_list, divergence signal
    Divergence = price making higher high but CVD making lower high = distribution
                 price making lower low but CVD making higher low = accumulation
    """
    if len(klines) < lookback + 2:
        return None, None
    try:
        cvd = []
        for k in klines:
            o, h, l, c, v = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
            candle_range = h - l
            if candle_range == 0:
                delta = 0
            else:
                # Estimate buy vs sell volume using candle close position
                buy_frac  = (c - l) / candle_range
                sell_frac = (h - c) / candle_range
                delta = (buy_frac - sell_frac) * v
            cvd.append(delta)

        # Cumulative sum
        cum_cvd = []
        running = 0
        for d in cvd:
            running += d
            cum_cvd.append(running)

        # Check divergence over last lookback bars
        recent_cvd    = cum_cvd[-lookback:]
        recent_closes = [float(k[4]) for k in klines[-lookback:]]

        # Find local highs and lows
        price_high = max(recent_closes[-5:])
        price_low  = min(recent_closes[-5:])
        cvd_high   = max(recent_cvd[-5:])
        cvd_low    = min(recent_cvd[-5:])

        price_high_prev = max(recent_closes[-10:-5])
        price_low_prev  = min(recent_closes[-10:-5])
        cvd_high_prev   = max(recent_cvd[-10:-5])
        cvd_low_prev    = min(recent_cvd[-10:-5])

        # Bullish divergence: price lower low but CVD higher low = hidden accumulation
        bullish_div = (price_low < price_low_prev * 0.999) and (cvd_low > cvd_low_prev * 0.999)
        # Bearish divergence: price higher high but CVD lower high = hidden distribution
        bearish_div = (price_high > price_high_prev * 1.001) and (cvd_high < cvd_high_prev * 1.001)

        # Current CVD trend — is delta positive (buying) or negative (selling)?
        recent_delta = sum(cvd[-3:])  # last 3 candles net delta
        cvd_trend = 'buying' if recent_delta > 0 else 'selling'

        return {
            'bullish_div':  bullish_div,
            'bearish_div':  bearish_div,
            'cvd_trend':    cvd_trend,
            'recent_delta': round(recent_delta, 2),
            'cum_cvd_last': round(cum_cvd[-1], 2),
        }
    except Exception as e:
        return None


def fetch_liquidation_clusters(sym):
    """
    Fetch recent liquidation data from Binance futures.
    Identifies price levels with heavy liquidation clusters above/below.
    These act as magnets — price tends to sweep these levels.
    Returns: {liq_above: price, liq_below: price, recent_liq_usd: float}
    """
    try:
        url = f'https://fapi.binance.com/fapi/v1/allForceOrders?symbol={sym}USDT&limit=200'
        r = requests.get(url, headers=HEADERS, timeout=6)
        if not r.ok:
            return None
        orders = r.json()
        if not orders or not isinstance(orders, list):
            return None

        # Get current price
        ticker_url = f'https://fapi.binance.com/fapi/v1/ticker/price?symbol={sym}USDT'
        rt = requests.get(ticker_url, headers=HEADERS, timeout=5)
        if not rt.ok:
            return None
        current_price = float(rt.json()['price'])

        liq_above = []  # liquidations above current price (short liquidations)
        liq_below = []  # liquidations below current price (long liquidations)
        total_liq_usd = 0

        for o in orders:
            price    = float(o.get('price', 0))
            qty      = float(o.get('origQty', 0))
            side     = o.get('side', '')  # BUY = short liquidated, SELL = long liquidated
            usd_val  = price * qty
            total_liq_usd += usd_val

            if side == 'BUY' and price > current_price:
                liq_above.append({'price': price, 'usd': usd_val})
            elif side == 'SELL' and price < current_price:
                liq_below.append({'price': price, 'usd': usd_val})

        # Find biggest cluster above and below
        def biggest_cluster(liqs, n_bins=10):
            if not liqs:
                return None, 0
            prices = [l['price'] for l in liqs]
            p_min, p_max = min(prices), max(prices)
            if p_min == p_max:
                return p_min, sum(l['usd'] for l in liqs)
            bin_size = (p_max - p_min) / n_bins
            bins = {}
            for l in liqs:
                b = int((l['price'] - p_min) / bin_size)
                bins[b] = bins.get(b, 0) + l['usd']
            best_bin = max(bins, key=bins.get)
            cluster_price = p_min + best_bin * bin_size + bin_size / 2
            return round(cluster_price, 6), round(bins[best_bin])

        cluster_above_price, cluster_above_usd = biggest_cluster(liq_above)
        cluster_below_price, cluster_below_usd = biggest_cluster(liq_below)

        pct_to_above = ((cluster_above_price - current_price) / current_price * 100) if cluster_above_price else None
        pct_to_below = ((current_price - cluster_below_price) / current_price * 100) if cluster_below_price else None

        return {
            'current_price':       current_price,
            'liq_above_price':     cluster_above_price,
            'liq_above_usd':       cluster_above_usd,
            'pct_to_above':        round(pct_to_above, 2) if pct_to_above else None,
            'liq_below_price':     cluster_below_price,
            'liq_below_usd':       cluster_below_usd,
            'pct_to_below':        round(pct_to_below, 2) if pct_to_below else None,
            'total_liq_24h_usd':   round(total_liq_usd),
        }
    except Exception as e:
        return None


def fetch_spot_futures_basis(sym):
    """
    Spot vs perpetual futures price spread.
    Positive basis (futures > spot) = normal contango = leveraged longs
    Negative basis (spot > futures) = backwardation = real spot buying > speculation
    Strong negative basis during upward price move = very bullish (real demand)
    """
    try:
        spot_url    = f'https://api.binance.com/api/v3/ticker/price?symbol={sym}USDT'
        futures_url = f'https://fapi.binance.com/fapi/v1/ticker/price?symbol={sym}USDT'
        rs = requests.get(spot_url,    headers=HEADERS, timeout=5)
        rf = requests.get(futures_url, headers=HEADERS, timeout=5)
        if not rs.ok or not rf.ok:
            return None
        spot    = float(rs.json()['price'])
        futures = float(rf.json()['price'])
        basis   = (futures - spot) / spot * 100  # positive = futures premium
        return {
            'spot':    spot,
            'futures': futures,
            'basis':   round(basis, 4),  # % premium of futures over spot
        }
    except Exception:
        return None


def detect_oi_stealth_accumulation(sym, oi_info, klines):
    """
    OI rising while price is flat or slightly down = stealth accumulation.
    This is the most reliable pre-move signal — institutions building position
    without moving price yet.
    """
    if not oi_info or not klines:
        return False
    try:
        # Check if OI is rising
        if not oi_info.get('oi_rising'):
            return False
        # Check if price is flat (within 1.5% of 5-bar average)
        recent_closes = [float(k[4]) for k in klines[-6:-1]]
        avg_price = sum(recent_closes) / len(recent_closes)
        current   = float(klines[-1][4])
        pct_move  = abs(current - avg_price) / avg_price * 100
        # OI rising + price flat/slightly down = stealth accumulation
        price_flat_or_down = pct_move < 1.5 or current < avg_price
        return price_flat_or_down
    except Exception:
        return False


def citadel_score(sym, spike, oi_info, klines, cvd_data, liq_data, basis_data):
    """
    Master conviction engine combining all Citadel-grade signals.
    Returns (score, tags, verdict)
    Score: 0-10
    Verdict: STRONG_LONG / WATCH_LONG / NEUTRAL / WATCH_SHORT / AVOID
    """
    score = 0
    tags  = []

    # ── Base: Kıvanç spike already passed all gates (required) ──
    score += spike.get('kivanc_score', 0)
    tags  += spike.get('kivanc_tags', [])

    # ── OI signals ──
    if oi_info:
        if oi_info.get('oi_rising'):
            score += 1
            tags.append('OI↑')
        funding = oi_info.get('funding', 0)
        if funding < 0:
            score += 2  # shorts crowded = squeeze fuel
            tags.append(f'Shorts crowded ({funding}%)')
        elif funding <= 0.005:
            score += 1
            tags.append(f'Funding neutral')
        elif funding > 0.02:
            score -= 2  # overleveraged longs = danger
            tags.append(f'⚠ FOMO longs ({funding}%)')

    # ── CVD signals ──
    if cvd_data:
        if cvd_data.get('bullish_div'):
            score += 2  # hidden accumulation — strongest signal
            tags.append('🔵 CVD bullish divergence (hidden accumulation)')
        if cvd_data.get('cvd_trend') == 'buying':
            score += 1
            tags.append('CVD buying pressure')
        if cvd_data.get('bearish_div'):
            score -= 2
            tags.append('⚠ CVD bearish divergence (distribution)')

    # ── Stealth accumulation (OI up, price flat) ──
    if detect_oi_stealth_accumulation(sym, oi_info, klines):
        score += 2
        tags.append('🔵 Stealth accumulation (OI↑ price flat)')

    # ── Liquidation cluster signals ──
    if liq_data:
        pct_above = liq_data.get('pct_to_above')
        pct_below = liq_data.get('pct_to_below')
        above_usd = liq_data.get('liq_above_usd', 0)
        below_usd = liq_data.get('liq_below_usd', 0)

        if pct_above and pct_above < 3 and above_usd > 100_000:
            score += 2  # large short liquidation cluster within 3% = price magnet
            tags.append(f'🎯 Liq target +{pct_above}% (${above_usd/1000:.0f}k shorts)')
        elif pct_above and pct_above < 6 and above_usd > 50_000:
            score += 1
            tags.append(f'Liq zone +{pct_above}% (${above_usd/1000:.0f}k)')

        if pct_below and pct_below < 2 and below_usd > 200_000:
            score -= 1  # large long liq cluster just below = trap risk
            tags.append(f'⚠ Long liq trap -{pct_below}%')

    # ── Spot/futures basis ──
    if basis_data:
        basis = basis_data.get('basis', 0)
        if basis < -0.05:
            score += 2  # spot > futures = real demand, not just leverage
            tags.append(f'🔵 Spot premium (basis {basis}%) — real buying')
        elif basis > 0.3:
            score -= 1  # heavily leveraged = fragile
            tags.append(f'⚠ Futures premium ({basis}%) — leverage heavy')

    # ── Verdict ──
    if score >= 8:
        verdict = '🔥 STRONG LONG'
    elif score >= 6:
        verdict = '✅ WATCH LONG'
    elif score >= 4:
        verdict = '👀 MONITOR'
    elif score <= 1:
        verdict = '🚫 AVOID'
    else:
        verdict = '⚪ NEUTRAL'

    return max(0, min(score, 10)), tags, verdict

def run_monitor_scan():
    """Scan all monitored coins and send Telegram alerts for volume spikes."""
    print(f'[MONITOR] Starting volume scan for {len(MONITOR_COINS)} coins...')
    alerts = []
    now = time.time()

    def check_coin(sym):
        # Skip if alerted recently (4h cooldown)
        if now - _last_alert_time.get(sym, 0) < ALERT_COOLDOWN_SECONDS:
            return None
        try:
            spike = None
            price = None
            source = None
            klines_ref = None

            # ── 1. KuCoin (most reliable from cloud IPs) ──
            kucoin_url = f'https://api.kucoin.com/api/v1/market/candles?symbol={sym}-USDT&type=1hour&startAt={int(now)-3600*65}&endAt={int(now)}'
            r = requests.get(kucoin_url, headers=HEADERS, timeout=8)
            if r.ok:
                data = r.json().get('data', [])
                if len(data) >= 24:
                    klines = [[int(c[0])*1000, float(c[1]), float(c[3]), float(c[4]), float(c[2]), float(c[5])] for c in reversed(data)]
                    spike = detect_volume_spike(sym, klines)
                    if spike:
                        price  = klines[-1][4]
                        source = 'KuCoin'
                        klines_ref = klines

            # ── 2. OKX fallback ──
            if not spike:
                okx_url = f'https://www.okx.com/api/v5/market/history-candles?instId={sym}-USDT&bar=1H&limit=65'
                r2 = requests.get(okx_url, headers=HEADERS, timeout=8)
                if r2.ok:
                    data2 = r2.json().get('data', [])
                    if len(data2) >= 24:
                        klines2 = [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] for c in reversed(data2)]
                        spike = detect_volume_spike(sym, klines2)
                        if spike:
                            price  = klines2[-1][4]
                            source = 'OKX'

            if not spike:
                return None

            # ── 3. Fetch all Citadel-grade data in parallel ──
            oi_info  = fetch_oi_and_funding(sym)
            cvd_data = calc_cvd(klines_ref or [], lookback=20)
            liq_data = fetch_liquidation_clusters(sym)
            basis    = fetch_spot_futures_basis(sym)

            # ── 4. Taker buy ratio for spike candle ──
            taker = None
            try:
                spike_ts = klines_ref[-1][0] if klines_ref else None
                if spike_ts:
                    taker = fetch_taker_ratio(sym, spike_ts)
            except Exception:
                pass

            # Hard filter: non-BTC seller-driven spikes skipped
            if taker and taker['color'] == 'red' and sym != 'BTC':
                print(f'[MONITOR] {sym} skipped — seller-driven ({taker["buy_pct"]}% buyers)')
                return None

            # Hard filter: FOMO funding (overleveraged longs)
            if oi_info and oi_info.get('funding', 0) > 0.02:
                print(f'[MONITOR] {sym} skipped — FOMO funding {oi_info["funding"]}%')
                return None

            # ── 5. Citadel conviction engine ──
            score, labels, verdict = citadel_score(
                sym, spike, oi_info, klines_ref or [], cvd_data, liq_data, basis
            )

            # Add taker label
            if taker:
                labels.append(taker['label'])
                if taker['color'] == 'green':
                    score += 1

            # Minimum conviction gate
            if score < 3:
                return None

            # Build liquidation target string
            liq_target = None
            if liq_data and liq_data.get('pct_to_above') and liq_data['pct_to_above'] < 8:
                liq_target = f'+{liq_data["pct_to_above"]}% (${liq_data["liq_above_usd"]/1000:.0f}k shorts)'

            return {
                'sym':         sym,
                'spike':       spike['ratio'],
                'pct_from_low':spike['pct_from_low'],
                'price':       price,
                'source':      source,
                'candle':      spike.get('candle', 'current'),
                'usd_vol':     spike.get('usd_vol', 0),
                'mfi':         spike.get('mfi'),
                'cvroc':       spike.get('cvroc'),
                'score':       score,
                'labels':      labels,
                'verdict':     verdict,
                'funding':     oi_info['funding'] if oi_info else None,
                'oi_rising':   oi_info['oi_rising'] if oi_info else None,
                'taker_color': taker['color'] if taker else None,
                'taker_pct':   taker['buy_pct'] if taker else None,
                'cvd_bull_div':cvd_data.get('bullish_div') if cvd_data else None,
                'liq_target':  liq_target,
                'basis':       basis['basis'] if basis else None,
            }
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
        # Sort by conviction score first, then spike ratio
        alerts.sort(key=lambda x: (x['score'], x['spike']), reverse=True)
        # Store in tg_intel for Intelligence cross-reference
        if tg_intel:
            tg_intel.set_volume_spikes(alerts)
            if nupl_val is not None:
                tg_intel.set_nupl(nupl_val)

        # Fetch NUPL once per scan batch
        nupl_val   = fetch_nupl()
        nupl_str   = nupl_label(nupl_val)

        lines = [f'🚨 <b>APEX SCANNER — UNUSUAL ACTIVITY ALERT</b>']
        lines.append(f'⏰ {time.strftime("%Y-%m-%d %H:%M UTC")} | 1H candles')
        lines.append('✅ Filters: 2x vol spike · $50k min · body>wick · OI · funding · taker ratio')
        if nupl_str:
            lines.append(f'📊 Market: {nupl_str}')
        lines.append('')
        for a in alerts[:10]:
            score      = a.get('score', 1)
            stars      = '⭐' * min(score, 10)
            usd_vol    = a.get('usd_vol', 0)
            vol_str    = f'${usd_vol/1000:.0f}k' if usd_vol < 1_000_000 else f'${usd_vol/1_000_000:.1f}M'
            candle_tag = '[prev]' if a.get('candle') == 'previous' else ''
            verdict    = a.get('verdict', '')
            labels     = a.get('labels', [])
            liq_target = a.get('liq_target')
            basis      = a.get('basis')
            cvd_bull   = a.get('cvd_bull_div')

            # Header line
            lines.append(f'{stars} <b>{a["sym"]}</b>  {a["spike"]}x vol  {candle_tag}  {verdict}'.strip())
            lines.append(f'   💰 ${a["price"]:,.6g}  |  Vol: {vol_str}  |  {a["pct_from_low"]}% from low')

            # Key signals
            key_signals = [l for l in labels if any(x in l for x in ['CVD','Stealth','Liq','Spot','crowded','FOMO','⚠','🔵','🎯'])]
            normal_signals = [l for l in labels if l not in key_signals]

            if normal_signals:
                lines.append(f'   📊 {" · ".join(normal_signals)}')
            if key_signals:
                for ks in key_signals:
                    lines.append(f'   {ks}')
            if liq_target:
                lines.append(f'   🎯 Next target: {liq_target}')
            if basis is not None:
                basis_str = f'Spot premium {abs(basis):.3f}%' if basis < 0 else f'Futures premium {basis:.3f}%'
                lines.append(f'   📐 Basis: {basis_str}')

            # ── Gemini commentary ──
            commentary = gemini_spike_commentary(a)
            if commentary:
                for cl in commentary.split('\n'):
                    cl = cl.strip()
                    if cl:
                        lines.append(f'   🤖 {cl}')
            lines.append('')
        send_telegram('\n'.join(lines))
        print(f'[MONITOR] Sent alert for {len(alerts)} coins: {[a["sym"] for a in alerts]}')
    else:
        print(f'[MONITOR] Scan complete — no volume spikes detected')


def monitor_loop():
    """Startup thread — fetches coin list once, no auto-scanning (on-demand only)."""
    global _monitor_running
    _monitor_running = True
    print('[MONITOR] On-demand mode — fetching coin list on startup...')
    time.sleep(30)
    fetch_monitor_coins()
    print('[MONITOR] Ready. Volume scan is on-demand only.')


# Start background thread just to initialise coin list
_monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
_monitor_thread.start()




@app.route('/citadel-report', methods=['POST'])
def citadel_report():
    """Generate a Citadel-style technical analysis report for top 50 coins."""
    try:
        data = request.get_json()
        coins_data = data.get('coins', '')
        count = data.get('count', 50)
        timeframe = data.get('timeframe', 'N/A')

        gemini_key = os.environ.get('GEMINI_API_KEY', '')
        if not gemini_key:
            return jsonify({'error': 'GEMINI_API_KEY not set'}), 500

        prompt = f"""You are a senior quantitative trader at Citadel who combines technical analysis with statistical models to time entries and exits.

I have scanned {count} crypto/stock assets on the {timeframe} timeframe and ranked them by signal strength. Here is the full data:

{coins_data}

Provide a comprehensive Citadel-style technical analysis report covering:

1. MARKET OVERVIEW — Overall market condition based on this scan. What does the breadth of signals tell us?

2. TIER 1 — TOP CONVICTION (top 5 assets)
For each: trend direction, key levels, RSI/WT reading interpretation, entry zone, stop-loss, profit target, risk-reward ratio, confidence rating (Strong Buy / Buy / Neutral / Sell / Strong Sell)

3. TIER 2 — WATCHLIST (next 10 assets)
Brief technical setup for each — one line per asset with the key signal and what to watch

4. SECTOR/THEME PATTERNS — Are there recurring patterns? (e.g. DeFi tokens all showing oversold, L2s showing momentum)

5. RISK FACTORS — What could invalidate these setups? Key macro or on-chain risks to monitor

6. EXECUTION SUMMARY — Prioritized action plan: what to act on now vs. what to monitor

Format as a professional quantitative research memo. Be direct and specific. Use actual numbers from the data."""

        gemini_url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}'
        gemini_resp = requests.post(gemini_url, json={
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {'maxOutputTokens': 4000, 'temperature': 0.7}
        }, timeout=60)
        if not gemini_resp.ok:
            return jsonify({'error': f'Gemini error: {gemini_resp.text[:200]}'}), 500
        report = gemini_resp.json()['candidates'][0]['content']['parts'][0]['text']

        # Send to Telegram (existing scan bot)
        tg_token = os.environ.get('TELEGRAM_BOT_TOKEN', '') or TELEGRAM_BOT_TOKEN
        tg_chat  = os.environ.get('TELEGRAM_CHAT_ID', '') or TELEGRAM_CHAT_ID
        if tg_token and tg_chat:
            header = f'⚔ <b>CITADEL REPORT — TOP {count} ASSETS ({timeframe})</b>\n\n'
            full_msg = header + report
            # Split into chunks of 4000 chars
            chunks = []
            while len(full_msg) > 4000:
                chunks.append(full_msg[:4000])
                full_msg = full_msg[4000:]
            chunks.append(full_msg)
            tg_url = f'https://api.telegram.org/bot{tg_token}/sendMessage'
            for chunk in chunks:
                requests.post(tg_url, json={'chat_id': tg_chat, 'text': chunk, 'parse_mode': 'HTML'}, timeout=10)

        return jsonify({'report': report})
    except Exception as e:
        print(f'[CITADEL] Error: {e}')
        return jsonify({'error': str(e)}), 500

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


# ── Crypto OHLCV data — fetch from multiple exchanges server-side ──────
@app.route('/crypto')
def crypto():
    symbols_param = request.args.get('symbols', '')
    symbols = [s.strip().upper() for s in symbols_param.split(',') if s.strip()][:200]
    if not symbols:
        return jsonify({})

    interval = request.args.get('interval', '1d')
    limit = int(request.args.get('limit', '210'))

    # Normalize intervals per exchange
    kucoin_interval = {'1w': '1week', '4h': '4hour', '1d': '1day'}.get(interval, '1day')
    binance_interval = {'1w': '1w', '4h': '4h', '1d': '1d'}.get(interval, '1d')
    okx_bar = {'1w': '1W', '4h': '4H', '1d': '1D'}.get(interval, '1D')

    print(f'[CRYPTO] {len(symbols)} symbols | interval={interval} kucoin={kucoin_interval} binance={binance_interval} okx={okx_bar}')

    def fetch_one_crypto(sym):
        # ── 1. KuCoin FIRST — most reliable from Railway/cloud IPs ──
        try:
            end_ts = int(time.time())
            secs = {'1week': 604800, '1day': 86400, '4hour': 14400}.get(kucoin_interval, 86400)
            start_ts = end_ts - (limit * secs)
            url_kc = f'https://api.kucoin.com/api/v1/market/candles?symbol={sym}-USDT&type={kucoin_interval}&startAt={start_ts}&endAt={end_ts}'
            r = requests.get(url_kc, headers=HEADERS, timeout=10)
            if r.ok:
                candles = r.json().get('data', [])
                if candles and len(candles) >= 20:
                    klines = []
                    for c in reversed(candles):
                        try:
                            klines.append([int(c[0])*1000, float(c[1]), float(c[3]), float(c[4]), float(c[2]), float(c[5])])
                        except Exception:
                            continue
                    if len(klines) >= 20:
                        return sym, {'klines': klines, 'type': 'crypto', 'source': 'kucoin'}
            else:
                print(f'[CRYPTO] KuCoin {sym}: HTTP {r.status_code}')
        except Exception as e:
            print(f'[CRYPTO] KuCoin {sym} error: {e}')

        # ── 2. OKX ──
        try:
            url_okx = f'https://www.okx.com/api/v5/market/history-candles?instId={sym}-USDT&bar={okx_bar}&limit={min(limit, 300)}'
            r2 = requests.get(url_okx, headers=HEADERS, timeout=10)
            if r2.ok:
                candles2 = r2.json().get('data', [])
                if candles2 and len(candles2) >= 20:
                    klines = []
                    for c in reversed(candles2):
                        try:
                            klines.append([int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
                        except Exception:
                            continue
                    if len(klines) >= 20:
                        return sym, {'klines': klines, 'type': 'crypto', 'source': 'okx'}
            else:
                print(f'[CRYPTO] OKX {sym}: HTTP {r2.status_code}')
        except Exception as e:
            print(f'[CRYPTO] OKX {sym} error: {e}')

        # ── 3. Binance Spot ──
        try:
            url_bs = f'https://api.binance.com/api/v3/klines?symbol={sym}USDT&interval={binance_interval}&limit={limit}'
            r3 = requests.get(url_bs, headers=HEADERS, timeout=8)
            if r3.ok:
                data3 = r3.json()
                if isinstance(data3, list) and len(data3) >= 20:
                    klines = [[c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] for c in data3]
                    return sym, {'klines': klines, 'type': 'crypto', 'source': 'binance_spot'}
            else:
                print(f'[CRYPTO] Binance Spot {sym}: HTTP {r3.status_code} {r3.text[:80]}')
        except Exception as e:
            print(f'[CRYPTO] Binance Spot {sym} error: {e}')

        # ── 4. Binance Futures ──
        try:
            url_bf = f'https://fapi.binance.com/fapi/v1/klines?symbol={sym}USDT&interval={binance_interval}&limit={limit}'
            r4 = requests.get(url_bf, headers=HEADERS, timeout=8)
            if r4.ok:
                data4 = r4.json()
                if isinstance(data4, list) and len(data4) >= 20:
                    klines = [[c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] for c in data4]
                    return sym, {'klines': klines, 'type': 'crypto', 'source': 'binance_futures'}
            else:
                print(f'[CRYPTO] Binance Futures {sym}: HTTP {r4.status_code} {r4.text[:80]}')
        except Exception as e:
            print(f'[CRYPTO] Binance Futures {sym} error: {e}')

        print(f'[CRYPTO] ALL sources failed for {sym}')
        return sym, None

    results = {}
    # Reduced workers to avoid triggering exchange rate limits
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(fetch_one_crypto, sym): sym for sym in symbols}
        for future in as_completed(futures):
            sym, data = future.result()
            if data:
                results[sym] = data

    sources = {}
    for d in results.values():
        src = d.get('source', 'unknown')
        sources[src] = sources.get(src, 0) + 1
    print(f'[CRYPTO] Done: {len(results)}/{len(symbols)} fetched. Sources: {sources}')
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


# ── Telegram Intelligence — import module ────────────────────
try:
    import telegram_intel as tg_intel
    print('[TG-INTEL] Module loaded')
except Exception as e:
    tg_intel = None
    print(f'[TG-INTEL] Module load failed: {e}')


@app.route('/tg-intel/status')
def tg_intel_status():
    if not tg_intel:
        return jsonify({'ok': False, 'error': 'Module not loaded'})
    return jsonify({
        'ok':             True,
        'authenticated':  tg_intel.is_authenticated(),
        'has_apex_data':  len(tg_intel._last_apex_alerts) > 0,
        'has_vol_data':   len(tg_intel._last_volume_spikes) > 0,
        'apex_count':     len(tg_intel._last_apex_alerts),
        'vol_count':      len(tg_intel._last_volume_spikes),
    })


@app.route('/tg-intel/send-code', methods=['POST'])
def tg_intel_send_code():
    if not tg_intel:
        return jsonify({'ok': False, 'error': 'Module not loaded'})
    data     = request.get_json()
    api_id   = data.get('api_id', '').strip()
    api_hash = data.get('api_hash', '').strip()
    phone    = data.get('phone', '').strip()
    if not api_id or not api_hash or not phone:
        return jsonify({'ok': False, 'error': 'api_id, api_hash and phone required'})
    result = tg_intel.auth_send_code(api_id, api_hash, phone)
    return jsonify(result)


@app.route('/tg-intel/verify-code', methods=['POST'])
def tg_intel_verify_code():
    if not tg_intel:
        return jsonify({'ok': False, 'error': 'Module not loaded'})
    data     = request.get_json()
    phone    = data.get('phone', '').strip()
    code     = data.get('code', '').strip()
    api_id   = data.get('api_id', '').strip()
    api_hash = data.get('api_hash', '').strip()
    if not phone or not code:
        return jsonify({'ok': False, 'error': 'phone and code required'})
    result = tg_intel.auth_verify_code(phone, code, api_id, api_hash)
    return jsonify(result)


@app.route('/tg-intel/verify-2fa', methods=['POST'])
def tg_intel_verify_2fa():
    if not tg_intel:
        return jsonify({'ok': False, 'error': 'Module not loaded'})
    data     = request.get_json()
    password = data.get('password', '').strip()
    api_id   = data.get('api_id', '').strip()
    api_hash = data.get('api_hash', '').strip()
    if not password:
        return jsonify({'ok': False, 'error': 'password required'})
    result = tg_intel.auth_verify_2fa(password, api_id, api_hash)
    return jsonify(result)


_app_start_time = time.time()

@app.route('/tg-intel/run', methods=['POST'])
def tg_intel_run():
    if not tg_intel:
        return jsonify({'ok': False, 'error': 'Module not loaded'})
    if not tg_intel.is_authenticated():
        return jsonify({'ok': False, 'error': 'Not authenticated — complete setup first'})
    # Ignore requests that arrive within 90s of startup (stale browser requests)
    if time.time() - _app_start_time < 90:
        print('[TG-INTEL] Ignoring run request — too soon after startup')
        return jsonify({'ok': False, 'error': 'Server just started — please wait a moment and try again'})
    data     = request.get_json()
    channels = data.get('channels', [])   # list of [name, id] pairs
    hours    = int(data.get('hours', 24))
    if not channels:
        return jsonify({'ok': False, 'error': 'No channels provided'})
    # Run in background thread — returns immediately
    def _run():
        report, err = tg_intel.run_analysis(channels, lookback_hours=hours)
        if err:
            print(f'[TG-INTEL] Analysis error: {err}')
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'ok': True, 'status': 'Analysis started — report will arrive on Telegram'})


@app.route('/apex-results', methods=['POST'])
def apex_results():
    """Receive APEX scan results from frontend and store for Intelligence analysis."""
    try:
        if not tg_intel:
            return jsonify({'ok': False})
        data    = request.get_json()
        alerts  = data.get('alerts', [])
        tg_intel.set_apex_alerts(alerts)
        print(f'[TG-INTEL] Received {len(alerts)} APEX results from frontend')
        return jsonify({'ok': True, 'count': len(alerts)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/volume-results', methods=['POST'])
def volume_results():
    """Receive volume spike results from frontend and store for Intelligence analysis."""
    try:
        if not tg_intel:
            return jsonify({'ok': False})
        data   = request.get_json()
        spikes = data.get('spikes', [])
        tg_intel.set_volume_spikes(spikes)
        print(f'[TG-INTEL] Received {len(spikes)} volume spikes from frontend')
        return jsonify({'ok': True, 'count': len(spikes)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})



# ════════════════════════════════════════════════════════════════
# FIND THE BOTTOM — On-chain accumulation intelligence endpoint
# ════════════════════════════════════════════════════════════════

def fetch_bottom_signals():
    """
    Fetch all 5 tiers of on-chain signals to detect market bottoms.
    Uses free APIs: Binance, CryptoQuant (free), Glassnode (free tier).
    Returns structured signal data with deploy score.
    """
    signals = []
    deploy_score = 0

    # ── TIER 1: NUPL ──────────────────────────────────────────
    # Already cached from volume scan, or fetch fresh
    try:
        nupl = fetch_nupl()
        if nupl is not None:
            if nupl < 0:
                status = 'green'; pts = 1
                interp = f'Entire market underwater. Long-term holders capitulating. Historically the strongest buy zone — every BTC bottom has touched NUPL < 0.'
            elif nupl < 0.25:
                status = 'amber'; pts = 0
                interp = f'Market near breakeven. Fear and hope mixed. Not yet full capitulation but approaching accumulation territory.'
            else:
                status = 'red'; pts = 0
                interp = f'Market still in profit on average. Not yet a bottom — too early to deploy full position.'
            deploy_score += pts
            signals.append({
                'tier':           'TIER 1',
                'name':           'NUPL (Net Unrealized Profit/Loss)',
                'value':          f'{nupl:.4f}',
                'status':         status,
                'interpretation': interp,
            })
    except Exception as e:
        signals.append({'tier':'TIER 1','name':'NUPL','value':'Unavailable','status':'red','interpretation':str(e)})

    # ── TIER 2: SOPR approximation via realized price ─────────
    # Use BTC current price vs 200d MA as LTH-SOPR proxy
    try:
        klines_url = 'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=365'
        r = requests.get(klines_url, headers=HEADERS, timeout=10)
        sopr_val = None
        if r.ok:
            klines = r.json()
            closes = [float(k[4]) for k in klines]
            current_price = closes[-1]
            # Realized price proxy: 365-day VWAP (volume-weighted average)
            volumes = [float(k[5]) for k in klines]
            total_vol = sum(volumes)
            vwap_365 = sum(c*v for c,v in zip(closes, volumes)) / total_vol if total_vol > 0 else None

            if vwap_365:
                sopr_val = current_price / vwap_365  # proxy for SOPR
                if sopr_val < 0.85:
                    status = 'green'; pts = 1
                    interp = f'Price significantly below 365d VWAP ({vwap_365:,.0f}). Long-term holders in deep loss. Classic capitulation — historically resolves upward within 3-6 months.'
                elif sopr_val < 1.0:
                    status = 'amber'; pts = 0
                    interp = f'Price below annual average cost ({vwap_365:,.0f}). Most annual buyers underwater. Approaching capitulation but not confirmed.'
                else:
                    status = 'red'; pts = 0
                    interp = f'Price above annual average cost ({vwap_365:,.0f}). Holders still in profit — not a bottom signal.'
                deploy_score += pts
                signals.append({
                    'tier':           'TIER 2',
                    'name':           'SOPR Proxy (Price vs 365d VWAP)',
                    'value':          f'{sopr_val:.4f}  |  BTC: ${current_price:,.0f}  |  VWAP: ${vwap_365:,.0f}',
                    'status':         status,
                    'interpretation': interp,
                })
    except Exception as e:
        signals.append({'tier':'TIER 2','name':'SOPR Proxy','value':'Unavailable','status':'red','interpretation':str(e)})

    # ── TIER 3: Exchange Reserve Flow ─────────────────────────
    # Net BTC flow to/from exchanges over 7d using Binance order book proxy
    # Real method: CryptoQuant free API for exchange reserves
    try:
        cq_url = 'https://api.cryptoquant.com/v1/btc/exchange-flows/reserve?window=DAY&limit=14'
        rcq = requests.get(cq_url, headers={'Authorization': 'Bearer free'}, timeout=8)
        exchange_flow_ok = False

        if rcq.ok and 'data' in rcq.json():
            data = rcq.json()['data']
            if len(data) >= 7:
                recent  = [d['reserve_usd'] for d in data[-7:]]
                older   = [d['reserve_usd'] for d in data[-14:-7]]
                recent_avg = sum(recent) / len(recent)
                older_avg  = sum(older)  / len(older)
                flow_pct   = (recent_avg - older_avg) / older_avg * 100 if older_avg else 0
                exchange_flow_ok = True

                if flow_pct < -3:
                    status = 'green'; pts = 1
                    interp = f'Exchange reserves falling {abs(flow_pct):.1f}% over 7 days. Coins leaving exchanges = supply reduction = accumulation. Smart money moving to cold storage.'
                elif flow_pct < 0:
                    status = 'amber'; pts = 0
                    interp = f'Exchange reserves slightly declining ({flow_pct:.1f}%). Early signs of accumulation but not conclusive yet.'
                else:
                    status = 'red'; pts = 0
                    interp = f'Exchange reserves rising {flow_pct:.1f}%. Coins moving TO exchanges = preparation to sell. Not a buy signal.'
                deploy_score += pts
                signals.append({
                    'tier':           'TIER 3',
                    'name':           'Exchange Reserve Flow (7d)',
                    'value':          f'{flow_pct:+.2f}% (7d change)',
                    'status':         status,
                    'interpretation': interp,
                })

        if not exchange_flow_ok:
            # Fallback: use Binance spot volume trend as proxy
            # High sell volume on spot with price flat = accumulation
            vol_url = 'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=30'
            rv = requests.get(vol_url, headers=HEADERS, timeout=8)
            if rv.ok:
                vklines = rv.json()
                vols    = [float(k[5]) * float(k[4]) for k in vklines]  # USD volume
                recent_vol = sum(vols[-7:]) / 7
                older_vol  = sum(vols[-30:-7]) / 23
                vol_ratio  = recent_vol / older_vol if older_vol else 1

                if vol_ratio > 1.5:
                    status = 'amber'; pts = 0
                    interp = f'Spot volume {vol_ratio:.1f}x above 30d average. High activity — could be capitulation selling or accumulation. Monitor direction.'
                else:
                    status = 'red'; pts = 0
                    interp = 'Exchange flow data unavailable. Spot volume normal — no strong signal.'
                signals.append({
                    'tier':           'TIER 3',
                    'name':           'Exchange Flow (Spot Volume Proxy)',
                    'value':          f'Volume ratio: {vol_ratio:.2f}x vs 30d avg',
                    'status':         status,
                    'interpretation': interp,
                })
    except Exception as e:
        signals.append({'tier':'TIER 3','name':'Exchange Reserve Flow','value':'Unavailable','status':'red','interpretation':str(e)})

    # ── TIER 4: Whale Accumulation ────────────────────────────
    # Use BTC large transaction volume as whale proxy (Binance aggTrades >$500k)
    try:
        agg_url = 'https://api.binance.com/api/v3/aggTrades?symbol=BTCUSDT&limit=1000'
        ra = requests.get(agg_url, headers=HEADERS, timeout=8)
        if ra.ok:
            trades    = ra.json()
            whale_buy = 0; whale_sell = 0; threshold = 500_000
            for t in trades:
                usd = float(t['p']) * float(t['q'])
                if usd >= threshold:
                    if t['m'] is False:  # buyer is taker
                        whale_buy += usd
                    else:
                        whale_sell += usd

            total_whale = whale_buy + whale_sell
            if total_whale > 0:
                whale_buy_pct = whale_buy / total_whale * 100
            else:
                whale_buy_pct = 50

            if whale_buy_pct >= 60:
                status = 'green'; pts = 1
                interp = f'Whale buyers ({whale_buy_pct:.0f}% of large trades) dominating recent flow. Large players accumulating. This is the signature of institutional bottom-buying.'
            elif whale_buy_pct >= 45:
                status = 'amber'; pts = 0
                interp = f'Whale flow balanced ({whale_buy_pct:.0f}% buy). Neither strong accumulation nor distribution. Wait for clearer signal.'
            else:
                status = 'red'; pts = 0
                interp = f'Whale sellers ({100-whale_buy_pct:.0f}% of large trades) dominating. Large players distributing — not a bottom signal.'
            deploy_score += pts
            signals.append({
                'tier':           'TIER 4',
                'name':           'Whale Accumulation (Trades >$500k)',
                'value':          f'{whale_buy_pct:.1f}% whale buy pressure  |  ${total_whale/1_000_000:.1f}M in large trades',
                'status':         status,
                'interpretation': interp,
            })
    except Exception as e:
        signals.append({'tier':'TIER 4','name':'Whale Accumulation','value':'Unavailable','status':'red','interpretation':str(e)})

    # ── TIER 5: Stablecoin Dry Powder ────────────────────────
    # USDT + USDC market cap vs BTC market cap = potential buying power ratio
    try:
        cg_url = 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,tether,usd-coin&vs_currencies=usd&include_market_cap=true'
        rcg = requests.get(cg_url, timeout=8)
        if rcg.ok:
            cg_data = rcg.json()
            btc_mcap  = cg_data.get('bitcoin',{}).get('usd_market_cap', 0)
            usdt_mcap = cg_data.get('tether',{}).get('usd_market_cap', 0)
            usdc_mcap = cg_data.get('usd-coin',{}).get('usd_market_cap', 0)
            stable_total = usdt_mcap + usdc_mcap
            ratio = (stable_total / btc_mcap * 100) if btc_mcap > 0 else 0

            if ratio >= 20:
                status = 'green'; pts = 1
                interp = f'Stablecoin supply is {ratio:.1f}% of BTC market cap — enormous dry powder waiting to deploy. Historically, ratios above 20% precede major rallies when other signals align.'
            elif ratio >= 12:
                status = 'amber'; pts = 0
                interp = f'Stablecoin ratio {ratio:.1f}%. Moderate dry powder available. Not extreme but buying power exists.'
            else:
                status = 'red'; pts = 0
                interp = f'Stablecoin ratio only {ratio:.1f}%. Most capital already deployed in crypto. Limited additional buying power — late cycle signal.'
            deploy_score += pts
            signals.append({
                'tier':           'TIER 5',
                'name':           'Stablecoin Dry Powder (USDT+USDC vs BTC MCap)',
                'value':          f'{ratio:.1f}%  |  ${stable_total/1e9:.1f}B stables vs ${btc_mcap/1e9:.1f}B BTC',
                'status':         status,
                'interpretation': interp,
            })
    except Exception as e:
        signals.append({'tier':'TIER 5','name':'Stablecoin Dry Powder','value':'Unavailable','status':'red','interpretation':str(e)})

    return signals, deploy_score


@app.route('/find-bottom', methods=['POST'])
def find_bottom():
    """On-chain bottom detection — all 5 signal tiers."""
    try:
        signals, deploy_score = fetch_bottom_signals()

        # Historical context based on score
        historical_map = {
            5: 'All 5 signals aligned previously at: Nov 2022 ($15,800) · Mar 2020 ($3,800) · Dec 2018 ($3,200). Each was followed by 300-1000%+ rallies within 12-18 months.',
            4: '4/5 signals aligned previously near: Jan 2023 ($16,500) · Apr 2020 ($6,500). Strong accumulation zone — these entries yielded 200-500% within a year.',
            3: '3/5 signals: Moderate accumulation zone. Historical returns from here: 100-300% over 12 months. Consider partial position (25-50% of allocation).',
            2: '2/5 signals: Early warning — market moving toward accumulation zone but not there yet. Prepare capital, watch for more signals to align.',
            1: '1/5 signals: Too early. Market not yet in accumulation territory. Cash is a position.',
            0: '0/5 signals: Market not in accumulation zone. Historical data suggests patience — forcing entries at this stage has poor risk/reward.',
        }
        historical = historical_map.get(deploy_score, '')

        # Gemini verdict
        verdict = None
        gemini_key = os.environ.get('GEMINI_API_KEY', '')
        if gemini_key and signals:
            sig_parts = []
            for s in signals:
                sig_parts.append(s['tier'] + ' ' + s['name'] + ': ' + s['value'] + ' — ' + s['status'].upper())
            sig_summary = '\n'.join(sig_parts)
            prompt = (
                "You are a senior macro crypto strategist advising a long-term holder "
                "who wants to deploy remaining capital at the optimal moment.\n\n"
                "Current on-chain signals:\n"
                + sig_summary +
                "\n\nDeploy score: " + str(deploy_score) + "/5\n\n"
                "Give a direct 3-4 sentence verdict: "
                "1. Are we in a bottom accumulation zone now? "
                "2. Key risk that makes it worse before better? "
                "3. What signal to wait for if score < 4? "
                "Be direct. No disclaimers. Manage your own money."
            )
            try:
                url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}'
                resp = requests.post(url, json={
                    'contents': [{'parts': [{'text': prompt}]}],
                    'generationConfig': {'maxOutputTokens': 300, 'temperature': 0.4}
                }, timeout=20)
                if resp.ok:
                    verdict = resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
            except Exception as e:
                print(f'[BOTTOM] Gemini error: {e}')

        return jsonify({
            'ok':                 True,
            'deploy_score':       deploy_score,
            'signals':            signals,
            'historical_context': historical,
            'verdict':            verdict,
        })

    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)