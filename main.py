from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
import subprocess
import os
import random
import tempfile
import requests
import json
import anthropic

app = Flask(__name__)
CORS(app)

ANTHROPIC_CLIENT = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
TEMP_DIR = tempfile.gettempdir()
COOKIES_PATH = os.path.join(TEMP_DIR, 'yt_cookies.txt')

def setup_cookies():
    cookies_content = os.environ.get('YOUTUBE_COOKIES', '')
    if cookies_content:
        with open(COOKIES_PATH, 'w') as f:
            f.write(cookies_content)

setup_cookies()

def get_ydl_opts(extra={}):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'web'],
            }
        },
        'http_headers': {
            'User-Agent': 'com.google.ios.youtube/19.29.1 (iPhone16,2; U; CPU iOS 17_5_1 like Mac OS X;)',
            'Accept-Language': 'en-US,en;q=0.9',
        },
    }
    if os.path.exists(COOKIES_PATH):
        opts['cookiefile'] = COOKIES_PATH
    opts.update(extra)
    return opts


@app.route('/video-info', methods=['POST'])
def video_info():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({'error': 'URL দাও'}), 400
    try:
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
            return jsonify({
                'title': info.get('title', 'Untitled'),
                'duration': info.get('duration', 0),
                'thumbnail': info.get('thumbnail', ''),
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/process-upload', methods=['POST'])
def process_upload():
    data = request.json
    url = data.get('url')
    duration = data.get('duration', 0)
    clip_index = data.get('clipIndex', 0)
    caption = data.get('caption', '')
    title = data.get('title', 'Video')

    auth = request.headers.get('Authorization', '')
    yt_token = auth.replace('Bearer ', '').strip()

    if not yt_token:
        return jsonify({'success': False, 'error': 'YouTube token নেই'}), 401
    if duration < 120:
        return jsonify({'success': False, 'error': 'Video too short'}), 400

    raw_path = os.path.join(TEMP_DIR, f'raw_{clip_index}_{os.getpid()}.mp4')
    clip_path = os.path.join(TEMP_DIR, f'clip_{clip_index}_{os.getpid()}.mp4')

    try:
        max_start = duration - 120
        start_time = random.randint(0, int(max_start))

        dl_opts = get_ydl_opts({
            'format': 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': raw_path,
        })
        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            ydl.download([url])

        subprocess.run([
            'ffmpeg', '-y', '-ss', str(start_time),
            '-i', raw_path, '-t', '120',
            '-c:v', 'libx264', '-c:a', 'aac', '-preset', 'fast',
            clip_path
        ], check=True, capture_output=True)

        if os.path.exists(raw_path):
            os.remove(raw_path)

        if not caption.strip():
            caption = generate_caption(title, clip_index)

        video_id = upload_to_youtube(clip_path, title, caption, yt_token, clip_index)

        if os.path.exists(clip_path):
            os.remove(clip_path)

        return jsonify({'success': True, 'videoId': video_id})

    except Exception as e:
        cleanup(raw_path, clip_path)
        return jsonify({'success': False, 'error': str(e)}), 500


def cleanup(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p): os.remove(p)
        except: pass


def generate_caption(title, clip_index):
    try:
        msg = ANTHROPIC_CLIENT.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=200,
            messages=[{'role': 'user', 'content': f'YouTube video title: "{title}". Clip #{clip_index+1}. Write a short catchy YouTube Short caption max 150 chars with 3-4 hashtags. Just the caption, nothing else.'}]
        )
        return msg.content[0].text.strip()
    except:
        return f'{title} | Clip {clip_index + 1} #shorts #viral #trending'


def upload_to_youtube(file_path, title, caption, token, clip_index):
    upload_url = 'https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status'
    metadata = {
        'snippet': {'title': f'{title} | Clip {clip_index + 1}', 'description': caption, 'tags': ['shorts', 'viral', 'trending'], 'categoryId': '22'},
        'status': {'privacyStatus': 'public', 'selfDeclaredMadeForKids': False}
    }
    init_res = requests.post(upload_url, headers={
        'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'X-Upload-Content-Type': 'video/mp4'
    }, data=json.dumps(metadata))

    if init_res.status_code != 200:
        raise Exception(f'Upload init failed: {init_res.text}')

    upload_uri = init_res.headers.get('Location')
    with open(file_path, 'rb') as f:
        upload_res = requests.put(upload_uri, headers={
            'Authorization': f'Bearer {token}', 'Content-Type': 'video/mp4'
        }, data=f)

    if upload_res.status_code not in [200, 201]:
        raise Exception(f'Upload failed: {upload_res.text}')

    return upload_res.json().get('id', 'unknown')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
