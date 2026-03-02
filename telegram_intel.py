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
            mfi_s  = f' | MFI:{mfi}' if mfi else ''
            vol_block += f"  {sym} | {ratio}x vol{mfi_s} | Conviction:{score} | [{labels}]\n"

    ch_names = ', '.join(n for n, _ in channels)

    return f"""You are a senior crypto trading strategist. Today is {now}.

You have THREE independent data sources. Cross-reference them to find HIGH-CONVICTION setups where multiple sources align.

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
Filters: 2x spike · MFI · CVROC · MavilimW · OI · Funding
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{vol_block}

ANALYSIS TASKS:

1. CHANNEL INTELLIGENCE SUMMARY
   - What coins/themes are creators focused on?
   - Overall sentiment per channel and combined
   - Specific setups, entry targets, or warnings mentioned
   - Agreements and conflicts between channels

2. TRIPLE CONFLUENCE (highest conviction — all 3 sources agree)
   For each coin: why it qualifies from each source, entry zone, stop, target, R:R ratio

3. DOUBLE CONFLUENCE (2 of 3 sources agree)
   Same format, labelled which 2 sources

4. DIVERGENCES & RED FLAGS
   - Creators bullish but technicals say otherwise
   - Volume spike but zero creator attention (hidden move?)
   - Strong conflicts between sources

5. MARKET CONTEXT
   - Macro narrative from channels vs what technicals show
   - Alignment or disconnect

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

    prompt  = _build_prompt(posts, _last_apex_alerts, _last_volume_spikes, channels)
    gemini_key = os.environ.get('GEMINI_API_KEY', '')
    if not gemini_key:
        return None, 'GEMINI_API_KEY not set'

    try:
        gemini_url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}'
        resp = requests.post(gemini_url, json={
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {'maxOutputTokens': 4000, 'temperature': 0.7}
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
