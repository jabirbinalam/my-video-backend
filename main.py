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
    try:
        res = requests.post(
            'https://api.cobalt.tools/api/json',
            headers={
                'Accept': 'application/json',
                'Content-Type': 'application/json',
            },
            json={
                'url': f'https://www.youtube.com/watch?v={video_id}',
                'vQuality': '720',
                'isAudioMuted': False,
            },
            timeout=20
        )
        print(f'[cobalt] status={res.status_code} body={res.text[:300]}')
        data = res.json()
        if data.get('status') in ['stream', 'redirect', 'tunnel', 'pick']:
            return data.get('url') or (data.get('picker', [{}])[0].get('url'))
    except Exception as e:
        print(f'[cobalt] error: {e}')

    try:
        res = requests.post(
            'https://yt1s.com/api/ajaxSearch/index',
            data={'q': f'https://www.youtube.com/watch?v={video_id}', 'vt': 'homevideo'},
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=20
        )
        print(f'[yt1s] status={res.status_code} body={res.text[:300]}')
        data = res.json()
        if data.get('status') == 'ok':
            links = data.get('links', {}).get('mp4', {})
            for quality in ['720', '480', '360']:
                if quality in links:
                    k = links[quality].get('k')
                    vid = data.get('vid')
                    if k and vid:
                        r2 = requests.post(
                            'https://yt1s.com/api/ajaxConvert/convert',
                            data={'vid': vid, 'k': k},
                            headers={'Content-Type': 'application/x-www-form-urlencoded'},
                            timeout=30
                        )
                        d2 = r2.json()
                        print(f'[yt1s convert] {d2}')
                        if d2.get('status') == 'ok':
                            return d2.get('dlink')
    except Exception as e:
        print(f'[yt1s] error: {e}')

    raise Exception('সব downloader fail করেছে')


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
        return jsonify({'title': title, 'duration': 600, 'videoId': video_id})
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
        print(f'[process] Getting download URL for {video_id}')
        download_url = get_download_url(video_id)
        print(f'[process] Download URL: {download_url[:80]}')

        print(f'[process] Downloading video...')
        r = requests.get(download_url, stream=True, timeout=300, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        print(f'[process] Download response: {r.status_code}, content-type: {r.headers.get("content-type")}')

        with open(raw_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

        file_size = os.path.getsize(raw_path)
        print(f'[process] File size: {file_size} bytes')

        if file_size < 100000:
            with open(raw_path, 'rb') as f:
                content = f.read(500)
            print(f'[process] File too small, content: {content}')
            cleanup(raw_path)
            return jsonify({'success': False, 'error': f'Download fail হয়েছে (file size: {file_size} bytes)'}), 500

        # Get actual duration
        probe = subprocess.run([
            'ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', raw_path
        ], capture_output=True, text=True)
        probe_data = json.loads(probe.stdout) if probe.stdout else {}
        actual_duration = float(probe_data.get('format', {}).get('duration', duration))
        print(f'[process] Actual duration: {actual_duration}s')

        if actual_duration < 120:
            cleanup(raw_path)
            return jsonify({'success': False, 'error': 'Video কমপক্ষে 2 মিনিটের হতে হবে'}), 400

        max_start = max(0, actual_duration - 120)
        start_time = random.randint(0, int(max_start))
        print(f'[process] Cutting clip from {start_time}s')

        result = subprocess.run([
            'ffmpeg', '-y', '-ss', str(start_time),
            '-i', raw_path, '-t', '120',
            '-c:v', 'libx264', '-c:a', 'aac', '-preset', 'fast',
            clip_path
        ], capture_output=True)
        print(f'[ffmpeg] returncode={result.returncode} stderr={result.stderr.decode()[-300:]}')

        if result.returncode != 0:
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
