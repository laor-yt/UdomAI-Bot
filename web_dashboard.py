import os
import json
import urllib.parse
import urllib.request
import cgi
import uuid
import random
import string
import time as _time
import hashlib
import hmac
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from user_manager import load_users, save_users, toggle_user_status, update_user_role, get_system_config, update_system_config, get_user_telegram_bots, add_user_telegram_bot, update_user_telegram_bot, set_user_max_custom_bots, delete_user_telegram_bot, get_active_user_bot, get_default_schedule_bot, set_default_schedule_bot
import scheduler

# ─── In-Memory Auth Stores ────────────────────────────────────────────────────
_otp_store = {}           # {user_id_str: {'code': '123456', 'expiry': float}}
_otp_store_lock = threading.Lock()

_session_store = {}       # {session_token: user_id_str}
_session_store_lock = threading.Lock()

_otp_verified_store = {}  # {temp_token: user_id_str} – after OTP OK, before password set
_otp_verified_lock = threading.Lock()

# Global dictionary to track progress of background tasks
TASK_PROGRESS = {}

def _background_yt_telegram_send(user_id_str, user_role, yt_url, format_id, num_clips, task_id=None):
    try:
        import asyncio
        from downloader import download_media
        from video_splitter import split_video
        import custom_bots_manager
        import scheduler
        import time

        if task_id:
            TASK_PROGRESS[task_id] = {"status": "running", "percent": 0, "text": "Starting download..."}

        print(f"[send_telegram] Starting download for {yt_url}")
        from utils import get_temp_dir
        from pytubefix import YouTube
        import subprocess
        
        temp_dir = get_temp_dir()
        downloaded_file = None

        proxies = {"http": "http://127.0.0.1:1080", "https": "http://127.0.0.1:1080"} if os.environ.get('USE_VPN_PROXY') == 'true' else None
        proxy_configs = [proxies, None] if proxies else [None]

        yt = None
        for p_conf in proxy_configs:
            if yt: break
            for client_str in ['ANDROID_VR', 'MWEB', 'IOS', 'TV', 'ANDROID', 'WEB_CREATOR', 'WEB']:
                try:
                    _yt = YouTube(yt_url, client=client_str, proxies=p_conf)
                    _ = _yt.streams
                    yt = _yt
                    break
                except Exception as e:
                    pass

        if not yt:
            print("[send_telegram] pytubefix failed to fetch info")
            if task_id: TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": "Failed to fetch video info"}
            return

        def on_progress(stream, chunk, bytes_remaining):
            if not task_id: return
            total = stream.filesize
            if total > 0:
                percent = int(((total - bytes_remaining) / total) * 100)
                TASK_PROGRESS[task_id] = {"status": "running", "percent": percent // 2, "text": f"Downloading {percent}%"}

        yt.register_on_progress_callback(on_progress)

        try:
            if format_id.startswith('SERVER_MERGE:'):
                v_itag = int(format_id.split(':', 1)[1])
                v_stream = yt.streams.get_by_itag(v_itag)
                a_stream = yt.streams.get_audio_only()
                
                if not v_stream or not a_stream:
                    raise Exception("Missing streams for merge")

                v_path = v_stream.download(output_path=temp_dir, filename=f"temp_v_{task_id}.mp4")
                a_path = a_stream.download(output_path=temp_dir, filename=f"temp_a_{task_id}.m4a")

                out_path = os.path.join(temp_dir, f"send_tg_{int(time.time())}_{task_id}.mp4")
                import imageio_ffmpeg
                ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                subprocess.run([ffmpeg_exe, '-y', '-i', v_path, '-i', a_path, '-c:v', 'copy', '-c:a', 'aac', out_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                if os.path.exists(v_path): os.remove(v_path)
                if os.path.exists(a_path): os.remove(a_path)
                downloaded_file = out_path
            else:
                itag = int(format_id)
                stream = yt.streams.get_by_itag(itag)
                if not stream:
                    raise Exception("Stream not found")
                downloaded_file = stream.download(output_path=temp_dir, filename=f"send_tg_{int(time.time())}_{task_id}.mp4")
                
        except Exception as e:
            print(f"[send_telegram] Download failed: {e}")
            
        if not downloaded_file or not os.path.exists(downloaded_file):
            print("[send_telegram] Download failed to produce a file.")
            if task_id: TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": "Download failed"}
            return
            
        print(f"[send_telegram] Downloaded {downloaded_file}")
        if task_id: TASK_PROGRESS[task_id] = {"status": "running", "percent": 50, "text": "Processing clips..."}
        
        clips = split_video(downloaded_file, num_clips)
        
        client_to_use = None
        if user_role == 'SUPER_ADMIN':
            import sys
            client_to_use = getattr(sys.modules.get('__main__'), 'app', None)
            if not client_to_use:
                raise Exception("Main bot client not found in memory.")
        else:
            default_bot_token = get_default_schedule_bot(user_id_str)
            if default_bot_token and default_bot_token in custom_bots_manager.active_custom_bots:
                client_to_use = custom_bots_manager.active_custom_bots[default_bot_token]
            
        if not client_to_use:
            print("[send_telegram] No Pyrogram client available to send.")
            if task_id: 
                err_msg = "No Telegram bot available" if user_role == 'SUPER_ADMIN' else "You must add a Custom Bot in settings first!"
                TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": err_msg}
            return

        async def send_clips():
            try:
                for i, clip in enumerate(clips):
                    print(f"[send_telegram] Sending clip {i+1}/{len(clips)}: {clip}")
                    caption = f"Clip {i+1}/{len(clips)}" if len(clips) > 1 else ""
                    
                    async def tg_progress(current, total):
                        if task_id and total > 0:
                            p = int((current / total) * 100)
                            clip_contribution = 50 / len(clips)
                            base_percent = 50 + (i * clip_contribution)
                            overall_percent = int(base_percent + (p * clip_contribution / 100))
                            TASK_PROGRESS[task_id] = {"status": "running", "percent": overall_percent, "text": f"Uploading to Telegram... {p}% (Clip {i+1}/{len(clips)})"}

                    await client_to_use.send_document(chat_id=int(user_id_str), document=clip, caption=caption, progress=tg_progress)
                    if clip != downloaded_file and os.path.exists(clip):
                        os.remove(clip)
                if os.path.exists(downloaded_file):
                    os.remove(downloaded_file)
                if task_id:
                    TASK_PROGRESS[task_id] = {"status": "done", "percent": 100, "text": "Completed!"}
            except Exception as e:
                if task_id: TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": f"Upload error: {e}"}
                raise e
                
        if custom_bots_manager.main_loop:
            asyncio.run_coroutine_threadsafe(send_clips(), custom_bots_manager.main_loop)
        else:
            asyncio.run(send_clips())
            
    except Exception as e:
        print(f"[send_telegram] Error: {e}")
        if task_id: TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": f"Error: {str(e)}"}

OTP_EXPIRY_SECONDS = 300  # 5 minutes


def _generate_otp():
    return ''.join(random.choices(string.digits, k=6))


def _generate_token():
    return uuid.uuid4().hex + uuid.uuid4().hex


def _send_otp_via_telegram(user_id_str, otp_code):
    """Send OTP code to user's Telegram chat via Bot API."""
    # Use the global configured OTP Bot or fallback to main bot
    system_config = get_system_config()
    custom_bot = system_config.get("otp_bot_token")
    if custom_bot == "main_bot":
        custom_bot = None

    if custom_bot:
        bot_token = custom_bot
    else:
        bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        
    if not bot_token:
        print("⚠️  No TELEGRAM_BOT_TOKEN set; cannot send OTP.")
        return False
    try:
        msg = (
            f"🔐 <b>Dashboard Login OTP</b>\n\n"
            f"Your verification code is:\n\n"
            f"<code>{otp_code}</code>\n\n"
            f"⏱ This code expires in <b>5 minutes</b>.\n"
            f"🚫 Do NOT share this code with anyone."
        )
        data = json.dumps({
            'chat_id': int(user_id_str),
            'text': msg,
            'parse_mode': 'HTML'
        }).encode('utf-8')
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=data,
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"Error sending OTP via Telegram: {e}")
        return False


def _hash_password(password):
    """Hash password using PBKDF2-HMAC-SHA256 with a random salt."""
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100_000)
    return salt.hex() + ':' + key.hex()


def _verify_password(stored_hash, password):
    """Verify a plaintext password against its stored PBKDF2 hash."""
    try:
        salt_hex, key_hex = stored_hash.split(':', 1)
        salt = bytes.fromhex(salt_hex)
        key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100_000)
        return hmac.compare_digest(key.hex(), key_hex)
    except Exception:
        return False


class DualStackThreadingServer(ThreadingHTTPServer):
    allow_reuse_address = True

class DashboardHandler(BaseHTTPRequestHandler):
    def get_query_param(self, name):
        if '?' in self.path:
            query = self.path.split('?')[1]
            params = urllib.parse.parse_qs(query)
            return params.get(name, [None])[0]
        return None

    def get_current_user(self):
        # Primary: session token-based auth (secure)
        token = self.get_query_param('token')
        if token:
            with _session_store_lock:
                user_id = _session_store.get(token)
            if user_id:
                users = load_users()
                user = users.get(user_id)
                if user:
                    role = user.get('role', 'USER')
                    status = user.get('status', 'PENDING')
                    if role in ('SUPER_ADMIN', 'ADMIN') or status == 'APPROVED':
                        return user
            return None

        # Legacy fallback: user_id query param (backward compat)
        user_id = self.get_query_param('user_id')
        if not user_id:
            return None
        users = load_users()
        user = users.get(str(user_id))
        if not user:
            return None
        role = user.get('role', 'USER')
        status = user.get('status', 'PENDING')
        if role in ('SUPER_ADMIN', 'ADMIN') or status == 'APPROVED':
            return user
        return None

    def send_json(self, data, status=200):
        body_bytes = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_HEAD(self):
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
        except Exception:
            pass

    def do_GET(self):
        try:
            path = self.path.split('?')[0]
            if path in ['/healthz', '/health', '/ping']:
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b"OK")
                return

            # Auth checks
            user = self.get_current_user()
            
            if path == '/api/me':
                if user:
                    self.send_json(user)
                else:
                    self.send_json({"error": "Unauthorized"}, 401)
                return

            if path == '/api/progress':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                task_id = query.get('task_id', [None])[0]
                if not task_id:
                    self.send_json({"error": "Missing task_id"}, 400)
                    return
                state = TASK_PROGRESS.get(task_id, {"status": "unknown", "percent": 0, "text": "Initializing..."})
                self.send_json(state)
                return

            if path == '/api/users':
                if not user or user.get('role') not in ['SUPER_ADMIN', 'ADMIN']:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                users_dict = load_users()
                users_list = list(users_dict.values())
                # filter out system config
                users_list = [u for u in users_list if isinstance(u, dict) and 'user_id' in u]
                users_list.sort(key=lambda x: x.get('joined_at', ''), reverse=True)
                self.send_json(users_list)
                return
                
            if path == '/api/settings':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                self.send_json(get_system_config())
                return
                
            if path == '/api/logs':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                log_file = "bot.log"
                if os.path.exists(log_file):
                    with open(log_file, "r", encoding="utf-8") as f:
                        # read last 500 lines
                        lines = f.readlines()
                        content = "".join(lines[-500:])
                else:
                    content = "No log file found."
                
                body_bytes = content.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return
                
            if path == '/api/schedules':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                user_id = str(user['user_id'])
                schedules = scheduler.get_schedules(user_id)
                self.send_json(schedules)
                return

            if path == '/api/my_facebook':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                from user_manager import get_user_facebook_pages
                user_id = str(user['user_id'])
                pages = get_user_facebook_pages(user_id)
                self.send_json({"fb_pages": pages})
                return

            if path == '/api/telegram_bots':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                default_bot = get_default_schedule_bot(str(user['user_id']))
                # If SUPER_ADMIN requests all, return all bots with user info
                if user.get('role') == 'SUPER_ADMIN' and self.get_query_param('all') == 'true':
                    all_bots = []
                    users_dict = load_users()
                    for uid, u_data in users_dict.items():
                        if isinstance(u_data, dict) and 'telegram_bots' in u_data:
                            for b in u_data['telegram_bots']:
                                b_copy = b.copy()
                                b_copy['owner_id'] = uid
                                b_copy['owner_name'] = u_data.get('first_name', '') + ' ' + u_data.get('last_name', '')
                                b_copy['owner_username'] = u_data.get('username', '')
                                all_bots.append(b_copy)
                    self.send_json({"bots": all_bots, "default_bot_token": default_bot})
                else:
                    user_id = str(user['user_id'])
                    bots = get_user_telegram_bots(user_id)
                    can_add_multiple = user.get("can_add_multiple_bots", False)
                    self.send_json({"bots": bots, "can_add_multiple": can_add_multiple, "default_bot_token": default_bot})
                return


            if path == '/':
                with open(os.path.join(os.path.dirname(__file__), "dashboard.html"), "r", encoding="utf-8") as f:
                    html_content = f.read()
                body_bytes = html_content.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body_bytes)))
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.send_header('Pragma', 'no-cache')
                self.send_header('Expires', '0')
                self.end_headers()
                self.wfile.write(body_bytes)
                return
                
            if path in ('/favicon.jpg', '/favicon.ico'):
                favicon_path = os.path.join(os.path.dirname(__file__), "favicon.jpg")
                if os.path.exists(favicon_path):
                    with open(favicon_path, "rb") as f:
                        body_bytes = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', str(len(body_bytes)))
                    self.end_headers()
                    self.wfile.write(body_bytes)
                    return

            if path.startswith('/api/yt/serve_merged'):
                yt_url = self.get_query_param('url')
                format_id = self.get_query_param('format_id')
                if not yt_url or not format_id:
                    self.send_response(400)
                    self.end_headers()
                    return
                
                import tempfile
                import subprocess
                import yt_dlp
                import shutil
                
                temp_dir = tempfile.mkdtemp()
                out_tmpl = os.path.join(temp_dir, 'video.%(ext)s')
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'format': format_id,
                    'outtmpl': out_tmpl,
                    'cookiefile': 'yt_cookies_for_render.txt' if os.path.exists('yt_cookies_for_render.txt') else None,
                    'merge_output_format': 'mp4',
                }
                proxies = {"http": "http://127.0.0.1:1080", "https": "http://127.0.0.1:1080"} if os.environ.get('USE_VPN_PROXY') == 'true' else None
                if proxies:
                    ydl_opts['proxy'] = "http://127.0.0.1:1080"
                    
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(yt_url, download=True)
                        file_path = ydl.prepare_filename(info)
                        if not os.path.exists(file_path):
                            file_path = os.path.splitext(file_path)[0] + '.mp4'
                        
                        if os.path.exists(file_path):
                            size = os.path.getsize(file_path)
                            self.send_response(200)
                            self.send_header('Content-Type', 'video/mp4')
                            self.send_header('Content-Length', str(size))
                            self.send_header('Content-Disposition', f'attachment; filename="video.mp4"')
                            self.end_headers()
                            with open(file_path, 'rb') as f:
                                shutil.copyfileobj(f, self.wfile)
                        else:
                            self.send_response(500)
                            self.end_headers()
                except Exception as e:
                    print(f"[web_dashboard] server merge failed: {e}")
                    try:
                        self.send_response(500)
                        self.end_headers()
                    except:
                        pass
                finally:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                return

            if path.startswith('/api/yt/serve_download'):
                filename = self.get_query_param('file')
                if not filename:
                    self.send_response(400)
                    self.end_headers()
                    return
                from utils import get_temp_dir
                import shutil
                temp_dir = get_temp_dir()
                # secure filename check
                if '/' in filename or '\\' in filename:
                    self.send_response(400)
                    self.end_headers()
                    return
                file_path = os.path.join(temp_dir, filename)
                if os.path.exists(file_path):
                    size = os.path.getsize(file_path)
                    self.send_response(200)
                    ext = os.path.splitext(filename)[1].lower()
                    ctype = 'video/webm' if ext == '.webm' else ('audio/mp4' if ext == '.m4a' else 'video/mp4')
                    self.send_header('Content-Type', ctype)
                    self.send_header('Content-Length', str(size))
                    self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
                    self.end_headers()
                    try:
                        with open(file_path, 'rb') as f:
                            shutil.copyfileobj(f, self.wfile)
                        # Optionally delete after serving to save space
                        os.remove(file_path)
                    except Exception as e:
                        print(f"Serve download failed: {e}")
                else:
                    self.send_response(404)
                    self.end_headers()
                return

            # ─── YouTube Stream Extractor (client downloads directly) ──────────
            if path == '/api/yt/info':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                yt_url = self.get_query_param('url')
                if not yt_url:
                    self.send_json({"error": "Missing url parameter"}, 400)
                    return
                try:
                    formats_out = []
                    title, thumbnail, duration, uploader = "Video", "", 0, "Unknown"
                    from pytubefix import YouTube
                    proxies = {"http": "http://127.0.0.1:1080", "https": "http://127.0.0.1:1080"} if os.environ.get('USE_VPN_PROXY') == 'true' else None
                    try:
                        yt = None
                        last_p_err = None
                        proxy_configs = [proxies, None] if proxies else [None]
                        for p_conf in proxy_configs:
                            if yt: break
                            for client_str in ['IOS', 'TV', 'ANDROID', 'WEB_CREATOR', 'WEB', 'MWEB', 'ANDROID_VR']:
                                try:
                                    _yt = YouTube(yt_url, client=client_str, proxies=p_conf)
                                    title = getattr(_yt, 'title', 'Video')
                                    thumbnail = getattr(_yt, 'thumbnail_url', '')
                                    duration = getattr(_yt, 'length', 0)
                                    uploader = getattr(_yt, 'author', 'Unknown')
                                    _ = _yt.streams # force fetch
                                    yt = _yt
                                    break
                                except Exception as e:
                                    last_p_err = e
                        
                        if not yt:
                            raise Exception(f"All pytubefix clients failed. Last error: {last_p_err}")
                            
                        seen = set()
                        for s in yt.streams:
                            fmt_id = str(s.itag)
                            ext = getattr(s, "subtype", "mp4")
                            is_video = getattr(s, "includes_video_track", getattr(s, "type", "") == "video")
                            is_audio = getattr(s, "includes_audio_track", getattr(s, "type", "") == "audio" or getattr(s, "is_progressive", False))
                            
                            if is_video and is_audio:
                                res = getattr(s, "resolution", "")
                                label = f"{res} {ext.upper()}" if res else f"{ext.upper()} (video+audio)"
                                key = (res, ext, 'va')
                                type_str = 'video'
                            elif is_video and not is_audio:
                                res = getattr(s, "resolution", "")
                                label = f"{res} {ext.upper()} (No Audio)" if res else f"{ext.upper()} (No Audio)"
                                key = (res, ext, 'v')
                                type_str = 'video'
                            elif is_audio and not is_video:
                                abr = getattr(s, "abr", "")
                                label = f"Audio {abr} {ext.upper()}" if abr else f"Audio {ext.upper()}"
                                key = (abr, ext, 'a')
                                type_str = 'audio'
                            else:
                                continue
                                
                            if key in seen:
                                continue
                            seen.add(key)
                            
                            if not hasattr(s, 'url') or not s.url:
                                continue

                            res_str = getattr(s, "resolution", "0p")
                            if not res_str: res_str = "0p"
                            
                            formats_out.append({
                                'format_id': fmt_id,
                                'label': label,
                                'ext': ext,
                                'height': int(res_str.replace("p", "")) if "p" in res_str else 0,
                                'filesize': getattr(s, 'filesize', getattr(s, 'filesize_approx', 0)),
                                'type': type_str,
                            })
                            
                            if is_video and not is_audio:
                                merge_key = (res_str, ext, 'v_merge')
                                if merge_key not in seen:
                                    seen.add(merge_key)
                                    formats_out.append({
                                        'format_id': f"SERVER_MERGE:{fmt_id}",
                                        'label': f"{res_str} {ext.upper()} (Auto-Merge)",
                                        'ext': 'mp4',
                                        'height': int(res_str.replace("p", "")) if "p" in res_str else 0,
                                        'filesize': 0,
                                        'type': 'video'
                                    })
                    except Exception as p_err:
                        print(f"[web_dashboard] pytubefix failed: {p_err}. Falling back to yt-dlp...")
                        import yt_dlp
                        ydl_opts = {
                            'quiet': True,
                            'no_warnings': True,
                            'format': 'all',
                            'cookiefile': 'yt_cookies_for_render.txt' if os.path.exists('yt_cookies_for_render.txt') else None,
                            'extractor_args': {
                                'youtube': {
                                    'player_client': ['android_vr', 'mweb', 'ios', 'tv'],
                                    'player_skip': ['webpage', 'configs']
                                }
                            }
                        }
                        if proxies:
                            ydl_opts['proxy'] = "http://127.0.0.1:1080"
                            
                        info = {}
                        try:
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                info = ydl.extract_info(yt_url, download=False)
                        except Exception as first_e:
                            if proxies:
                                print(f"[web_dashboard] yt info proxy failed: {first_e}. Retrying without proxy...")
                                del ydl_opts['proxy']
                                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                    info = ydl.extract_info(yt_url, download=False)
                            else:
                                raise

                        title = info.get('title', 'Video')
                        thumbnail = info.get('thumbnail', '')
                        duration = info.get('duration', 0)
                        uploader = info.get('uploader', 'Unknown')

                        formats_out = []
                        seen = set()
                        
                        for f in info.get('formats', []):
                            if not f.get('url'):
                                continue
                                
                            vcodec = f.get('vcodec') != 'none'
                            acodec = f.get('acodec') != 'none'
                            ext = f.get('ext', 'mp4')
                            height = f.get('height') or 0
                            filesize = f.get('filesize') or f.get('filesize_approx') or 0
                            fmt_id = str(f.get('format_id', ''))
                            
                            if vcodec and acodec:
                                type_str = 'video'
                                label = f"{height}p {ext.upper()}" if height else f"{ext.upper()} (video+audio)"
                                key = (height, ext, 'va')
                            elif acodec and not vcodec:
                                type_str = 'audio'
                                abr = f.get('abr')
                                label = f"Audio {abr}kbps {ext.upper()}" if abr else f"Audio {ext.upper()}"
                                key = (abr, ext, 'a')
                            elif vcodec and not acodec:
                                type_str = 'video'
                                label = f"{height}p {ext.upper()} (No Audio)" if height else f"{ext.upper()} (No Audio)"
                                key = (height, ext, 'v')
                            else:
                                continue
                                
                            if key in seen:
                                continue
                            seen.add(key)
                            
                            formats_out.append({
                                'format_id': fmt_id,
                                'label': label,
                                'ext': ext,
                                'height': height,
                                'filesize': filesize,
                                'type': type_str,
                            })
                            
                            if vcodec and not acodec:
                                merge_key = (height, ext, 'v_merge')
                                if merge_key not in seen:
                                    seen.add(merge_key)
                                    formats_out.append({
                                        'format_id': f"SERVER_MERGE:{fmt_id}",
                                        'label': f"{height}p {ext.upper()} (Auto-Merge)" if height else f"{ext.upper()} (Auto-Merge)",
                                        'ext': 'mp4',
                                        'height': height,
                                        'filesize': 0,
                                        'type': 'video'
                                    })

                    formats_out.sort(key=lambda x: (x['height'], x['type'] == 'video'), reverse=True)

                    self.send_json({
                        'title': title,
                        'thumbnail': thumbnail,
                        'duration': duration,
                        'uploader': uploader,
                        'formats': formats_out[:20],
                    })
                except Exception as e:
                    self.send_json({'error': f'Failed to fetch video info: {e}'}, 500)
                return

            self.send_response(404)
            self.end_headers()

        except Exception as e:
            print(f"HTTP GET Error: {e}")

    def do_POST(self):
        try:
            path = self.path.split('?')[0]
            user = self.get_current_user()
            
            # ─── Auth Endpoints (no session required) ──────────────────────────
            if path == '/api/auth/request-otp':
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                input_id = str(body.get('user_id', '')).strip()
                if not input_id:
                    self.send_json({'error': 'Telegram ID or Username is required.'}, 400)
                    return
                users = load_users()
                u = None
                user_id = input_id
                if input_id in users:
                    u = users[input_id]
                else:
                    search_un = input_id.lstrip('@').lower()
                    for uid, user_obj in users.items():
                        if isinstance(user_obj, dict) and str(user_obj.get('username', '')).lower() == search_un:
                            u = user_obj
                            user_id = uid
                            break
                if not u:
                    self.send_json({'error': 'User not found. Please start the bot first and ensure your ID or Username is correct.'}, 404)
                    return
                role = u.get('role', 'USER')
                status = u.get('status', 'PENDING')
                if status == 'BLOCKED':
                    self.send_json({'error': 'Your account is blocked. Please contact an admin.'}, 403)
                    return
                if role not in ('SUPER_ADMIN', 'ADMIN') and status != 'APPROVED':
                    self.send_json({'error': 'Your account is pending admin approval.'}, 403)
                    return
                
                otp = _generate_otp()
                with _otp_store_lock:
                    _otp_store[user_id] = {'code': otp, 'expiry': _time.time() + OTP_EXPIRY_SECONDS}
                ok = _send_otp_via_telegram(user_id, otp)
                if not ok:
                    self.send_json({'error': 'Failed to send OTP via Telegram. Check the bot is running and your User ID is correct.'}, 500)
                    return
                self.send_json({'success': True, 'message': 'OTP sent to your Telegram!'})
                return

            if path == '/api/auth/verify-otp':
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                input_id = str(body.get('user_id', '')).strip()
                otp_input = str(body.get('otp', '')).strip()
                if not input_id or not otp_input:
                    self.send_json({'error': 'User ID and OTP are required.'}, 400)
                    return
                users = load_users()
                user_id = input_id
                if input_id not in users:
                    search_un = input_id.lstrip('@').lower()
                    for uid, user_obj in users.items():
                        if isinstance(user_obj, dict) and str(user_obj.get('username', '')).lower() == search_un:
                            user_id = uid
                            break
                with _otp_store_lock:
                    entry = _otp_store.get(user_id)
                if not entry:
                    self.send_json({'error': 'No OTP was requested. Please request a new one.'}, 400)
                    return
                if _time.time() > entry['expiry']:
                    with _otp_store_lock:
                        _otp_store.pop(user_id, None)
                    self.send_json({'error': 'OTP has expired. Please request a new one.'}, 400)
                    return
                if otp_input != entry['code']:
                    self.send_json({'error': 'Incorrect OTP. Please try again.'}, 400)
                    return
                # OTP valid — clear it
                with _otp_store_lock:
                    _otp_store.pop(user_id, None)
                # Issue a short-lived token to proceed to set-password
                temp_token = _generate_token()
                with _otp_verified_lock:
                    _otp_verified_store[temp_token] = user_id
                self.send_json({'success': True, 'otp_verified_token': temp_token})
                return

            if path == '/api/auth/set-password':
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                otp_token = str(body.get('otp_verified_token', '')).strip()
                password = str(body.get('password', ''))
                if not otp_token or not password:
                    self.send_json({'error': 'Verification token and password are required.'}, 400)
                    return
                with _otp_verified_lock:
                    user_id = _otp_verified_store.get(otp_token)
                if not user_id:
                    self.send_json({'error': 'Invalid or expired verification token. Please start the OTP flow again.'}, 400)
                    return
                if len(password) < 6:
                    self.send_json({'error': 'Password must be at least 6 characters.'}, 400)
                    return
                users = load_users()
                if user_id not in users:
                    self.send_json({'error': 'User not found.'}, 404)
                    return
                users[user_id]['password_hash'] = _hash_password(password)
                save_users(users, sync_github=True)
                with _otp_verified_lock:
                    _otp_verified_store.pop(otp_token, None)
                session_token = _generate_token()
                with _session_store_lock:
                    _session_store[session_token] = user_id
                self.send_json({'success': True, 'session_token': session_token})
                return

            if path == '/api/auth/login':
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                input_id = str(body.get('user_id', '')).strip()
                password = str(body.get('password', ''))
                if not input_id or not password:
                    self.send_json({'error': 'Telegram ID or Username and password are required.'}, 400)
                    return
                users = load_users()
                u = None
                user_id = input_id
                if input_id in users:
                    u = users[input_id]
                else:
                    search_un = input_id.lstrip('@').lower()
                    for uid, user_obj in users.items():
                        if isinstance(user_obj, dict) and str(user_obj.get('username', '')).lower() == search_un:
                            u = user_obj
                            user_id = uid
                            break
                if not u:
                    self.send_json({'error': 'User not found.'}, 404)
                    return
                role = u.get('role', 'USER')
                status = u.get('status', 'PENDING')
                if status == 'BLOCKED':
                    self.send_json({'error': 'Your account is blocked. Contact an admin.'}, 403)
                    return
                if role not in ('SUPER_ADMIN', 'ADMIN') and status != 'APPROVED':
                    self.send_json({'error': 'Your account is pending approval.'}, 403)
                    return
                
                stored_hash = u.get('password_hash')
                if not stored_hash:
                    self.send_json({'error': 'No password set. Please use the OTP flow to create a password first.', 'need_otp': True}, 400)
                    return
                if not _verify_password(stored_hash, password):
                    self.send_json({'error': 'Incorrect password. Please try again.'}, 401)
                    return
                session_token = _generate_token()
                with _session_store_lock:
                    _session_store[session_token] = user_id
                self.send_json({'success': True, 'session_token': session_token})
                return

            if path == '/api/upload':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                
                content_type = self.headers.get('Content-Type', '')
                
                if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                    upload_dir = "/var/data/uploads"
                else:
                    upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
                os.makedirs(upload_dir, exist_ok=True)
                
                if 'multipart/form-data' in content_type:
                    form = cgi.FieldStorage(
                        fp=self.rfile,
                        headers=self.headers,
                        environ={'REQUEST_METHOD': 'POST',
                                 'CONTENT_TYPE': self.headers['Content-Type'],
                                 'CONTENT_LENGTH': self.headers.get('Content-Length', '0')}
                    )
                    
                    if 'file' not in form:
                        self.send_json({"error": "No file provided"}, 400)
                        return
                        
                    file_item = form['file']
                    if not file_item.file:
                        self.send_json({"error": "Invalid file"}, 400)
                        return
                    
                    filename = file_item.filename
                    if not filename:
                        filename = "upload.bin"
                    safe_name = f"{uuid.uuid4().hex}_{os.path.basename(filename)}"
                    save_path = os.path.join(upload_dir, safe_name)
                    
                    with open(save_path, "wb") as f:
                        while True:
                            chunk = file_item.file.read(8192)
                            if not chunk:
                                break
                            f.write(chunk)
                else:
                    # Raw binary upload
                    filename = self.get_query_param('filename')
                    if not filename:
                        filename = "upload.bin"
                    
                    safe_name = f"{uuid.uuid4().hex}_{os.path.basename(filename)}"
                    save_path = os.path.join(upload_dir, safe_name)
                    
                    content_length = int(self.headers.get('Content-Length', 0))
                    
                    with open(save_path, "wb") as f:
                        bytes_left = content_length
                        while bytes_left > 0:
                            chunk = self.rfile.read(min(8192, bytes_left))
                            if not chunk:
                                break
                            f.write(chunk)
                            bytes_left -= len(chunk)
                            
                self.send_json({"success": True, "file_path": f"uploads/{safe_name}"})
                return

            if path == '/api/toggle':
                if not user or user.get('role') not in ['SUPER_ADMIN', 'ADMIN']:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_id = data.get('target_id')
                updated_user = toggle_user_status(target_id)
                self.send_json({"success": True, "user": updated_user})
                return
                
            if path == '/api/assign_server':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_id = data.get('target_id')
                server_id = data.get('server_id')
                
                from user_manager import update_user_assigned_server
                updated_user = update_user_assigned_server(target_id, server_id)
                self.send_json({"success": True, "user": updated_user})
                return
                
            if path == '/api/fb_access':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_id = data.get('target_id')
                page_id = data.get('page_id')
                from user_manager import toggle_fb_page_access
                updated_user = toggle_fb_page_access(target_id, page_id)
                self.send_json({"success": True, "user": updated_user})
                return
                
            if path == '/api/role':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_id = data.get('target_id')
                new_role = data.get('role')
                updated_user = update_user_role(target_id, new_role)
                self.send_json({"success": True, "user": updated_user})
                return
                
            if path == '/api/remove':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_id = data.get('target_id')
                from user_manager import remove_user
                success = remove_user(target_id)
                self.send_json({"success": success})
                return
                
            if path == '/api/settings':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                admin_contact = data.get('admin_contact')
                if admin_contact is not None:
                    update_system_config("admin_contact", admin_contact)
                    
                otp_bot_token = data.get('otp_bot_token')
                if otp_bot_token is not None:
                    update_system_config("otp_bot_token", otp_bot_token)
                    
                otp_bot_restricted_mode = data.get('otp_bot_restricted_mode')
                if otp_bot_restricted_mode is not None:
                    update_system_config("otp_bot_restricted_mode", otp_bot_restricted_mode)
                    
                webapp_url = data.get('webapp_url')
                if webapp_url is not None:
                    update_system_config("webapp_url", webapp_url)
                    
                self.send_json({"success": True})
                return
                
            if path == '/api/schedules':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                user_id = str(user['user_id'])
                idx = data.pop('idx', None)
                if idx is not None:
                    scheduler.update_schedule(user_id, int(idx), data)
                else:
                    scheduler.add_schedule(user_id, data)
                self.send_json({"success": True})
                return

            if path == '/api/my_facebook':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                pages = data.get('fb_pages', [])
                from user_manager import update_user_facebook
                update_user_facebook(user['user_id'], pages)
                self.send_json({"success": True})
                return

            if path == '/api/admin/delete_facebook_page':
                if not user or user.get('role') not in ['SUPER_ADMIN', 'ADMIN']:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_user_id = data.get('user_id')
                page_id = data.get('page_id')
                if not target_user_id or not page_id:
                    self.send_json({"error": "Missing parameters"}, 400)
                    return
                from user_manager import delete_user_facebook_page
                success = delete_user_facebook_page(target_user_id, page_id)
                if success:
                    self.send_json({"success": True})
                else:
                    self.send_json({"error": "Failed to delete or not found"}, 404)
                return

            if path == '/api/telegram_bots':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                token = data.get('bot_token', '').strip()
                if not token:
                    self.send_json({"error": "Bot token is required"}, 400)
                    return
                
                # Check limits
                user_id = str(user['user_id'])
                bots = get_user_telegram_bots(user_id)
                max_bots = user.get("max_custom_bots", 1)
                
                if user.get("role") != "SUPER_ADMIN" and len(bots) >= max_bots:
                    self.send_json({"error": f"You can only add {max_bots} bot(s). Contact admin for more."}, 403)
                    return
                    
                import requests
                try:
                    res = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
                    if res.status_code == 200:
                        b_data = res.json().get("result", {})
                        bot_username = b_data.get("username", "")
                        bot_name = b_data.get("first_name", "")
                        
                        bot_obj = {
                            "bot_token": token,
                            "bot_username": bot_username,
                            "bot_name": bot_name,
                            "status": "APPROVED",
                        }
                        if add_user_telegram_bot(user_id, bot_obj):
                            # Auto-start the newly approved bot dynamically
                            import custom_bots_manager
                            if hasattr(custom_bots_manager, 'main_loop') and custom_bots_manager.main_loop:
                                import asyncio
                                asyncio.run_coroutine_threadsafe(custom_bots_manager.start_custom_bot(user_id, token), custom_bots_manager.main_loop)
                                
                            self.send_json({"success": True})
                        else:
                            self.send_json({"error": "Failed to save bot"}, 500)
                    else:
                        self.send_json({"error": "Invalid bot token"}, 400)
                except Exception as e:
                    self.send_json({"error": f"Error verifying token: {e}"}, 500)
                return

            if path == '/api/telegram_bots/profile':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                token = data.get('bot_token')
                name = data.get('name')
                desc = data.get('description')
                short_desc = data.get('short_description')
                
                import requests
                try:
                    if name:
                        requests.post(f"https://api.telegram.org/bot{token}/setMyName", json={"name": name})
                    if desc:
                        requests.post(f"https://api.telegram.org/bot{token}/setMyDescription", json={"description": desc})
                    if short_desc:
                        requests.post(f"https://api.telegram.org/bot{token}/setMyShortDescription", json={"short_description": short_desc})
                    
                    update_user_telegram_bot(user['user_id'], token, {"bot_name": name or ""})
                    self.send_json({"success": True})
                except Exception as e:
                    self.send_json({"error": str(e)}, 500)
                return

            if path == '/api/telegram_bots/admin/status':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_user_id = data.get('user_id')
                token = data.get('bot_token')
                status = data.get('status')
                if update_user_telegram_bot(target_user_id, token, {"status": status}):
                    import custom_bots_manager
                    import asyncio
                    if status == "APPROVED":
                        if custom_bots_manager.main_loop:
                            asyncio.run_coroutine_threadsafe(
                                custom_bots_manager.start_custom_bot(target_user_id, token), 
                                custom_bots_manager.main_loop
                            )
                    else:
                        if custom_bots_manager.main_loop:
                            asyncio.run_coroutine_threadsafe(
                                custom_bots_manager.stop_custom_bot(token), 
                                custom_bots_manager.main_loop
                            )
                    self.send_json({"success": True})
                else:
                    self.send_json({"error": "Failed to update"}, 500)
                return

            if path == '/api/telegram_bots/admin/otp':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_user_id = data.get('user_id')
                token = data.get('bot_token')
                can_send_otp = data.get('can_send_otp')
                if update_user_telegram_bot(target_user_id, token, {"can_send_otp": can_send_otp}):
                    self.send_json({"success": True})
                else:
                    self.send_json({"error": "Failed to update"}, 500)
                return

            if path == '/api/telegram_bots/admin/multiple':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_user_id = data.get('user_id')
                max_bots = data.get('max_bots', 1)
                new_val = set_user_max_custom_bots(target_user_id, max_bots)
                self.send_json({"success": True, "max_custom_bots": new_val})
                return

            if path == '/api/telegram_bots/default':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_user_id = str(user['user_id'])
                bot_token = data.get('bot_token', 'main_bot')
                set_default_schedule_bot(target_user_id, bot_token)
                self.send_json({"success": True})
                return

            def _background_yt_device_download(task_id, yt_url, format_id):
                print(f"[device_download] Starting download for {yt_url}")
                from utils import get_temp_dir
                from pytubefix import YouTube
                import subprocess
                import time
                
                temp_dir = get_temp_dir()
                downloaded_file = None

                proxies = {"http": "http://127.0.0.1:1080", "https": "http://127.0.0.1:1080"} if os.environ.get('USE_VPN_PROXY') == 'true' else None
                proxy_configs = [proxies, None] if proxies else [None]

                yt = None
                for p_conf in proxy_configs:
                    if yt: break
                    for client_str in ['ANDROID_VR', 'MWEB', 'IOS', 'TV', 'ANDROID', 'WEB_CREATOR', 'WEB']:
                        try:
                            _yt = YouTube(yt_url, client=client_str, proxies=p_conf)
                            _ = _yt.streams
                            yt = _yt
                            break
                        except Exception:
                            pass

                if not yt:
                    print("[device_download] pytubefix failed to fetch info")
                    if task_id: TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": "Failed to fetch video info"}
                    return

                def on_progress(stream, chunk, bytes_remaining):
                    if not task_id: return
                    total = stream.filesize
                    if total > 0:
                        percent = int(((total - bytes_remaining) / total) * 100)
                        TASK_PROGRESS[task_id] = {"status": "running", "percent": percent, "text": f"Downloading {percent}%"}

                yt.register_on_progress_callback(on_progress)

                try:
                    if format_id.startswith('SERVER_MERGE:'):
                        v_itag = int(format_id.split(':', 1)[1])
                        v_stream = yt.streams.get_by_itag(v_itag)
                        a_stream = yt.streams.get_audio_only()
                        
                        if not v_stream or not a_stream:
                            raise Exception("Missing streams for merge")

                        v_path = v_stream.download(output_path=temp_dir, filename=f"temp_v_{task_id}.mp4")
                        a_path = a_stream.download(output_path=temp_dir, filename=f"temp_a_{task_id}.m4a")

                        if task_id: TASK_PROGRESS[task_id] = {"status": "running", "percent": 99, "text": "Merging audio and video..."}
                        out_path = os.path.join(temp_dir, f"device_{int(time.time())}_{task_id}.mp4")
                        import imageio_ffmpeg
                        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                        subprocess.run([ffmpeg_exe, '-y', '-i', v_path, '-i', a_path, '-c:v', 'copy', '-c:a', 'aac', out_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        
                        if os.path.exists(v_path): os.remove(v_path)
                        if os.path.exists(a_path): os.remove(a_path)
                        downloaded_file = out_path
                    else:
                        itag = int(format_id)
                        stream = yt.streams.get_by_itag(itag)
                        if not stream:
                            raise Exception("Stream not found")
                        ext = getattr(stream, 'subtype', 'mp4')
                        downloaded_file = stream.download(output_path=temp_dir, filename=f"device_{int(time.time())}_{task_id}.{ext}")
                        
                except Exception as e:
                    print(f"[device_download] Download failed: {e}")
                    
                if not downloaded_file or not os.path.exists(downloaded_file):
                    print("[device_download] Download failed to produce a file.")
                    if task_id: TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": "Download failed"}
                    return

                if task_id:
                    import urllib.parse
                    filename = os.path.basename(downloaded_file)
                    download_url = f"/api/yt/serve_download?file={urllib.parse.quote(filename)}"
                    TASK_PROGRESS[task_id] = {"status": "done", "percent": 100, "text": "Download ready!", "download_url": download_url}

            if path == '/api/yt/download_device':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                yt_url = body.get('url', '').strip()
                format_id = body.get('format_id')
                task_id = body.get('task_id')
                
                if not yt_url or not format_id or not task_id:
                    self.send_json({'error': 'Missing parameters'}, 400)
                    return
                    
                import threading
                threading.Thread(target=_background_yt_device_download, args=(task_id, yt_url, format_id)).start()
                self.send_json({'success': True})
                return

            if path == '/api/yt/send_telegram':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                yt_url = body.get('url', '').strip()
                format_id = body.get('format_id', 'bestvideo+bestaudio/best')
                num_clips = body.get('num_clips', 1)
                task_id = body.get('task_id')
                
                if not yt_url:
                    self.send_json({'error': 'Missing url'}, 400)
                    return
                    
                import threading
                threading.Thread(target=_background_yt_telegram_send, args=(str(user['user_id']), user.get('role'), yt_url, format_id, num_clips, task_id)).start()
                
                self.send_json({'success': True})
                return

            if path == '/api/yt/stream':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                yt_url = body.get('url', '').strip()
                format_id = body.get('format_id', 'bestvideo+bestaudio/best')
                if not yt_url:
                    self.send_json({'error': 'Missing url'}, 400)
                    return
                try:
                    import urllib.parse
                    if format_id.startswith('SERVER_MERGE:'):
                        real_format = format_id.split(':', 1)[1] + "+bestaudio/best"
                        self.send_json({
                            'stream_url': f"/api/yt/serve_merged?url={urllib.parse.quote(yt_url)}&format_id={urllib.parse.quote(real_format)}",
                            'filename': f"merged_video.mp4",
                            'ext': 'mp4',
                            'filesize': 0,
                            'http_headers': {},
                        })
                        return
                    
                    from pytubefix import YouTube
                    proxies = {"http": "http://127.0.0.1:1080", "https": "http://127.0.0.1:1080"} if os.environ.get('USE_VPN_PROXY') == 'true' else None
                    try:
                        yt = None
                        last_p_err = None
                        proxy_configs = [proxies, None] if proxies else [None]
                        for p_conf in proxy_configs:
                            if yt: break
                            for client_str in ['IOS', 'TV', 'ANDROID', 'WEB_CREATOR', 'WEB', 'MWEB', 'ANDROID_VR']:
                                try:
                                    _yt = YouTube(yt_url, client=client_str, proxies=p_conf)
                                    _ = _yt.streams # force fetch
                                    yt = _yt
                                    break
                                except Exception as e:
                                    last_p_err = e
                                
                        if not yt:
                            raise Exception(f"All pytubefix clients failed. Last error: {last_p_err}")
                            
                        requested_fmt = None
                        for s in yt.streams:
                            if str(s.itag) == format_id and getattr(s, 'url', None):
                                requested_fmt = s
                                break
                        if not requested_fmt:
                            requested_fmt = yt.streams.get_highest_resolution()
                        if not requested_fmt or not getattr(requested_fmt, 'url', None):
                            raise Exception("No direct stream URL found via pytubefix")
                        
                        ext = getattr(requested_fmt, 'subtype', 'mp4')
                        filesize = getattr(requested_fmt, 'filesize', getattr(requested_fmt, 'filesize_approx', 0))
                        
                        self.send_json({
                            'stream_url': requested_fmt.url,
                            'filename': f"{yt.title}.{ext}",
                            'ext': ext,
                            'filesize': filesize,
                            'http_headers': {},
                        })
                        return
                    except Exception as p_err:
                        print(f"[web_dashboard] pytubefix stream failed: {p_err}. Falling back to yt-dlp...")
                        import yt_dlp
                        ydl_opts = {
                            'quiet': True,
                            'no_warnings': True,
                            'format': format_id,
                            'cookiefile': 'yt_cookies_for_render.txt' if os.path.exists('yt_cookies_for_render.txt') else None,
                            'extractor_args': {
                                'youtube': {
                                    'player_client': ['android_vr', 'mweb', 'ios', 'tv'],
                                    'player_skip': ['webpage', 'configs']
                                }
                            }
                        }
                        if proxies:
                            ydl_opts['proxy'] = "http://127.0.0.1:1080"
                            
                        info = {}
                        try:
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                info = ydl.extract_info(yt_url, download=False)
                        except Exception as first_e:
                            if proxies:
                                print(f"[web_dashboard] yt stream proxy failed: {first_e}. Retrying without proxy...")
                                del ydl_opts['proxy']
                                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                    info = ydl.extract_info(yt_url, download=False)
                            else:
                                raise

                        # Extract best stream URL
                        requested_fmt = None
                        if 'requested_formats' in info:
                            for fmt in info['requested_formats']:
                                if fmt.get('url'):
                                    requested_fmt = fmt
                                    break
                        elif info.get('url'):
                            requested_fmt = info

                        if not requested_fmt or not requested_fmt.get('url'):
                            self.send_json({'error': 'No direct stream URL found for this format.'}, 404)
                            return

                        ext = requested_fmt.get('ext', 'mp4')
                        filesize = requested_fmt.get('filesize') or requested_fmt.get('filesize_approx') or 0
                        title = info.get('title', 'Video')

                        self.send_json({
                            'stream_url': requested_fmt['url'],
                            'filename': f"{title}.{ext}",
                            'ext': ext,
                            'filesize': filesize,
                            'http_headers': requested_fmt.get('http_headers', {}),
                        })
                except Exception as e:
                    self.send_json({'error': f'Stream extraction failed: {e}'}, 500)
                return


            # ── Stop a running scheduled task ─────────────────────────────────
            if path == '/api/schedules/stop':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                task_id = str(body.get('task_id', '')).strip()
                if not task_id:
                    self.send_json({'error': 'Missing task_id'}, 400)
                    return
                
                # Import scheduler to use request_stop
                import scheduler as sched_module
                stopped = sched_module.request_stop(task_id)
                if stopped:
                    self.send_json({'success': True, 'message': 'Stop signal sent to the running task.'})
                else:
                    # Task may have already finished; just mark it stopped in storage
                    data = sched_module.load_all_schedules()
                    uid = str(user['user_id'])
                    found = False
                    for t in data.get(uid, []):
                        if t.get('id') == task_id:
                            t['status'] = '⏹ Stopped by user'
                            found = True
                            break
                    if found:
                        sched_module.save_all_schedules(data)
                    self.send_json({'success': found, 'message': 'Task was not actively running; status updated.' if found else 'Task not found.'})
                return

            self.send_response(404)
            self.end_headers()
        except Exception as e:
            print(f"HTTP POST Error: {e}")
            try:
                self.send_response(400)
                self.end_headers()
            except Exception:
                pass

    def do_DELETE(self):
        try:
            path = self.path.split('?')[0]
            user = self.get_current_user()
            if path == '/api/schedules':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                idx = self.get_query_param('idx')
                if idx is not None:
                    user_id = str(user['user_id'])
                    scheduler.delete_schedule(user_id, int(idx))
                    self.send_json({"success": True})
                return

            if path == '/api/telegram_bots':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                token = self.get_query_param('bot_token')
                target_user_id = self.get_query_param('target_user_id')
                if token:
                    user_id = str(user['user_id'])
                    # If target_user_id is provided, verify caller is super admin
                    if target_user_id:
                        if user.get('role') == 'SUPER_ADMIN':
                            user_id = str(target_user_id)
                        else:
                            self.send_json({"error": "Unauthorized to delete other users' bots"}, 403)
                            return
                            
                    from user_manager import delete_user_telegram_bot
                    if delete_user_telegram_bot(user_id, token):
                        import custom_bots_manager
                        import asyncio
                        if custom_bots_manager.main_loop:
                            asyncio.run_coroutine_threadsafe(
                                custom_bots_manager.stop_custom_bot(token), 
                                custom_bots_manager.main_loop
                            )
                        self.send_json({"success": True})
                    else:
                        self.send_json({"error": "Failed to delete bot"}, 500)
                else:
                    self.send_json({"error": "Missing bot_token parameter"}, 400)
                return
            self.send_response(404)
            self.end_headers()
        except Exception as e:
             print(f"HTTP DELETE Error: {e}")

    def log_message(self, format, *args):
        return

def run_dashboard_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        server = DualStackThreadingServer(('0.0.0.0', port), DashboardHandler)
        print(f"🚀 Web Dashboard Threading Server running on port {port}...")
        server.serve_forever()
    except Exception as e:
        print(f"Fatal Web Dashboard Server Error: {e}")
