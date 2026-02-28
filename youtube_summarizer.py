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
CHECK_INTERVAL     = 6 * 3600

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# All channel IDs hardcoded — no scraping needed
CHANNELS = [
    # English
    ('Coin Bureau',          'UCqK_GSMbpiV8spgD3ZGloSw', 'en'),
    ('Coin Bureau Trading',  'UCRvqjQPSeaWn-uEx-w0XLIg', 'en'),
    ('Credible Crypto',      'UCFSn-h8wTnhpKJMteN76Abg', 'en'),
    ('DataDash',             'UCCatR7nWbYrkVXdxXb4cGXtA', 'en'),
    ('Michaël van de Poppe', 'UCdOcUKKU7fIZyIAmVoKU3cg', 'en'),
    ('Crypto Banter',        'UCN9Nj4tjXbVTLYWN0EKly_Q', 'en'),
    ('The Limiting Factor',  'UCTlBSoSGmxBSBP7jZnkuAGA', 'en'),
    ('Rayner Teo',           'UCh6SEPAmBMI8sPMTM8WrAPg', 'en'),
    ('Peter McCormack',      'UCzrWKkFIRS0kjZf7x24GdGg', 'en'),
    ('Swan Bitcoin',         'Swan_Bitcoin', 'en'),
    ('Crypto Crush Show',    'CRYPTOCRUSHSHOW', 'en'),
    # Turkish
    ('Kripto Tugay',         'UC5vFx-5UrwQSMBo2jBVRoZA', 'tr'),
    ('Kripto Ofis',          'UCaYdCdrM3vZsaP9bJXJqzUw', 'tr'),
    ('Selcoin',              'UCPBKbzQqjpv8aYotZHUV0YA', 'tr'),
    ('Kriptolik',            'UCNPpHMQGPrAm9MFzBVjCZxA', 'tr'),
    ('Crypto Mete',          'UCOj-sZB6oNaJBBe6B3xxbkA', 'tr'),
    ('JrKripto',             'UCnWTsToiAjGpGqLg7xIUKOw', 'tr'),
    ('Tarik Bilen',          'UCRGGpCBhcPrWB4bOHFGv-Ww', 'tr'),
    ('Kanal Finans',         'UCY5gEE3IFpEUTqTSUqH0M8w', 'tr'),
    ('Smart Finance',        'UCz0i7P-jBFlPFRKBqLqLSpA', 'tr'),
    ('Turhan Bozkurt',       'UCqb9O8rz3aHDJ0l6Wdxydxw', 'tr'),
    ('Halil Recber',         'UCwP5JExg2M9a2jWNQVqeXNA', 'tr'),
    ('Integral Forextv',     'UCBkRVBCEo-i7avECPPi2yig', 'tr'),
    ('TRADER MAN X',         'UCTZvpH2MroVf-tczS_KPVIQ', 'tr'),
]

_processed_videos = set()


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print('[YT] Telegram not configured')
        return
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
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


def fetch_latest_video(channel_id):
    # Support both UC... channel IDs and @handle format
    if channel_id.startswith('UC'):
        rss_url = f'https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}'
    else:
        # Try handle-based RSS (works for some channels)
        rss_url = f'https://www.youtube.com/feeds/videos.xml?user={channel_id}'
    try:
        r = requests.get(rss_url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        if not r.ok:
            print(f'[YT] RSS failed ({r.status_code}) for {channel_id}')
            return None
        root = ET.fromstring(r.content)
        ns = {'atom': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}
        entries = root.findall('atom:entry', ns)
        if not entries:
            return None
        entry = entries[0]
        video_id  = entry.find('yt:videoId', ns).text
        title     = entry.find('atom:title', ns).text
        published = entry.find('atom:published', ns).text
        pub_dt    = datetime.fromisoformat(published.replace('Z', '+00:00'))
        return {'id': video_id, 'title': title, 'published': pub_dt}
    except Exception as e:
        print(f'[YT] RSS error for {channel_id}: {e}')
        return None


def get_transcript(video_id, lang='en'):
    try:
        # Try new API style first (v0.6+)
        ytt_api = YouTubeTranscriptApi()
        try:
            transcript = ytt_api.fetch(video_id, languages=[lang, 'en', 'tr'])
        except Exception:
            transcript = ytt_api.fetch(video_id)
        text = ' '.join([s.text for s in transcript])
        return text[:8000] if len(text) > 8000 else text
    except Exception:
        pass
    try:
        # Fallback to old API style (v0.5.x)
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        for code in [lang, 'en', 'tr']:
            try:
                t = transcript_list.find_transcript([code])
                chunks = t.fetch()
                text = ' '.join([c['text'] for c in chunks])
                return text[:8000] if len(text) > 8000 else text
            except Exception:
                continue
    except Exception as e:
        print(f'[YT] Transcript error for {video_id}: {e}')
    return None


def summarize(channel_name, video_title, transcript, lang='en'):
    try:
        if lang == 'tr':
            prompt = f"""Sen bir kripto ve finans içerik analistisin.

Kanal: {channel_name}
Video başlığı: {video_title}

Transkript:
{transcript}

Lütfen aşağıdaki formatta Türkçe özet yap:

📊 GENEL GÖRÜŞ: (Boğa/Ayı/Nötr ve kısa açıklama)
💰 BAHSEDİLEN VARLIKLAR: (Ticker sembolleri ve yorum)
🎯 FİYAT HEDEFLERİ: (Varsa belirt, yoksa "Belirtilmedi")
⚠️ RİSKLER: (Bahsedilen riskler)
💡 ANA MESAJ: (1-2 cümle özet)

Sadece önemli bilgileri yaz."""
        else:
            prompt = f"""You are a crypto and finance content analyst.

Channel: {channel_name}
Video title: {video_title}

Transcript:
{transcript}

Provide a concise summary in this exact format:

📊 OVERALL SENTIMENT: (Bullish/Bearish/Neutral + brief reason)
💰 ASSETS MENTIONED: (Ticker symbols with commentary)
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
        print(f'[YT] Claude error: {e}')
        return None


def run_youtube_scan():
    print(f'[YT] Starting YouTube scan for {len(CHANNELS)} channels...')
    summaries_sent = 0
    for name, channel_id, lang in CHANNELS:
        try:
            video = fetch_latest_video(channel_id)
            if not video:
                print(f'[YT] No video found for {name}')
                continue
            vid_id = video['id']
            if vid_id in _processed_videos:
                print(f'[YT] Already sent latest for {name} — skipping')
                continue
            print(f'[YT] Processing: {name} — {video["title"]}')
            transcript = get_transcript(vid_id, lang)
            if not transcript:
                print(f'[YT] No transcript for {vid_id} — skipping')
                _processed_videos.add(vid_id)
                continue
            summary = summarize(name, video['title'], transcript, lang)
            if not summary:
                continue
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
            time.sleep(2)
        except Exception as e:
            print(f'[YT] Error processing {name}: {e}')
            continue
    print(f'[YT] Scan complete — {summaries_sent} summaries sent')


def youtube_monitor_loop():
    print(f'[YT] YouTube monitor started — checking every {CHECK_INTERVAL//3600} hours')
    time.sleep(120)
    while True:
        try:
            run_youtube_scan()
        except Exception as e:
            print(f'[YT] Monitor error: {e}')
        time.sleep(CHECK_INTERVAL)


def start_youtube_monitor():
    t = threading.Thread(target=youtube_monitor_loop, daemon=True)
    t.start()
    print('[YT] YouTube monitor thread started')


if __name__ == '__main__':
    run_youtube_scan()
