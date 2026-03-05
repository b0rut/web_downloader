import os
import uuid
import threading
import time
import re
import json
import subprocess
import tempfile
import shutil
from flask import Flask, request, jsonify, Response, send_file, render_template
from flask_cors import CORS
import yt_dlp

# ---------- ffmpeg check ----------
FFMPEG_PATH = shutil.which('ffmpeg')
if FFMPEG_PATH:
    print(f"✅ ffmpeg found at: {FFMPEG_PATH}")
else:
    print("❌ ffmpeg NOT found. Audio conversion will fail.")

def ffmpeg_available():
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        return True
    except:
        return False

FFMPEG_OK = ffmpeg_available()
if not FFMPEG_OK:
    print("⚠️  WARNING: ffmpeg not found – audio conversion disabled")

app = Flask(__name__)
CORS(app)

downloads = {}
batches = {}

def format_size(bytes):
    if bytes is None:
        return "N/A"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes < 1024:
            return f"{bytes:.1f} {unit}"
        bytes /= 1024
    return f"{bytes:.1f} TB"

def download_worker(url, ydl_opts, download_id, retry_without_subs=False):
    initial_filepath = None
    final_filepath = None
    base_name = None

    def progress_hook(d):
        nonlocal initial_filepath, base_name
        if d['status'] == 'downloading':
            percent = d.get('_percent_str', '0%').strip('%')
            percent = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', percent)
            try:
                percent = float(percent)
            except:
                percent = 0
            speed = d.get('_speed_str', '').strip()
            eta = d.get('_eta_str', '').strip()
            downloads[download_id].update({
                'status': 'downloading',
                'progress': percent,
                'speed': speed,
                'eta': eta
            })
        elif d['status'] == 'finished':
            initial_filepath = d['filename']
            base_name = os.path.splitext(os.path.basename(initial_filepath))[0]
            print(f"[{download_id}] download finished, temp file: {initial_filepath}")
            print(f"[{download_id}] base name: {base_name}")

    def postprocessor_hook(d):
        nonlocal final_filepath
        print(f"[{download_id}] postprocessor_hook: {d}")
        if d['status'] == 'finished' and 'filepath' in d:
            final_filepath = d['filepath']
            print(f"[{download_id}] post‑processing finished, final file: {final_filepath}")

    ydl_opts['progress_hooks'] = [progress_hook]
    ydl_opts['postprocessor_hooks'] = [postprocessor_hook]

    if FFMPEG_PATH:
        ydl_opts['ffmpeg_location'] = FFMPEG_PATH

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        error_str = str(e)
        # If it's a subtitle 429 error and we haven't retried yet, retry without subs
        if 'subtitles' in error_str and '429' in error_str and not retry_without_subs:
            print(f"[{download_id}] Subtitle error (429), retrying without subtitles...")
            # Remove subtitle options
            ydl_opts.pop('writesubtitles', None)
            ydl_opts.pop('writeautomaticsub', None)
            ydl_opts.pop('subtitleslangs', None)
            # Recursive call with retry flag
            return download_worker(url, ydl_opts, download_id, retry_without_subs=True)
        else:
            raise

    # After download, determine final file
    if final_filepath:
        downloads[download_id]['filepath'] = final_filepath
        print(f"[{download_id}] using hook filepath: {final_filepath}")
    else:
        found = False
        if base_name and initial_filepath:
            temp_dir = os.path.dirname(initial_filepath)
            allowed_exts = ['.mp3', '.m4a', '.wav', '.aac', '.opus', '.ogg', '.flac',
                            '.mp4', '.mkv', '.webm', '.mov', '.avi']
            for f in os.listdir(temp_dir):
                f_path = os.path.join(temp_dir, f)
                if os.path.isfile(f_path):
                    name, ext = os.path.splitext(f)
                    if name == base_name and ext.lower() in allowed_exts:
                        final_filepath = f_path
                        print(f"[{download_id}] found converted/merged file by scanning: {final_filepath}")
                        found = True
                        break

        if not found:
            # Fallback: find the most recent file in temp dir with a valid extension
            temp_dir = tempfile.gettempdir()
            now = time.time()
            candidates = []
            for f in os.listdir(temp_dir):
                f_path = os.path.join(temp_dir, f)
                if os.path.isfile(f_path):
                    # Check if file was modified within the last 60 seconds
                    if now - os.path.getmtime(f_path) < 60:
                        ext = os.path.splitext(f)[1].lower()
                        if ext in ['.mp3', '.m4a', '.wav', '.aac', '.opus', '.ogg', '.flac',
                                   '.mp4', '.mkv', '.webm', '.mov', '.avi']:
                            candidates.append((os.path.getmtime(f_path), f_path))
            if candidates:
                # Choose the most recent
                candidates.sort(reverse=True)
                final_filepath = candidates[0][1]
                print(f"[{download_id}] fallback: using most recent file: {final_filepath}")
                found = True

        if final_filepath:
            downloads[download_id]['filepath'] = final_filepath
        else:
            downloads[download_id]['filepath'] = initial_filepath
            print(f"[{download_id}] no file found, using initial: {initial_filepath}")

    downloads[download_id]['status'] = 'finished'
    downloads[download_id]['progress'] = 100
    print(f"[{download_id}] ✅ fully finished, file: {downloads[download_id]['filepath']}")

@app.route('/')
def index():
    with open('index.html', encoding='utf-8') as f:
        return f.read()

@app.route('/api/info', methods=['POST'])
def get_info():
    data = request.get_json()
    url = data.get('url')
    if not url:
        return jsonify({'error': 'URL required'}), 400

    try:
        ydl_opts = {
            'quiet': False,
            'verbose': True,
            'no_warnings': False,
            'extract_flat': False,
            'noplaylist': True,
            'socket_timeout': 30,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = []
        for f in info.get('formats', []):
            if f.get('vcodec') == 'none' and f.get('acodec') == 'none':
                continue
            filesize = f.get('filesize') or f.get('filesize_approx')
            formats.append({
                'format_id': f['format_id'],
                'ext': f.get('ext', '?'),
                'resolution': f.get('resolution') or f.get('format_note') or 'N/A',
                'vcodec': f.get('vcodec', 'none'),
                'acodec': f.get('acodec', 'none'),
                'filesize': filesize,
                'filesize_str': format_size(filesize),
                'fps': f.get('fps')
            })

        response = {
            'title': info.get('title', 'N/A'),
            'duration': info.get('duration'),
            'thumbnail': info.get('thumbnail'),
            'formats': formats
        }
        return jsonify(response)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download', methods=['POST'])
def start_download():
    data = request.get_json()
    url = data.get('url')
    format_id = data.get('format_id')
    options = data.get('options', {})

    print(f"Received download request: url={url}, audio_only={options.get('audio_only')}")
    if not url:
        return jsonify({'error': 'URL required'}), 400

    download_id = str(uuid.uuid4())
    downloads[download_id] = {
        'status': 'starting',
        'progress': 0,
        'speed': '',
        'eta': '',
        'filepath': None,
        'error': None
    }

    ydl_opts = {
        'outtmpl': os.path.join(tempfile.gettempdir(), '%(title)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30,
        'extractor_retries': 5,
        'sleep_requests': 1,
    }

    if options.get('playlist'):
        ydl_opts['yes_playlist'] = True
    else:
        ydl_opts['noplaylist'] = True

    if options.get('subs'):
        ydl_opts['writesubtitles'] = True
        ydl_opts['writeautomaticsub'] = True
        ydl_opts['subtitleslangs'] = ['en']

    if options.get('embed_thumb'):
        ydl_opts['writethumbnail'] = True
        ydl_opts['embedthumbnail'] = True

    if options.get('embed_meta'):
        ydl_opts['embedmetadata'] = True

    if options.get('audio_only'):
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': options.get('audio_format', 'mp3'),
            'preferredquality': options.get('audio_bitrate', '192'),
        }]
    else:
        if format_id is None:
            choice = options.get('quality_choice', '')
            if 'Best combined' in choice:
                ydl_opts['format'] = 'best[ext=mp4]/best'
            elif 'Best video + best audio' in choice:
                ydl_opts['format'] = 'bestvideo+bestaudio/best'
            else:
                ydl_opts['format'] = 'best'
        else:
            ydl_opts['format'] = format_id

    thread = threading.Thread(target=download_worker, args=(url, ydl_opts, download_id))
    thread.daemon = True
    thread.start()

    return jsonify({'download_id': download_id})

@app.route('/api/progress/<download_id>')
def progress_stream(download_id):
    def generate():
        if not FFMPEG_OK and downloads.get(download_id, {}).get('status') == 'starting':
            yield f"event: warning\ndata: ffmpeg not found. Audio conversion may fail.\n\n"

        while True:
            if download_id not in downloads:
                yield f"event: error\ndata: Download not found\n\n"
                break
            state = downloads[download_id]
            if state['status'] == 'downloading':
                yield f"event: progress\ndata: {json.dumps({'progress': state['progress'], 'speed': state['speed'], 'eta': state['eta']})}\n\n"
            elif state['status'] == 'finished':
                yield f"event: finished\ndata: {json.dumps({'file': f'/api/file/{download_id}'})}\n\n"
                break
            elif state['status'] == 'error':
                yield f"event: error\ndata: {state['error']}\n\n"
                break
            elif state['status'] == 'starting':
                yield f"event: starting\ndata: \n\n"
            time.sleep(0.5)
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/file/<download_id>')
def get_file(download_id):
    import sys
    print(f"\n📥 GET file for {download_id}"); sys.stdout.flush()

    if download_id not in downloads:
        print("❌ Download ID not found"); sys.stdout.flush()
        return 'Download ID not found', 404

    state = downloads[download_id]
    print(f"Status: {state['status']}"); sys.stdout.flush()
    if state['status'] != 'finished':
        print("❌ File not ready"); sys.stdout.flush()
        return 'File not ready yet', 404

    filepath = state['filepath']
    print(f"Stored path: {repr(filepath)}"); sys.stdout.flush()

    if not filepath:
        print("❌ No filepath stored"); sys.stdout.flush()
        return 'No filepath', 404

    filepath = os.path.normpath(filepath)
    print(f"Normalized: {repr(filepath)}"); sys.stdout.flush()

    if not os.path.exists(filepath):
        print(f"❌ File does NOT exist at: {filepath}"); sys.stdout.flush()
        dirname = os.path.dirname(filepath)
        if os.path.exists(dirname):
            files = os.listdir(dirname)
            print(f"Files in {dirname}: {files}"); sys.stdout.flush()
        return f'File not found on server: {filepath}', 404

    print(f"✅ File exists, sending: {filepath}"); sys.stdout.flush()

    # Safe copy fallback for Windows Unicode issues
    safe_dir = tempfile.gettempdir()
    safe_name = f"download_{download_id}{os.path.splitext(filepath)[1]}"
    safe_path = os.path.join(safe_dir, safe_name)
    shutil.copy2(filepath, safe_path)
    print(f"📋 Copied to safe path: {safe_path}"); sys.stdout.flush()

    return send_file(safe_path, as_attachment=True, download_name=os.path.basename(filepath))

# ---------- Batch endpoints (optional) ----------
@app.route('/api/batch', methods=['POST'])
def start_batch():
    data = request.get_json()
    urls = data.get('urls', [])
    if not urls:
        return jsonify({'error': 'No URLs provided'}), 400

    batch_id = str(uuid.uuid4())
    batches[batch_id] = {
        'status': 'pending',
        'total': len(urls),
        'current': 0,
        'items': urls,
        'errors': [],
        'download_ids': []
    }

    thread = threading.Thread(target=batch_worker, args=(batch_id,))
    thread.daemon = True
    thread.start()
    return jsonify({'batch_id': batch_id})

def batch_worker(batch_id):
    batch = batches[batch_id]
    batch['status'] = 'running'
    for idx, url in enumerate(batch['items']):
        batch['current'] = idx + 1
        download_id = str(uuid.uuid4())
        batch['download_ids'].append(download_id)
        downloads[download_id] = {'status': 'starting', 'progress': 0, 'speed': '', 'eta': '', 'filepath': None, 'error': None}
        ydl_opts = {
            'outtmpl': os.path.join(tempfile.gettempdir(), f'batch_{batch_id}_{idx}.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'format': 'best',
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            downloads[download_id]['status'] = 'finished'
        except Exception as e:
            downloads[download_id]['status'] = 'error'
            downloads[download_id]['error'] = str(e)
            batch['errors'].append(str(e))
    batch['status'] = 'completed'

@app.route('/api/batch_progress/<batch_id>')
def batch_progress(batch_id):
    def generate():
        while True:
            if batch_id not in batches:
                yield f"event: error\ndata: Batch not found\n\n"
                break
            batch = batches[batch_id]
            if batch['status'] == 'running':
                data = {
                    'total': batch['total'],
                    'current': batch['current'],
                    'percent': (batch['current'] / batch['total']) * 100,
                    'errors': batch['errors']
                }
                yield f"event: progress\ndata: {json.dumps(data)}\n\n"
            elif batch['status'] == 'completed':
                yield f"event: completed\ndata: {json.dumps({'total': batch['total'], 'errors': batch['errors']})}\n\n"
                break
            time.sleep(1)
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(debug=True, threaded=True)