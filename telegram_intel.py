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
    try:
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession
        with _client_lock:
            _tg_client = TelegramClient(StringSession(), int(api_id), api_hash)
            _tg_client.connect()
            result     = _tg_client.send_code_request(phone)
            _phone_hash = result.phone_code_hash
        print(f'[TG-INTEL] Code sent to {phone}')
        return {'ok': True}
    except Exception as e:
        print(f'[TG-INTEL] send_code error: {e}')
        return {'ok': False, 'error': str(e)}


# ── Auth Step 2: Verify code ──────────────────────────────────
def auth_verify_code(phone, code, api_id, api_hash):
    global _tg_client, _phone_hash
    try:
        with _client_lock:
            if not _tg_client:
                return {'ok': False, 'error': 'Session expired — resend code'}
            try:
                _tg_client.sign_in(phone=phone, code=code, phone_code_hash=_phone_hash)
            except Exception as e:
                if 'Two-steps' in str(e) or '2FA' in str(e) or 'password' in str(e).lower():
                    return {'ok': False, 'needs_2fa': True, 'error': '2FA required'}
                raise
            session_str = _tg_client.session.save()
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
    try:
        with _client_lock:
            if not _tg_client:
                return {'ok': False, 'error': 'Session expired — resend code'}
            _tg_client.sign_in(password=password)
            session_str = _tg_client.session.save()
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
    from datetime import datetime, timezone, timedelta
    session = os.environ.get('TELEGRAM_SESSION', '')
    api_id  = os.environ.get('TELEGRAM_API_ID', '')
    api_hash = os.environ.get('TELEGRAM_API_HASH', '')
    if not session:
        return [], 'Not authenticated'
    try:
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        return [], 'telethon not installed — run: pip install telethon'

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    posts  = []
    try:
        client = TelegramClient(StringSession(session), int(api_id), api_hash)
        client.connect()
        for name, cid in channels:
            try:
                try:
                    cid_val = int(cid)
                except (ValueError, TypeError):
                    cid_val = cid
                msgs  = client.get_messages(cid_val, limit=limit)
                count = 0
                for msg in msgs:
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
        client.disconnect()
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
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return None, 'ANTHROPIC_API_KEY not set'

    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key':         api_key,
                'anthropic-version': '2023-06-01',
                'content-type':      'application/json',
            },
            json={
                'model':      'claude-sonnet-4-20250514',
                'max_tokens': 4000,
                'messages':   [{'role': 'user', 'content': prompt}]
            },
            timeout=90
        )
        if not resp.ok:
            return None, f'Claude API error {resp.status_code}: {resp.text[:200]}'
        report = resp.json()['content'][0]['text']
    except Exception as e:
        return None, f'Claude call failed: {e}'

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
        f'🧠 <b>APEX INTELLIGENCE REPORT</b>\n'
        f'⏰ {now}\n'
        f'📡 {channel_count} channels · {post_count} posts analyzed\n\n'
    )
    full   = header + report
    url    = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    chunks = []
    while len(full) > 4000:
        split = full[:4000].rfind('\n')
        if split < 3000:
            split = 4000
        chunks.append(full[:split])
        full  = full[split:]
    chunks.append(full)
    for chunk in chunks:
        try:
            requests.post(url, json={
                'chat_id': chat_id, 'text': chunk, 'parse_mode': 'HTML'
            }, timeout=10)
            time.sleep(0.5)
        except Exception as e:
            print(f'[TG-INTEL] Send error: {e}')
    print(f'[TG-INTEL] Report sent ({len(chunks)} messages)')
