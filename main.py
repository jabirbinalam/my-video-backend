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

app = Flask(__name__)
CORS(app)

ANTHROPIC_CLIENT = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
TEMP_DIR = tempfile.gettempdir()

PIPED_INSTANCES = [
    'https://pipedapi.kavin.rocks',
    'https://piped-api.garudalinux.org',
    'https://api.piped.projectsegfau.lt',
]

def extract_video_id(url):
    patterns = [
        r'(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'(?:embed/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$'
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

def get_video_info(video_id):
    for instance in PIPED_INSTANCES:
        try:
            res = requests.get(f'{instance}/streams/{video_id}', timeout=10)
            if res.status_code == 200:
                data = res.json()
                return {
                    'title': data.get('title', 'Untitled'),
                    'duration': data.get('duration', 0),
                    'thumbnail': data.get('thumbnailUrl', ''),
                    'streams': data.get('videoStreams', []),
                    'audioStreams': data.get('audioStreams', []),
                }
        except:
            continue
    raise Exception('সব Piped instance fail করেছে')

def get_best_stream_url(streams, max_height=720):
    filtered = [s for s in streams if s.get('videoOnly') == False]
    if not filtered:
        filtered = streams
    filtered = [s for s in filtered if s.get('quality', '').replace('p','').isdigit()]
    filtered = [s for s in filtered if int(s.get('quality','0p').replace('p','')) <= max_height]
    if not filtered:
        return streams[0].get('url') if streams else None
    filtered.sort(key=lambda x: int(x.get('quality','0p').replace('p','')), reverse=True)
    return filtered[0].get('url')

def get_audio_stream_url(audio_streams):
    if not audio_streams:
        return None
    audio_streams.sort(key=lambda x: x.get('bitrate', 0), reverse=True)
    return audio_streams[0].get('url')


# ========== VIDEO INFO ==========
@app.route('/video-info', methods=['POST'])
def video_info():
    data = request.json
    url = data.get('url', '')
    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({'error': 'Valid YouTube URL দাও'}), 400
    try:
        info = get_video_info(video_id)
        return jsonify({
            'title': info['title'],
            'duration': info['duration'],
            'thumbnail': info['thumbnail'],
            'videoId': video_id,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ========== PROCESS + UPLOAD ==========
@app.route('/process-upload', methods=['POST'])
def process_upload():
    data = request.json
    url = data.get('url', '')
    duration = data.get('duration', 0)
    clip_index = data.get('clipIndex', 0)
    caption = data.get('caption', '')
    title = data.get('title', 'Video')

    auth = request.headers.get('Authorization', '')
    yt_token = auth.replace('Bearer ', '').strip()

    if not yt_token:
        return jsonify({'success': False, 'error': 'YouTube token নেই'}), 401
    if duration < 120:
        return jsonify({'success': False, 'error': 'Video কমপক্ষে 2 মিনিটের হতে হবে'}), 400

    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({'success': False, 'error': 'Invalid YouTube URL'}), 400

    pid = os.getpid()
    video_path = os.path.join(TEMP_DIR, f'video_{clip_index}_{pid}.mp4')
    audio_path = os.path.join(TEMP_DIR, f'audio_{clip_index}_{pid}.m4a')
    merged_path = os.path.join(TEMP_DIR, f'merged_{clip_index}_{pid}.mp4')
    clip_path = os.path.join(TEMP_DIR, f'clip_{clip_index}_{pid}.mp4')

    try:
        # Get stream URLs from Piped
        info = get_video_info(video_id)
        video_url = get_best_stream_url(info['streams'])
        audio_url = get_audio_stream_url(info['audioStreams'])

        if not video_url:
            return jsonify({'success': False, 'error': 'Stream URL পাওয়া যায়নি'}), 500

        # Random 2 min start
        max_start = max(0, duration - 120)
        start_time = random.randint(0, int(max_start))

        # Download video stream
        v_res = requests.get(video_url, stream=True, timeout=60)
        with open(video_path, 'wb') as f:
            for chunk in v_res.iter_content(chunk_size=8192):
                f.write(chunk)

        if audio_url:
            # Download audio stream
            a_res = requests.get(audio_url, stream=True, timeout=60)
            with open(audio_path, 'wb') as f:
                for chunk in a_res.iter_content(chunk_size=8192):
                    f.write(chunk)

            # Merge video + audio
            subprocess.run([
                'ffmpeg', '-y',
                '-i', video_path,
                '-i', audio_path,
                '-c:v', 'copy', '-c:a', 'aac',
                merged_path
            ], check=True, capture_output=True)

            cleanup(video_path, audio_path)
            source = merged_path
        else:
            source = video_path

        # Cut 2 min clip
        subprocess.run([
            'ffmpeg', '-y',
            '-ss', str(start_time),
            '-i', source,
            '-t', '120',
            '-c:v', 'libx264', '-c:a', 'aac',
            '-preset', 'fast',
            clip_path
        ], check=True, capture_output=True)

        cleanup(source)

        # Generate caption if empty
        if not caption.strip():
            caption = generate_caption(title, clip_index)

        # Upload to YouTube
        video_id_uploaded = upload_to_youtube(clip_path, title, caption, yt_token, clip_index)
        cleanup(clip_path)

        return jsonify({'success': True, 'videoId': video_id_uploaded})

    except subprocess.CalledProcessError as e:
        cleanup(video_path, audio_path, merged_path, clip_path)
        return jsonify({'success': False, 'error': 'ffmpeg error: ' + e.stderr.decode()[-200:]}), 500
    except Exception as e:
        cleanup(video_path, audio_path, merged_path, clip_path)
        return jsonify({'success': False, 'error': str(e)}), 500


def cleanup(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except:
            pass


# ========== CAPTION GENERATOR ==========
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


# ========== YOUTUBE UPLOAD ==========
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
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
