"""
APEX SCANNER — Telegram Intelligence Analyzer
==============================================
Fetches posts from configured Telegram channels, analyzes with Claude,
cross-references with APEX Scanner + Volume Spike results,
generates a unified strategy and posts to Telegram bot.

Required Railway env vars:
    TELEGRAM_API_ID       — from my.telegram.org
    TELEGRAM_API_HASH     — from my.telegram.org
    TELEGRAM_SESSION      — auto-saved after phone auth
    ANTHROPIC_API_KEY     — for Claude analysis
    TELEGRAM_BOT_TOKEN    — scan results bot (for output)
    TELEGRAM_CHAT_ID      — your chat ID
    RAILWAY_TOKEN         — named Telegram_data_fetch in Railway
    RAILWAY_SERVICE_ID    — Railway service ID
    RAILWAY_PROJECT_ID    — Railway project ID
"""

import os
import re
import time
import threading
import requests

# ── In-memory auth state (lives for duration of auth flow) ───
_tg_client  = None
_phone_hash = None
_client_lock = threading.Lock()

# ── Shared state injected from app.py after each scan ────────
_last_apex_alerts   = []
_last_volume_spikes = []


def set_apex_alerts(alerts):
    global _last_apex_alerts
    _last_apex_alerts = list(alerts or [])


def set_volume_spikes(spikes):
    global _last_volume_spikes
    _last_volume_spikes = list(spikes or [])


_last_nupl = None

# On-chain metrics extracted from Telegram posts (Blitztrading / Kripto Messi)
_last_onchain = {
    'mpi':              None,   # Miners Position Index
    'cdd':              None,   # Coin Days Destroyed
    'whale_ratio':      None,   # Exchange Whale Ratio (spot)
    'stablecoin_txns':  None,   # Stablecoin deposit txns to derivative exchanges
    'nupl_text':        None,   # NUPL value mentioned in posts
    'sopr_text':        None,   # SOPR value mentioned in posts
    'lth_realized':     None,   # LTH Realized Price mentioned in posts
    'mvrv':             None,   # MVRV Z-Score mentioned in posts
    'raw_alerts':       [],     # Raw CryptoQuant alert texts
}

def set_nupl(nupl):
    global _last_nupl
    _last_nupl = nupl

def get_last_onchain():
    """Return last parsed on-chain metrics for use by /find-bottom endpoint."""
    return dict(_last_onchain)


def is_authenticated():
    return bool(os.environ.get('TELEGRAM_SESSION', ''))


# ── Railway API helpers ───────────────────────────────────────
def _railway_env_id(token, project_id):
    query = '''query GetEnvs($pid: String!) {
      project(id: $pid) {
        environments { edges { node { id name } } }
      }
    }'''
    try:
        r = requests.post(
            'https://backboard.railway.app/graphql/v2',
            json={'query': query, 'variables': {'pid': project_id}},
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            timeout=10
        )
        if r.ok:
            edges = r.json()['data']['project']['environments']['edges']
            for e in edges:
                if e['node']['name'].lower() == 'production':
                    return e['node']['id']
            if edges:
                return edges[0]['node']['id']
    except Exception as e:
        print(f'[TG-INTEL] Railway env_id error: {e}')
    return ''


def _save_to_railway(key, value):
    token      = os.environ.get('RAILWAY_TOKEN', '')
    service_id = os.environ.get('RAILWAY_SERVICE_ID', '')
    project_id = os.environ.get('RAILWAY_PROJECT_ID', '')
    if not token or not service_id or not project_id:
        print(f'[TG-INTEL] Railway creds missing — cannot save {key}')
        os.environ[key] = value
        return False
    env_id = _railway_env_id(token, project_id)
    if not env_id:
        print(f'[TG-INTEL] Could not get Railway env ID')
        os.environ[key] = value
        return False
    mutation = '''mutation Upsert($input: VariableCollectionUpsertInput!) {
      variableCollectionUpsert(input: $input)
    }'''
    payload = {
        'query': mutation,
        'variables': {
            'input': {
                'projectId':     project_id,
                'serviceId':     service_id,
                'environmentId': env_id,
                'variables':     {key: value}
            }
        }
    }
    try:
        r = requests.post(
            'https://backboard.railway.app/graphql/v2',
            json=payload,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            timeout=10
        )
        if r.ok and 'errors' not in r.json():
            os.environ[key] = value
            print(f'[TG-INTEL] Saved {key} to Railway ✓')
            return True
        else:
            print(f'[TG-INTEL] Railway save failed: {r.text[:200]}')
            os.environ[key] = value
    except Exception as e:
        print(f'[TG-INTEL] Railway save error: {e}')
        os.environ[key] = value
    return False


# ── Auth Step 1: Send code to phone ──────────────────────────
def auth_send_code(api_id, api_hash, phone):
    global _tg_client, _phone_hash
    import asyncio
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        async def _send():
            global _tg_client, _phone_hash
            client = TelegramClient(StringSession(), int(api_id), api_hash)
            await client.connect()
            result      = await client.send_code_request(phone)
            _tg_client  = client
            _phone_hash = result.phone_code_hash

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_send())
        # Don't close loop — client needs it for verify step
        print(f'[TG-INTEL] Code sent to {phone}')
        return {'ok': True}
    except Exception as e:
        print(f'[TG-INTEL] send_code error: {e}')
        return {'ok': False, 'error': str(e)}


# ── Auth Step 2: Verify code ──────────────────────────────────
def auth_verify_code(phone, code, api_id, api_hash):
    global _tg_client, _phone_hash
    import asyncio
    try:
        async def _verify():
            global _tg_client
            if not _tg_client:
                raise Exception('Session expired — resend code')
            try:
                await _tg_client.sign_in(phone=phone, code=code, phone_code_hash=_phone_hash)
            except Exception as e:
                if 'Two-steps' in str(e) or '2FA' in str(e) or 'password' in str(e).lower():
                    raise Exception('__2FA__')
                raise
            return _tg_client.session.save()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            session_str = loop.run_until_complete(_verify())
        except Exception as e:
            if '__2FA__' in str(e):
                return {'ok': False, 'needs_2fa': True, 'error': '2FA required'}
            raise
        finally:
            loop.close()

        _save_to_railway('TELEGRAM_SESSION',  session_str)
        _save_to_railway('TELEGRAM_API_ID',   str(api_id))
        _save_to_railway('TELEGRAM_API_HASH', api_hash)
        print('[TG-INTEL] Auth complete ✓')
        return {'ok': True}
    except Exception as e:
        print(f'[TG-INTEL] verify_code error: {e}')
        return {'ok': False, 'error': str(e)}


# ── Auth Step 2b: 2FA password ────────────────────────────────
def auth_verify_2fa(password, api_id, api_hash):
    global _tg_client
    import asyncio
    try:
        async def _2fa():
            if not _tg_client:
                raise Exception('Session expired — resend code')
            await _tg_client.sign_in(password=password)
            return _tg_client.session.save()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        session_str = loop.run_until_complete(_2fa())
        loop.close()
        _save_to_railway('TELEGRAM_SESSION',  session_str)
        _save_to_railway('TELEGRAM_API_ID',   str(api_id))
        _save_to_railway('TELEGRAM_API_HASH', api_hash)
        print('[TG-INTEL] 2FA auth complete ✓')
        return {'ok': True}
    except Exception as e:
        print(f'[TG-INTEL] 2FA error: {e}')
        return {'ok': False, 'error': str(e)}


# ── Fetch posts from channels ─────────────────────────────────
def fetch_posts(channels, lookback_hours=24, limit=25):
    """Fetch posts using asyncio in a fresh event loop — avoids gunicorn conflicts."""
    import asyncio
    from datetime import datetime, timezone, timedelta

    session  = os.environ.get('TELEGRAM_SESSION', '')
    api_id   = os.environ.get('TELEGRAM_API_ID', '')
    api_hash = os.environ.get('TELEGRAM_API_HASH', '')

    if not session:
        return [], 'Not authenticated'

    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        return [], 'telethon not installed'

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    posts  = []

    async def _fetch():
        client = TelegramClient(StringSession(session), int(api_id), api_hash)
        await client.connect()
        try:
            for name, cid in channels:
                try:
                    try:
                        cid_val = int(cid)
                    except (ValueError, TypeError):
                        cid_val = str(cid).replace('t.me/', '@').strip()
                    count = 0
                    async for msg in client.iter_messages(cid_val, limit=limit):
                        if not msg.text or not msg.text.strip():
                            continue
                        d = msg.date
                        if d.tzinfo is None:
                            d = d.replace(tzinfo=timezone.utc)
                        if d < cutoff:
                            continue
                        posts.append({
                            'channel': name,
                            'text':    msg.text.strip()[:600],
                            'date':    d.strftime('%b %d %H:%M UTC'),
                        })
                        count += 1
                    print(f'[TG-INTEL] {name}: {count} posts')
                except Exception as e:
                    print(f'[TG-INTEL] {name} failed: {e}')
        finally:
            await client.disconnect()

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_fetch())
        loop.close()
    except Exception as e:
        return [], f'Fetch error: {e}'

    print(f'[TG-INTEL] Total posts: {len(posts)}')
    return posts, None


# ── Build Claude prompt ───────────────────────────────────────

def _parse_onchain_from_posts(posts):
    """
    Extract CryptoQuant alert values from Blitztrading posts and on-chain
    metrics mentioned by Kripto Messi / other analysts.
    Updates _last_onchain in-place.
    """
    global _last_onchain
    raw_alerts = []
    mpi_vals = []; cdd_vals = []; whale_vals = []; stable_txn_vals = []
    nupl_vals = []; sopr_vals = []; lth_vals = []; mvrv_vals = []

    for p in posts:
        text    = p.get('text', '')
        channel = p.get('channel', '')

        # Collect raw Blitztrading CryptoQuant alerts
        if any(kw in text for kw in ['Current Value', 'Exchange Depositing', "Miners'", 'Coin Days', 'Whale Ratio']):
            raw_alerts.append(f"[{channel}] {text[:300]}")

        # MPI — "Miners' Position Index (MPI): 2.957..."
        m = re.search(r"Miners'?\s*Position\s*Index.*?:\s*([\d.]+)", text, re.IGNORECASE | re.DOTALL)
        if m:
            try: mpi_vals.append(float(m.group(1)))
            except: pass

        # CDD — "Coin Days Destroyed: 4,589,176.35"
        m = re.search(r"Coin\s*Days\s*Destroyed.*?:\s*([\d,]+\.?\d*)", text, re.IGNORECASE | re.DOTALL)
        if m:
            try: cdd_vals.append(float(m.group(1).replace(',', '')))
            except: pass

        # Exchange Whale Ratio — "Exchange Whale Ratio: 0.8297..."
        m = re.search(r"Exchange\s*Whale\s*Ratio.*?:\s*([\d.]+)", text, re.IGNORECASE | re.DOTALL)
        if m:
            try: whale_vals.append(float(m.group(1)))
            except: pass

        # Stablecoin depositing transactions — "Exchange Depositing Transactions: 863"
        m = re.search(r"Exchange\s*Depositing\s*Transactions.*?:\s*([\d,]+)", text, re.IGNORECASE | re.DOTALL)
        if m:
            try: stable_txn_vals.append(int(m.group(1).replace(',', '')))
            except: pass

        # NUPL — "NUPL: 0.42" or "NUPL 0.38"
        m = re.search(r"NUPL[:\s]+([+-]?[\d.]+)", text, re.IGNORECASE)
        if m:
            try: nupl_vals.append(float(m.group(1)))
            except: pass

        # SOPR — "SOPR: 0.98"
        m = re.search(r"\bSOPR[:\s]+([+-]?[\d.]+)", text, re.IGNORECASE)
        if m:
            try: sopr_vals.append(float(m.group(1)))
            except: pass

        # LTH Realized Price — "LTH Realized Price: $72,400"
        m = re.search(r"LTH\s*Realized\s*Price[:\s$]+([\d,k.]+)", text, re.IGNORECASE)
        if m:
            try:
                raw = m.group(1).replace(',', '').replace('k', '000')
                lth_vals.append(float(raw))
            except: pass

        # MVRV Z-Score — "MVRV Z-Score: 2.4"
        m = re.search(r"MVRV\s*Z.?Score[:\s]+([+-]?[\d.]+)", text, re.IGNORECASE)
        if m:
            try: mvrv_vals.append(float(m.group(1)))
            except: pass

    _last_onchain['raw_alerts']      = raw_alerts[-10:]
    _last_onchain['mpi']             = mpi_vals[-1]          if mpi_vals        else None
    _last_onchain['cdd']             = cdd_vals[-1]          if cdd_vals        else None
    _last_onchain['whale_ratio']     = max(whale_vals)       if whale_vals      else None
    _last_onchain['stablecoin_txns'] = max(stable_txn_vals)  if stable_txn_vals else None
    _last_onchain['nupl_text']       = nupl_vals[-1]         if nupl_vals       else None
    _last_onchain['sopr_text']       = sopr_vals[-1]         if sopr_vals       else None
    _last_onchain['lth_realized']    = lth_vals[-1]          if lth_vals        else None
    _last_onchain['mvrv']            = mvrv_vals[-1]         if mvrv_vals       else None

    found = [k for k, v in _last_onchain.items() if v and k != 'raw_alerts']
    print(f'[TG-INTEL] On-chain parsed from posts: {found}')


def _build_prompt(posts, apex_alerts, volume_spikes, channels):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    posts_block = ''
    for p in posts[:50]:
        posts_block += f"[{p['channel']} — {p['date']}]\n{p['text']}\n\n"
    if not posts_block:
        posts_block = 'No posts fetched.\n'

    apex_block = 'No recent APEX scan data.\n'
    if apex_alerts:
        apex_block = ''
        for a in apex_alerts[:25]:
            sym  = a.get('symbol', '?')
            sc   = a.get('score', '?')
            rsi  = a.get('rsi', '?')
            wt1  = a.get('wt1', '?')
            sigs = ', '.join(a.get('signals', []))
            vs   = a.get('vsEma200', '')
            vs_s = f' | vsEMA200:{vs}%' if vs != '' else ''
            apex_block += f"  {sym} | Score:{sc} | RSI:{rsi} | WT1:{wt1}{vs_s} | [{sigs}]\n"

    vol_block = 'No recent volume spike alerts.\n'
    if volume_spikes:
        vol_block = ''
        for v in volume_spikes[:10]:
            sym    = v.get('sym', '?')
            ratio  = v.get('spike', '?')
            mfi    = v.get('mfi', '')
            score  = v.get('score', '')
            labels = ', '.join(v.get('labels', []))
            taker  = v.get('taker_pct', None)
            mfi_s  = f' | MFI:{mfi}' if mfi else ''
            taker_s = f' | Buyers:{taker}%' if taker else ''
            vol_block += f"  {sym} | {ratio}x vol{mfi_s}{taker_s} | Conviction:{score} | [{labels}]\n"

    # NUPL — prefer value extracted from posts, fall back to proxy
    nupl = _last_onchain.get('nupl_text') or _last_nupl
    if nupl is not None:
        if nupl >= 0.75:
            nupl_context = f"BTC NUPL: {nupl:.2f} — EUPHORIA ZONE. Long-term holders are deeply in profit and historically begin distributing. High risk of major correction."
        elif nupl >= 0.5:
            nupl_context = f"BTC NUPL: {nupl:.2f} — BELIEF ZONE. Market trending up, holders in profit but not yet extreme. Favorable for continuation."
        elif nupl >= 0.25:
            nupl_context = f"BTC NUPL: {nupl:.2f} — OPTIMISM ZONE. Moderate profits across the market. Neutral-to-bullish."
        elif nupl >= 0:
            nupl_context = f"BTC NUPL: {nupl:.2f} — HOPE/FEAR ZONE. Market near breakeven. Long-term holders starting to feel pain. Historically good accumulation zone."
        else:
            nupl_context = f"BTC NUPL: {nupl:.2f} — CAPITULATION ZONE. Long-term holders are selling at a loss. This is historically the best accumulation zone but extreme caution needed as price may fall further."
    else:
        nupl_context = "BTC NUPL: Not available"


    # ── On-chain block from Blitztrading / Kripto Messi posts ────────────────
    oc = _last_onchain
    onchain_lines = []

    if oc['mpi'] is not None:
        interp = "HIGH — miners selling well above average. Local top / miner stress signal." if oc['mpi'] >= 2 else "Normal miner outflow."
        onchain_lines.append(f"  MPI (Miners Position Index): {oc['mpi']:.4f} — {interp}")

    if oc['cdd'] is not None:
        interp = "ELEVATED — old coins moving. Long-term holders distributing into strength." if oc['cdd'] >= 2_000_000 else "Normal coin age movement."
        onchain_lines.append(f"  CDD (Coin Days Destroyed): {oc['cdd']:,.0f} — {interp}")

    if oc['whale_ratio'] is not None:
        interp = "HIGH — whales dominating spot inflows. Large players sending BTC to exchanges = preparing to sell." if oc['whale_ratio'] >= 0.8 else "Normal whale activity."
        onchain_lines.append(f"  Exchange Whale Ratio (Spot): {oc['whale_ratio']:.4f} — {interp}")

    if oc['stablecoin_txns'] is not None:
        interp = "VERY HIGH — massive stablecoin inflow to derivatives. Large leveraged position being built." if oc['stablecoin_txns'] >= 500 else "Elevated — stablecoins flowing into derivatives." if oc['stablecoin_txns'] >= 200 else "Normal."
        onchain_lines.append(f"  Stablecoin Deposit Txns (Deriv. Exchanges): {oc['stablecoin_txns']:,} — {interp}")

    if oc['sopr_text'] is not None:
        interp = "Below 1 — coins moved at LOSS. Capitulation signal / potential bottom." if oc['sopr_text'] < 1 else "Above 1 — coins moved at profit. Distribution phase."
        onchain_lines.append(f"  SOPR: {oc['sopr_text']:.4f} — {interp}")

    if oc['lth_realized'] is not None:
        onchain_lines.append(f"  LTH Realized Price: ${oc['lth_realized']:,.0f} — key level: price below this = LTH holders underwater.")

    if oc['mvrv'] is not None:
        interp = "OVERHEATED (>7 = cycle top zone)" if oc['mvrv'] > 7 else "UNDERVALUED (<1 = cycle bottom zone)" if oc['mvrv'] < 1 else "Neutral range."
        onchain_lines.append(f"  MVRV Z-Score: {oc['mvrv']:.2f} — {interp}")

    if oc['raw_alerts']:
        onchain_lines.append("\n  Raw CryptoQuant alerts from Blitztrading (last 24h):")
        for alert in oc['raw_alerts'][:5]:
            onchain_lines.append(f"    {alert[:200]}")

    onchain_block = '\n'.join(onchain_lines) if onchain_lines else "No on-chain metrics extracted from posts this period."

    ch_names = ', '.join(n for n, _ in channels)

    return f"""You are a senior crypto trading strategist. Today is {now}.

You have FOUR independent data sources. Cross-reference them to find HIGH-CONVICTION setups where multiple sources align.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOURCE 1 — TELEGRAM CHANNEL INTELLIGENCE (last 24h)
Channels: {ch_names}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{posts_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOURCE 2 — APEX SCANNER TECHNICAL SIGNALS
Indicators: WaveTrend, RSI Combo, MACD, Bollinger, EMA200, StochRSI, VWAP, OBV, ATR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{apex_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOURCE 3 — VOLUME ANOMALY ALERTS (1H, Kıvanç methodology)
Filters: 2x spike · MFI · CVROC · MavilimW · OI · Funding · Taker Buy Ratio
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{vol_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOURCE 4 — ON-CHAIN INTELLIGENCE (CryptoQuant alerts parsed from Blitztrading / Kripto Messi)
Real institutional-grade on-chain metrics extracted directly from channel posts.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{onchain_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MARKET CONTEXT — BITCOIN NUPL (on-chain sentiment)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{nupl_context}

ANALYSIS TASKS:

1. CHANNEL INTELLIGENCE SUMMARY
   - What coins/themes are creators focused on?
   - Overall sentiment per channel and combined
   - Specific setups, entry targets, or warnings mentioned
   - Agreements and conflicts between channels

2. ON-CHAIN MARKET HEALTH (Source 4)
   - What does the combined MPI + CDD + Whale Ratio + Stablecoin flow tell us?
   - Are we in accumulation or distribution phase right now?
   - If SOPR/LTH Realized Price/MVRV available: what cycle phase does it suggest?
   - On-chain verdict: bullish / bearish / neutral for BTC

3. TRIPLE CONFLUENCE (highest conviction — 3+ sources agree)
   For each coin: why it qualifies from each source, entry zone, stop, target, R:R ratio

4. DOUBLE CONFLUENCE (2 of 4 sources agree)
   Same format, labelled which 2 sources

5. DIVERGENCES & RED FLAGS
   - On-chain bearish (high whale ratio + CDD) but channels bullish?
   - Volume spike bullish but whale ratio says distribution?
   - CDD + MPI both elevated = confirmed distribution
   - Volume spike with zero creator attention (hidden move?)

6. PRIORITY ACTION LIST
   Ranked: what to enter now, what to watch, what to avoid.
   Use real coin names and numbers. No disclaimers."""


# ── Run full analysis pipeline ────────────────────────────────
def run_analysis(channels, lookback_hours=24):
    print(f'[TG-INTEL] Starting analysis — {len(channels)} channels...')

    posts, err = fetch_posts(channels, lookback_hours=lookback_hours)
    if err:
        return None, err
    if not posts:
        return None, 'No posts found in the last 24h'

    # Parse on-chain metrics from posts before building prompt
    _parse_onchain_from_posts(posts)

    prompt  = _build_prompt(posts, _last_apex_alerts, _last_volume_spikes, channels)
    gemini_key = os.environ.get('GEMINI_API_KEY', '')
    if not gemini_key:
        return None, 'GEMINI_API_KEY not set'

    try:
        gemini_url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}'
        resp = requests.post(gemini_url, json={
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {'maxOutputTokens': 8000, 'temperature': 0.7}
        }, timeout=90)
        if not resp.ok:
            return None, f'Gemini API error {resp.status_code}: {resp.text[:200]}'
        report = resp.json()['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        return None, f'Gemini call failed: {e}'

    _post_to_telegram(report, len(posts), len(channels))
    return report, None


def _post_to_telegram(report, post_count, channel_count):
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    chat_id   = os.environ.get('TELEGRAM_CHAT_ID', '')
    if not bot_token or not chat_id:
        return
    from datetime import datetime, timezone
    now    = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    header = (
        f'🧠 APEX INTELLIGENCE REPORT\n'
        f'⏰ {now}\n'
        f'📡 {channel_count} channels · {post_count} posts analyzed\n\n'
    )
    # Strip markdown formatting that breaks Telegram
    import re
    clean = report
    clean = re.sub(r'\*\*(.+?)\*\*', r'\1', clean)   # **bold** → plain
    clean = re.sub(r'\*(.+?)\*',     r'\1', clean)   # *italic* → plain
    clean = re.sub(r'^#{1,3}\s+',    '',    clean, flags=re.MULTILINE)  # ### headers
    clean = re.sub(r'`(.+?)`',       r'\1', clean)   # `code` → plain

    full = header + clean
    url  = f'https://api.telegram.org/bot{bot_token}/sendMessage'

    # Split into safe 3800-char chunks at newline boundaries
    chunks = []
    while len(full) > 3800:
        split = full[:3800].rfind('\n')
        if split < 2000:
            split = 3800
        chunks.append(full[:split].strip())
        full = full[split:].strip()
    if full:
        chunks.append(full)

    print(f'[TG-INTEL] Sending report in {len(chunks)} chunks...')
    for i, chunk in enumerate(chunks):
        try:
            r = requests.post(url, json={
                'chat_id': chat_id,
                'text':    chunk,
            }, timeout=10)
            if not r.ok:
                print(f'[TG-INTEL] Chunk {i+1} failed: {r.text[:100]}')
            time.sleep(0.8)
        except Exception as e:
            print(f'[TG-INTEL] Send error chunk {i+1}: {e}')
    print(f'[TG-INTEL] Report sent ({len(chunks)} messages)')
