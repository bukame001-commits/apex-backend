import os
import time
import threading
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from youtube_transcript_api import YouTubeTranscriptApi
from anthropic import Anthropic

# ── Config ────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get('YOUTUBE_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.environ.get('YOUTUBE_CHAT_ID', '')
ANTHROPIC_API_KEY  = os.environ.get('ANTHROPIC_API_KEY', '')
CHECK_INTERVAL     = 6 * 3600  # check every 6 hours
LOOKBACK_HOURS     = 25        # fetch videos published in last 25 hours

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Channel list ──────────────────────────────────────────────
# Format: (display_name, youtube_handle_or_channel_id, language)
CHANNELS = [
    # English
    ('Coin Bureau',           'UCqK_GSMbpiV8spgD3ZGloSw', 'en'),
    ('Coin Bureau Trading',   'UCRvqjQPSeaWn-uEx-w0XLIg', 'en'),
    ('Credible Crypto',       'UCFkLiNTuZHFq6xTAVilFpyg', 'en'),
    ('DataDash',              'UCCatR7nWbYrkVXdxXb4cGXtA', 'en'),
    ('Michaël van de Poppe',  'UCdOcUKKU7fIZyIAmVoKU3cg', 'en'),
    ('Crypto Banter',         'UCN9Nj4tjXbVTLYWN0EKly_Q', 'en'),
    ('The Limiting Factor',   'UCTlBSoSGmxBSBP7jZnkuAGA', 'en'),
    ('Rayner Teo',            'UCh6SEPAmBMI8sPMTM8WrAPg', 'en'),
    ('Peter McCormack',       'UCIQLg9z8CTnDFboGR4BoOSg', 'en'),
    ('Swan Bitcoin',          'UCISdBpWNRIeGq4JxwNuYr6A', 'en'),
    ('Crypto Crush Show',     'CRYPTOCRUSHSHOW',            'en'),
    # Turkish
    ('Kripto Tugay',          'kriptugay',                  'tr'),
    ('Kripto Ofis',           'kriptoofis',                 'tr'),
    ('Selcoin',               'Selcoin',                    'tr'),
    ('Kriptolik',             'Kriptolik',                  'tr'),
    ('Crypto Mete',           'kriptomete',                 'tr'),
    ('JrKripto',              'JrKripto_Youtube',           'tr'),
    ('Tarik Bilen',           'tarikbilenn',                'tr'),
    ('Kanal Finans',          'KanalFinans',                'tr'),
    ('Smart Finance',         'AkıllıFinans',               'tr'),
    ('Turhan Bozkurt',        'TurhanBozkurt',              'tr'),
    ('Halil Recber',          'halilrecber4608',            'tr'),
    ('Integral Forextv',      'IntegralForexTV',            'tr'),
    ('TRADER MAN X',          'TRADERMANX',                 'tr'),
]

# Track already-processed video IDs to avoid duplicate summaries
_processed_videos = set()


# ── Telegram ──────────────────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print('[YT] Telegram not configured')
        return
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
        # Split into chunks if too long
        chunks = []
        while len(message) > 4000:
            chunks.append(message[:4000])
            message = message[4000:]
        chunks.append(message)
        for chunk in chunks:
            requests.post(url, json={
                'chat_id': TELEGRAM_CHAT_ID,
                'text': chunk,
                'parse_mode': 'HTML'
            }, timeout=10)
    except Exception as e:
        print(f'[YT] Telegram error: {e}')


# ── Fetch latest videos via RSS ───────────────────────────────
def get_channel_id_from_handle(handle):
    """Try to resolve a YouTube @handle to a channel ID via RSS."""
    # Try direct channel ID first (starts with UC)
    if handle.startswith('UC') and len(handle) == 24:
        return handle
    # Try handle-based RSS
    urls_to_try = [
        f'https://www.youtube.com/@{handle}',
        f'https://www.youtube.com/c/{handle}',
        f'https://www.youtube.com/user/{handle}',
    ]
    headers = {'User-Agent': 'Mozilla/5.0'}
    for url in urls_to_try:
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.ok and 'channel_id' in r.text:
                # Extract channel ID from page source
                idx = r.text.find('"channelId":"')
                if idx != -1:
                    start = idx + 13
                    end = r.text.find('"', start)
                    channel_id = r.text[start:end]
                    if channel_id.startswith('UC'):
                        return channel_id
                # Try alternate pattern
                idx2 = r.text.find('"externalId":"')
                if idx2 != -1:
                    start2 = idx2 + 14
                    end2 = r.text.find('"', start2)
                    channel_id2 = r.text[start2:end2]
                    if channel_id2.startswith('UC'):
                        return channel_id2
        except Exception:
            continue
    return None


def fetch_recent_videos(channel_id, lookback_hours=25):
    """Fetch videos published in the last N hours via YouTube RSS feed."""
    rss_url = f'https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}'
    try:
        r = requests.get(rss_url, timeout=10)
        if not r.ok:
            return []
        root = ET.fromstring(r.content)
        ns = {
            'atom': 'http://www.w3.org/2005/Atom',
            'yt':   'http://www.youtube.com/xml/schemas/2015',
            'media':'http://search.yahoo.com/mrss/'
        }
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        videos = []
        for entry in root.findall('atom:entry', ns):
            try:
                video_id  = entry.find('yt:videoId', ns).text
                title     = entry.find('atom:title', ns).text
                published = entry.find('atom:published', ns).text
                pub_dt    = datetime.fromisoformat(published.replace('Z', '+00:00'))
                if pub_dt >= cutoff:
                    videos.append({'id': video_id, 'title': title, 'published': pub_dt})
            except Exception:
                continue
        return videos
    except Exception as e:
        print(f'[YT] RSS fetch error for {channel_id}: {e}')
        return []


# ── Get transcript ────────────────────────────────────────────
def get_transcript(video_id, lang='en'):
    """Fetch transcript for a video. Returns text or None."""
    try:
        # Try requested language first, then auto-generated
        lang_codes = [lang, f'{lang}-auto', 'en', 'tr'] if lang == 'tr' else [lang, 'en']
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript = None
        for code in lang_codes:
            try:
                transcript = transcript_list.find_transcript([code])
                break
            except Exception:
                continue
        if not transcript:
            # Try any available transcript
            try:
                transcript = transcript_list.find_generated_transcript(['en', 'tr'])
            except Exception:
                transcript = next(iter(transcript_list), None)
        if not transcript:
            return None
        chunks = transcript.fetch()
        text = ' '.join([c['text'] for c in chunks])
        # Limit to ~8000 chars to keep token usage reasonable
        return text[:8000] if len(text) > 8000 else text
    except Exception as e:
        print(f'[YT] Transcript error for {video_id}: {e}')
        return None


# ── Summarize with Claude ──────────────────────────────────────
def summarize(channel_name, video_title, transcript, lang='en'):
    """Use Claude to summarize the transcript."""
    try:
        if lang == 'tr':
            prompt = f"""Sen bir kripto ve finans içerik analistisin.

Kanal: {channel_name}
Video başlığı: {video_title}

Transkript:
{transcript}

Lütfen aşağıdaki formatta Türkçe özet yap:

📊 GENEL GÖRÜŞ: (Boğa/Ayı/Nötr ve kısa açıklama)
💰 BAHSEDİLEN VARLIKLAR: (Ticker sembolleri ve yorum - örn: BTC - güçlü destek)
🎯 FİYAT HEDEFLERİ: (Varsa belirt, yoksa "Belirtilmedi")
⚠️ RİSKLER: (Bahsedilen riskler)
💡 ANA MESAJ: (1-2 cümle özet)

Sadece önemli bilgileri yaz, gereksiz detay ekleme."""
        else:
            prompt = f"""You are a crypto and finance content analyst.

Channel: {channel_name}
Video title: {video_title}

Transcript:
{transcript}

Provide a concise summary in this exact format:

📊 OVERALL SENTIMENT: (Bullish/Bearish/Neutral + brief reason)
💰 ASSETS MENTIONED: (Ticker symbols with commentary - e.g. BTC - strong support)
🎯 PRICE TARGETS: (If mentioned, otherwise "None mentioned")
⚠️ RISKS: (Key risks mentioned)
💡 KEY TAKEAWAY: (1-2 sentence summary)

Be concise. Only include significant information."""

        response = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=600,
            messages=[{'role': 'user', 'content': prompt}]
        )
        return response.content[0].text
    except Exception as e:
        print(f'[YT] Claude summarize error: {e}')
        return None


# ── Main scan loop ────────────────────────────────────────────
def run_youtube_scan():
    print(f'[YT] Starting YouTube scan for {len(CHANNELS)} channels...')
    summaries_sent = 0

    for name, handle, lang in CHANNELS:
        try:
            # Resolve channel ID
            if handle.startswith('UC') and len(handle) == 24:
                channel_id = handle
            else:
                channel_id = get_channel_id_from_handle(handle)
                if not channel_id:
                    print(f'[YT] Could not resolve channel ID for {name} (@{handle})')
                    continue

            # Fetch videos and take only the latest one
            videos = fetch_recent_videos(channel_id, lookback_hours=24*90)  # 90 days to ensure we find something
            if not videos:
                print(f'[YT] No videos found for {name}')
                continue

            # Sort newest first, take only the most recent
            videos.sort(key=lambda v: v['published'], reverse=True)
            video = videos[0]
            vid_id = video['id']

            if vid_id in _processed_videos:
                print(f'[YT] Already sent latest for {name} — skipping')
                continue

            print(f'[YT] Processing latest: {name} — {video["title"]}')

            # Get transcript
            transcript = get_transcript(vid_id, lang)
            if not transcript:
                print(f'[YT] No transcript for {vid_id} — skipping')
                _processed_videos.add(vid_id)
                continue

            # Summarize
            summary = summarize(name, video['title'], transcript, lang)
            if not summary:
                continue

            # Format and send to Telegram
            pub_str = video['published'].strftime('%d %b %Y %H:%M UTC')
            flag = '🇹🇷' if lang == 'tr' else '🇬🇧'
            message = (
                f'{flag} <b>{name}</b>\n'
                f'📺 {video["title"]}\n'
                f'🕐 {pub_str}\n'
                f'🔗 https://youtube.com/watch?v={vid_id}\n\n'
                f'{summary}'
            )
            send_telegram(message)
            _processed_videos.add(vid_id)
            summaries_sent += 1
            time.sleep(2)  # small delay between messages

        except Exception as e:
            print(f'[YT] Error processing {name}: {e}')
            continue

    print(f'[YT] Scan complete — {summaries_sent} summaries sent')


def youtube_monitor_loop():
    """Background thread — runs forever, scanning every CHECK_INTERVAL seconds."""
    print(f'[YT] YouTube monitor started — checking every {CHECK_INTERVAL//3600} hours')
    # Wait 2 minutes after startup before first scan
    time.sleep(120)
    while True:
        try:
            run_youtube_scan()
        except Exception as e:
            print(f'[YT] Monitor error: {e}')
        time.sleep(CHECK_INTERVAL)


# ── Start thread (called from app.py) ────────────────────────
def start_youtube_monitor():
    t = threading.Thread(target=youtube_monitor_loop, daemon=True)
    t.start()
    print('[YT] YouTube monitor thread started')


if __name__ == '__main__':
    # For testing — run once immediately
    run_youtube_scan()
