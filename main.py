from flask import Flask, request, jsonify
from flask_cors import CORS
import subprocess
import os
import random
import tempfile
import requests
import json
import anthropic
import re
import traceback

app = Flask(__name__)
CORS(app)

ANTHROPIC_CLIENT = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
TEMP_DIR = tempfile.gettempdir()
RAPIDAPI_KEY = os.environ.get('RAPIDAPI_KEY', '198a0d8021msh7c915ee82977c80p143959jsne0a91020f9be')

def extract_video_id(url):
    patterns = [
        r'(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'(?:embed/)([a-zA-Z0-9_-]{11})',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

def get_download_url(video_id):
    print(f'[rapidapi] Getting download URL for {video_id}')
    try:
        res = requests.get(
            'https://youtube-media-downloader.p.rapidapi.com/v2/video/details',
            headers={
                'x-rapidapi-key': RAPIDAPI_KEY,
                'x-rapidapi-host': 'youtube-media-downloader.p.rapidapi.com'
            },
            params={'videoId': video_id},
            timeout=30
        )
        print(f'[rapidapi] status={res.status_code} body={res.text[:500]}')
        data = res.json()

        if not data.get('status'):
            raise Exception(f'RapidAPI error: {data}')

        # Get best video stream
        videos = data.get('videos', {}).get('items', [])
        print(f'[rapidapi] Found {len(videos)} video streams')

        # Filter for mp4 with audio
        mp4_streams = [v for v in videos if v.get('extension') == 'mp4' and not v.get('videoOnly', True)]
        if not mp4_streams:
            mp4_streams = [v for v in videos if v.get('extension') == 'mp4']

        if not mp4_streams:
            raise Exception('কোনো mp4 stream পাওয়া যায়নি')

        # Sort by quality
        def get_height(s):
            try:
                return int(s.get('height', 0) or 0)
            except:
                return 0

        mp4_streams.sort(key=get_height, reverse=True)

        # Pick 720p or best available
        chosen = None
        for s in mp4_streams:
            h = get_height(s)
            if h <= 720:
                chosen = s
                break
        if not chosen:
            chosen = mp4_streams[-1]

        url = chosen.get('url')
        print(f'[rapidapi] Chosen stream: height={chosen.get("height")} url={str(url)[:80]}')
        return url

    except Exception as e:
        print(f'[rapidapi] EXCEPTION: {traceback.format_exc()}')
        raise Exception(f'RapidAPI fail: {str(e)}')


@app.route('/video-info', methods=['POST'])
def video_info():
    data = request.json
    url = data.get('url', '')
    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({'error': 'Valid YouTube URL দাও'}), 400
    try:
        res = requests.get(
            f'https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json',
            timeout=10
        )
        title = res.json().get('title', 'Untitled') if res.status_code == 200 else 'Untitled'

        # Get duration via RapidAPI
        try:
            r2 = requests.get(
                'https://youtube-media-downloader.p.rapidapi.com/v2/video/details',
                headers={
                    'x-rapidapi-key': RAPIDAPI_KEY,
                    'x-rapidapi-host': 'youtube-media-downloader.p.rapidapi.com'
                },
                params={'videoId': video_id},
                timeout=20
            )
            d2 = r2.json()
            duration = int(d2.get('lengthSeconds', 600) or 600)
        except:
            duration = 600

        return jsonify({'title': title, 'duration': duration, 'videoId': video_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/process-upload', methods=['POST'])
def process_upload():
    data = request.json
    url = data.get('url', '')
    duration = data.get('duration', 600)
    clip_index = data.get('clipIndex', 0)
    caption = data.get('caption', '')
    title = data.get('title', 'Video')

    auth = request.headers.get('Authorization', '')
    yt_token = auth.replace('Bearer ', '').strip()

    if not yt_token:
        return jsonify({'success': False, 'error': 'YouTube token নেই'}), 401

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({'success': False, 'error': 'Invalid YouTube URL'}), 400

    pid = os.getpid()
    raw_path = os.path.join(TEMP_DIR, f'raw_{clip_index}_{pid}.mp4')
    clip_path = os.path.join(TEMP_DIR, f'clip_{clip_index}_{pid}.mp4')

    try:
        download_url = get_download_url(video_id)
        if not download_url:
            return jsonify({'success': False, 'error': 'Download URL পাওয়া যায়নি'}), 500

        print(f'[process] Downloading video...')
        r = requests.get(download_url, stream=True, timeout=300, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        print(f'[process] Download status: {r.status_code}')

        with open(raw_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

        file_size = os.path.getsize(raw_path)
        print(f'[process] File size: {file_size} bytes')

        if file_size < 100000:
            cleanup(raw_path)
            return jsonify({'success': False, 'error': f'Download fail (size: {file_size} bytes)'}), 500

        # Get actual duration
        probe = subprocess.run([
            'ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', raw_path
        ], capture_output=True, text=True)
        probe_data = json.loads(probe.stdout) if probe.stdout else {}
        actual_duration = float(probe_data.get('format', {}).get('duration', duration))
        print(f'[process] Duration: {actual_duration}s')

        if actual_duration < 120:
            cleanup(raw_path)
            return jsonify({'success': False, 'error': 'Video কমপক্ষে 2 মিনিটের হতে হবে'}), 400

        max_start = max(0, actual_duration - 120)
        start_time = random.randint(0, int(max_start))
        print(f'[process] Cutting from {start_time}s')

        result = subprocess.run([
            'ffmpeg', '-y', '-ss', str(start_time),
            '-i', raw_path, '-t', '120',
            '-c:v', 'libx264', '-c:a', 'aac', '-preset', 'fast',
            clip_path
        ], capture_output=True)

        if result.returncode != 0:
            print(f'[ffmpeg] error: {result.stderr.decode()[-300:]}')
            cleanup(raw_path, clip_path)
            return jsonify({'success': False, 'error': 'ffmpeg clip cut fail'}), 500

        cleanup(raw_path)

        if not caption.strip():
            caption = generate_caption(title, clip_index)

        print(f'[process] Uploading to YouTube...')
        video_id_uploaded = upload_to_youtube(clip_path, title, caption, yt_token, clip_index)
        cleanup(clip_path)
        print(f'[process] Done! videoId={video_id_uploaded}')

        return jsonify({'success': True, 'videoId': video_id_uploaded})

    except Exception as e:
        print(f'[process] EXCEPTION: {traceback.format_exc()}')
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
        'snippet': {
            'title': f'{title} | Clip {clip_index + 1}',
            'description': caption,
            'tags': ['shorts', 'viral', 'trending', 'clips'],
            'categoryId': '22',
        },
        'status': {'privacyStatus': 'public', 'selfDeclaredMadeForKids': False}
    }
    init_res = requests.post(upload_url, headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'X-Upload-Content-Type': 'video/mp4',
    }, data=json.dumps(metadata))

    if init_res.status_code != 200:
        raise Exception(f'Upload init failed: {init_res.text}')

    upload_uri = init_res.headers.get('Location')
    with open(file_path, 'rb') as f:
        upload_res = requests.put(upload_uri, headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'video/mp4',
        }, data=f)

    if upload_res.status_code not in [200, 201]:
        raise Exception(f'Upload failed: {upload_res.text}')

    return upload_res.json().get('id', 'unknown')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
