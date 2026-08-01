import os
import json
import threading
import time
from datetime import datetime
import asyncio
from pyrogram import Client

import redis

REDIS_URL = os.environ.get("REDIS_URL")
redis_client = None
if REDIS_URL:
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        # Test connection
        redis_client.ping()
        print("Connected to Redis for scheduler! ✅")
    except Exception as e:
        print(f"Failed to connect to Redis: {e}")
        redis_client = None

if os.path.exists("/var/data") and os.path.isdir("/var/data"):
    SCHEDULES_FILE = "/var/data/schedules.json"
else:
    SCHEDULES_FILE = os.path.join(os.path.dirname(__file__), "schedules.json")
_lock = threading.Lock()

def load_all_schedules():
    with _lock:
        if redis_client:
            try:
                data = redis_client.get("schedules")
                if data:
                    return json.loads(data)
            except Exception as e:
                print(f"Error loading schedules from Redis: {e}")
                
        # Fallback to local file
        if not os.path.exists(SCHEDULES_FILE):
            return {}
        try:
            with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
                file_data = json.load(f)
                # Auto-migrate to Redis if possible
                if redis_client and file_data:
                    try:
                        redis_client.set("schedules", json.dumps(file_data, ensure_ascii=False))
                        print("Migrated local schedules.json to Redis! ✅")
                    except Exception as e:
                        print(f"Error migrating schedules to Redis: {e}")
                return file_data
        except:
            return {}

def save_all_schedules(data):
    with _lock:
        # Save to Redis
        if redis_client:
            try:
                redis_client.set("schedules", json.dumps(data, ensure_ascii=False))
            except Exception as e:
                print(f"Error saving schedules to Redis: {e}")
                
        # Also backup to local file
        try:
            with open(SCHEDULES_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving schedules to file: {e}")

def get_schedules(user_id):
    data = load_all_schedules()
    return data.get(str(user_id), [])

def add_schedule(user_id, schedule_data):
    data = load_all_schedules()
    user_id = str(user_id)
    if user_id not in data:
        data[user_id] = []
    
    import uuid
    schedule_data['id'] = str(uuid.uuid4())
    schedule_data['last_run'] = None
    schedule_data['status'] = 'Pending'
    data[user_id].append(schedule_data)
    save_all_schedules(data)

def delete_schedule(user_id, index):
    data = load_all_schedules()
    user_id = str(user_id)
    if user_id in data and 0 <= index < len(data[user_id]):
        data[user_id].pop(index)
        save_all_schedules(data)

def update_schedule(user_id, index, new_data):
    data = load_all_schedules()
    user_id = str(user_id)
    if user_id in data and 0 <= index < len(data[user_id]):
        old_id = data[user_id][index].get('id')
        new_data['id'] = old_id
        new_data['last_run'] = None
        new_data['status'] = 'Pending'
        data[user_id][index] = new_data
        save_all_schedules(data)

def clear_user_schedules(user_id):
    data = load_all_schedules()
    user_id = str(user_id)
    if user_id in data:
        del data[user_id]
        save_all_schedules(data)

# --- Background Task Runner ---
import time
import asyncio

# ── Stop-flag registry ─────────────────────────────────────────────────────────
# Maps task_id -> asyncio.Event that signals cancellation.
# Set by request_stop(); checked by the running coroutine.
_RUNNING_TASKS: dict = {}   # {task_id: asyncio.Event}
_RUNNING_LOCK = threading.Lock()

class CancellationToken:
    """Thin wrapper around asyncio.Event used to cancel a running task."""
    def __init__(self):
        self._event = asyncio.Event()  # set() when stop requested

    def request_stop(self):
        self._event.set()

    def is_stop_requested(self) -> bool:
        return self._event.is_set()

    async def check(self):
        """Raise asyncio.CancelledError if stop has been requested."""
        if self._event.is_set():
            raise asyncio.CancelledError("Stop requested by user.")

def request_stop(task_id: str) -> bool:
    """Signal a running task to stop. Returns True if the task was found."""
    with _RUNNING_LOCK:
        token = _RUNNING_TASKS.get(task_id)
    if token:
        token.request_stop()
        return True
    return False

def is_task_running(task_id: str) -> bool:
    with _RUNNING_LOCK:
        return task_id in _RUNNING_TASKS


class ScheduleAnimator:
    """
    Runs a background asyncio task that edits a Telegram message every 2 seconds
    to show animated dots, so users know the scheduled job is still running.
    Cycles:  ⏳ Running .  →  ⏳ Running ..  →  ⏳ Running ...
    Also checks a CancellationToken and raises CancelledError if set.
    """
    FRAMES = [".", "..", "..."]

    def __init__(self, msg, header: str, interval: float = 2.0,
                 cancel_token: CancellationToken = None):
        self.msg = msg
        self.header = header
        self.interval = interval
        self.cancel_token = cancel_token
        self._task = None
        self._stopped = False
        self._current_detail = ""   # overwritten by ThrottleProgress

    def set_detail(self, text: str):
        """Called by ThrottleProgress to update the sub-line shown under the header."""
        self._current_detail = text

    async def _run(self):
        frame_idx = 0
        while not self._stopped:
            # Check cancellation before each edit
            if self.cancel_token and self.cancel_token.is_stop_requested():
                raise asyncio.CancelledError("Stop requested by user.")
            dots = self.FRAMES[frame_idx % len(self.FRAMES)]
            frame_idx += 1
            detail = f"\n⏳ {self._current_detail}" if self._current_detail else ""
            try:
                await self.msg.edit_text(
                    f"{self.header}\n"
                    f"🔄 **Running{dots}**"
                    f"{detail}"
                )
            except Exception:
                pass
            await asyncio.sleep(self.interval)

    def start(self):
        self._stopped = False
        self._task = asyncio.ensure_future(self._run())

    async def stop(self, final_text: str = None):
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if final_text:
            try:
                await self.msg.edit_text(final_text)
            except Exception:
                pass


class ThrottleProgress:
    def __init__(self, msg, prefix, animator: ScheduleAnimator = None):
        self.msg = msg
        self.prefix = prefix
        self.last_update = 0
        self.last_text = ""
        self.animator = animator
        
    def __call__(self, text_or_cur, total=None):
        now = time.time()
        text = str(text_or_cur)
        if total is not None:
            pct = (float(text_or_cur) / float(total)) * 100
            text = f"Uploading... {pct:.1f}%"
            
        is_major_update = "downloading" not in text.lower() and "uploading" not in text.lower()
        
        if text != self.last_text and (now - self.last_update > 2.5 or is_major_update):
            self.last_update = now
            self.last_text = text
            # Feed the text to the animator so it shows it under the header
            if self.animator:
                self.animator.set_detail(text)
            else:
                try:
                    asyncio.create_task(self.msg.edit_text(f"{self.prefix}\n⏳ {text}"))
                except:
                    pass

async def execute_task(app, user_id, task):
    task_id = task.get("id")

    # Register a cancellation token so the dashboard can stop this task
    cancel_token = CancellationToken()
    if task_id:
        with _RUNNING_LOCK:
            _RUNNING_TASKS[task_id] = cancel_token

    def update_status(status_msg):
        if not task_id: return
        data = load_all_schedules()
        for t in data.get(str(user_id), []):
            if t.get("id") == task_id:
                t["status"] = status_msg
                save_all_schedules(data)
                break

    async def check_cancel():
        """Raise CancelledError immediately if the user pressed Stop."""
        await cancel_token.check()

    custom_client = None
    target_app = app
    try:
        user_id_int = int(user_id)
        
        task_type = task.get("task_type")
        task_data_str = task.get("data", "{}")
        
        try:
            task_data = json.loads(task_data_str)
        except:
            task_data = {}
            
        # Determine which bot to use
        bot_token_to_use = task_data.get("bot_token")
        if bot_token_to_use == "main_bot":
            bot_token_to_use = None # force main bot
        elif not bot_token_to_use:
            # fallback for old schedules
            from user_manager import get_active_user_bot
            custom_bot = get_active_user_bot(user_id_int)
            bot_token_to_use = custom_bot.get("bot_token") if custom_bot else None

        if bot_token_to_use:
            from custom_bots_manager import get_active_custom_bots
            active_bots = get_active_custom_bots()
            if bot_token_to_use in active_bots:
                target_app = active_bots[bot_token_to_use]
            else:
                api_id = os.environ.get("API_ID")
                api_hash = os.environ.get("API_HASH")
                if api_id and api_hash:
                    try:
                        custom_client = Client(
                            f"custom_bot_{bot_token_to_use[:10]}",
                            bot_token=bot_token_to_use,
                            api_id=int(api_id),
                            api_hash=api_hash,
                            in_memory=True
                        )
                        await custom_client.start()
                        target_app = custom_client
                    except Exception as e:
                        print(f"Failed to start custom bot client for {user_id}: {e}")
                        target_app = app
                    
        

        
        task_success = False
        
        if task_type == "send_message":
            msg = await target_app.send_message(user_id_int, "🕐 **Scheduled Message**\n🔄 **Running...**")
            anim = ScheduleAnimator(msg, "🕐 **Scheduled Message**", cancel_token=cancel_token)
            anim.start()
            try:
                content = task_data.get("message", "Hello! This is your scheduled automated message.")
                await target_app.send_message(user_id_int, content)
                task_success = True
                await anim.stop("✅ Scheduled message sent!")
            except Exception as e:
                await anim.stop(f"❌ Failed to send message: {e}")
            
        elif task_type == "daily_summary":
            msg = await target_app.send_message(user_id_int, "📊 **Daily Summary**\n🔄 **Running...**")
            anim = ScheduleAnimator(msg, "📊 **Daily Summary**", cancel_token=cancel_token)
            anim.start()
            try:
                summary = "📊 **Daily Bot Summary**\nYour scheduled task ran successfully.\nNo new downloads today."
                await target_app.send_message(user_id_int, summary)
                task_success = True
                await anim.stop("✅ Daily summary sent!")
            except Exception as e:
                await anim.stop(f"❌ Failed: {e}")
            
        elif task_type == "download":
            url = task_data.get("url", "")
            prefix = f"📥 **Scheduled Download**\n🔗 {url}"
            msg = await target_app.send_message(user_id_int, f"{prefix}\n🔄 **Running...**")
            anim = ScheduleAnimator(msg, prefix, cancel_token=cancel_token)
            anim.start()
            import asyncio, os
            from downloader import download_media
            
            prog = ThrottleProgress(msg, prefix, anim)
            if url.startswith("uploads/"):
                if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                    input_path = os.path.join("/var/data", url)
                else:
                    input_path = os.path.abspath(os.path.join(os.path.dirname(__file__), url))
            else:
                from plugins.core import is_telegram_link, download_telegram_post_media
                if is_telegram_link(url):
                    input_path = await download_telegram_post_media(target_app, url, False, prog, requesting_user_id=user_id_int)
                else:
                    dl_res = await asyncio.to_thread(download_media, url, False, prog)
                    input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res
            
            if input_path and not input_path.startswith("ERROR") and input_path not in ["BOT_DETECTED", "TOO_LARGE"]:
                try:
                    await target_app.send_video(user_id_int, input_path, progress=prog)
                except:
                    await target_app.send_document(user_id_int, input_path, progress=prog)
                if os.path.exists(input_path): os.remove(input_path)
                task_success = True
                await anim.stop("✅ Download complete!")
            else:
                await anim.stop(f"❌ Failed to download: {input_path}")
            
        elif task_type == "clip":
            url = task_data.get("url", "")
            mode = task_data.get("clip_mode", "parts")
            thumb = task_data.get("thumbnail")
            prefix = f"✂️ **Scheduled Video Clip**\n🔗 {url}"
            msg = await target_app.send_message(user_id_int, f"{prefix}\n🔄 **Running...**")
            anim = ScheduleAnimator(msg, prefix, cancel_token=cancel_token)
            anim.start()
            
            import asyncio, os
            from downloader import download_media
            from converter import clip_video_into_parts, clip_video_by_duration, prepare_telegram_thumbnail
            from utils import cleanup_file
            
            prog = ThrottleProgress(msg, prefix, anim)
            if url.startswith("uploads/"):
                if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                    input_path = os.path.join("/var/data", url)
                else:
                    input_path = os.path.abspath(os.path.join(os.path.dirname(__file__), url))
            else:
                from plugins.core import is_telegram_link, download_telegram_post_media
                if is_telegram_link(url):
                    input_path = await download_telegram_post_media(target_app, url, False, prog, requesting_user_id=user_id_int)
                else:
                    dl_res = await asyncio.to_thread(download_media, url, False, prog)
                    input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res
            
            if input_path and not input_path.startswith("ERROR") and input_path not in ["BOT_DETECTED", "TOO_LARGE"]:
                if thumb and thumb.startswith("uploads/"):
                    if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                        abs_thumb = os.path.join("/var/data", thumb)
                    else:
                        abs_thumb = os.path.abspath(os.path.join(os.path.dirname(__file__), thumb))
                else:
                    abs_thumb = thumb
                tg_thumb = await asyncio.to_thread(prepare_telegram_thumbnail, abs_thumb) if abs_thumb and os.path.exists(abs_thumb) else None
                clips = []
                try:
                    if mode == "parts":
                        val = int(task_data.get("parts", "2") or "2")
                        clips = await asyncio.to_thread(clip_video_into_parts, input_path, val, 0, prog)
                    elif mode == "duration":
                        val = int(task_data.get("duration", "60") or "60")
                        clips = await asyncio.to_thread(clip_video_by_duration, input_path, val, 0, prog)
                    elif mode == "start_end":
                        start = task_data.get("start", "00:00:00")
                        end = task_data.get("end", "00:01:00")
                        out = input_path + "_clip.mp4"
                        import subprocess
                        subprocess.run(["ffmpeg", "-y", "-i", input_path, "-ss", start, "-to", end, "-c", "copy", out], capture_output=True)
                        if os.path.exists(out): clips = [out]
                except Exception as e:
                    await anim.stop(f"❌ Error during clipping: {e}")
                
                cleanup_file(input_path)
                if clips:
                    all_clips = []
                    if isinstance(clips, dict):
                        if clips.get("intro"): all_clips.append(clips["intro"])
                        if clips.get("main_clips"): all_clips.extend(clips["main_clips"])
                        if clips.get("outro"): all_clips.append(clips["outro"])
                    else:
                        all_clips = clips
                    for c in all_clips:
                        send_kwargs = {}
                        if tg_thumb and os.path.exists(tg_thumb):
                            send_kwargs["thumb"] = tg_thumb
                        try:
                            await target_app.send_video(user_id_int, c, progress=prog, **send_kwargs)
                        except:
                            await target_app.send_document(user_id_int, c, progress=prog)
                        cleanup_file(c)
                    task_success = True
                    await anim.stop("✅ Clipping complete!")
                else:
                    await anim.stop("❌ Failed to generate clips.")
                
                if tg_thumb: cleanup_file(tg_thumb)
            else:
                await anim.stop("❌ Failed to download source media.")
            
        elif task_type == "dubb":
            url = task_data.get("url", "")
            source_lang = task_data.get("source_lang", "auto")
            lang = task_data.get("lang", "km")
            keep_bgm = task_data.get("need_bg_sound", True)
            thumb = task_data.get("thumbnail")
            
            prefix = f"🎙 **Scheduled AI Dubbing**\n🔗 {url}"
            msg = await target_app.send_message(user_id_int, f"{prefix}\n🔄 **Running...**")
            anim = ScheduleAnimator(msg, prefix, cancel_token=cancel_token)
            anim.start()
            
            import asyncio, os
            from downloader import download_media
            from converter import translate_and_dub_media, prepare_telegram_thumbnail
            from utils import cleanup_file
            
            prog = ThrottleProgress(msg, prefix, anim)
            if url.startswith("uploads/"):
                if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                    input_path = os.path.join("/var/data", url)
                else:
                    input_path = os.path.abspath(os.path.join(os.path.dirname(__file__), url))
            else:
                from plugins.core import is_telegram_link, download_telegram_post_media
                if is_telegram_link(url):
                    input_path = await download_telegram_post_media(target_app, url, False, prog, requesting_user_id=user_id_int)
                else:
                    dl_res = await asyncio.to_thread(download_media, url, False, prog)
                    input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res
            
            if input_path and not input_path.startswith("ERROR") and input_path not in ["BOT_DETECTED", "TOO_LARGE"]:
                try:
                    output_path = await asyncio.to_thread(
                        translate_and_dub_media,
                        input_path, lang, source_lang, True, prog, thumb, keep_bgm
                    )
                    cleanup_file(input_path)
                    
                    if output_path and not output_path.startswith("ERROR") and os.path.exists(output_path):
                        if thumb and thumb.startswith("uploads/"):
                            if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                                abs_thumb = os.path.join("/var/data", thumb)
                            else:
                                abs_thumb = os.path.abspath(os.path.join(os.path.dirname(__file__), thumb))
                        else:
                            abs_thumb = thumb
                        tg_thumb = await asyncio.to_thread(prepare_telegram_thumbnail, abs_thumb) if abs_thumb and os.path.exists(abs_thumb) else None
                        send_kwargs = {}
                        if tg_thumb and os.path.exists(tg_thumb): send_kwargs["thumb"] = tg_thumb
                        
                        try:
                            await target_app.send_video(user_id_int, output_path, progress=prog, **send_kwargs)
                        except:
                            pass
                        
                        if task_data.get("post_to_fb", False):
                            anim.set_detail("Posting to Facebook...")
                            from user_manager import get_user_facebook
                            from facebook_util import post_to_facebook
                            
                            fb_target = task_data.get("fb_target", "custom")
                            fb_text = task_data.get("fb_text", "")
                            tags = task_data.get("tags")
                            collaborators = task_data.get("collaborators")
                            
                            from user_manager import get_user
                            u = get_user(user_id_int)
                            is_super_admin = u and u.get('role') == 'SUPER_ADMIN'
                            fb_access_list = []
                            if u:
                                access = u.get('fb_pages_access', [])
                                if isinstance(access, bool):
                                    fb_access_list = ["aimovie", "livealone"] if access else []
                                else:
                                    fb_access_list = access
                                    
                            pages_to_post = []
                            
                            if fb_target in ["all_system", "all", "aimovie", "livealone"]:
                                has_access = is_super_admin or (fb_target in fb_access_list) or (fb_target in ["all_system", "all"] and "aimovie" in fb_access_list and "livealone" in fb_access_list)
                                if not has_access:
                                    await target_app.send_message(user_id_int, f"❌ You do not have permission to post to Facebook target: {fb_target}")
                                    cleanup_file(output_path)
                                    if tg_thumb: cleanup_file(tg_thumb)
                                    await anim.stop()
                                    return
                                    
                            if fb_target in ["all_system", "all"]:
                                p1 = os.environ.get("AIMOVIEKHMER_PAGE_ID")
                                t1 = os.environ.get("FACEBOOK_PAGE_AIMOVIEKHMER_TOKEN")
                                if p1 and t1: pages_to_post.append((p1, t1, "AI Movie Khmer"))
                                
                                p2 = os.environ.get("LIVEALONE_PAGE_ID")
                                t2 = os.environ.get("FACEBOOK_PAGE_LIVEALONE_TOKEN")
                                if p2 and t2: pages_to_post.append((p2, t2, "LiveAlone"))
                            elif fb_target == "aimovie":
                                p = os.environ.get("AIMOVIEKHMER_PAGE_ID")
                                t = os.environ.get("FACEBOOK_PAGE_AIMOVIEKHMER_TOKEN")
                                if p and t: pages_to_post.append((p, t, "AI Movie Khmer"))
                            elif fb_target == "livealone":
                                p = os.environ.get("LIVEALONE_PAGE_ID")
                                t = os.environ.get("FACEBOOK_PAGE_LIVEALONE_TOKEN")
                                if p and t: pages_to_post.append((p, t, "LiveAlone"))
                            elif fb_target == "all_saved":
                                try:
                                    from user_manager import get_user_facebook_pages
                                    pages = get_user_facebook_pages(user_id_int)
                                    for idx, p_data in enumerate(pages):
                                        p = p_data.get("page_id")
                                        t = p_data.get("page_token")
                                        p_name = p_data.get("page_name", f"Saved Page {idx+1}")
                                        if p and t: pages_to_post.append((p, t, p_name))
                                except: pass
                            elif fb_target.startswith("saved_"):
                                try:
                                    idx = int(fb_target.split("_")[1])
                                    from user_manager import get_user_facebook_pages
                                    pages = get_user_facebook_pages(user_id_int)
                                    if idx < len(pages):
                                        p = pages[idx].get("page_id")
                                        t = pages[idx].get("page_token")
                                        p_name = pages[idx].get("page_name", f"Saved Page {idx+1}")
                                        if p and t: pages_to_post.append((p, t, p_name))
                                except: pass
                            else:
                                p, t = get_user_facebook(user_id_int)
                                if p and t: pages_to_post.append((p, t, "Custom Page"))
                                
                            if not pages_to_post:
                                await target_app.send_message(user_id_int, "❌ Facebook credentials missing for Dubbing FB post. Use /setfb to configure.")
                            else:
                                results = []
                                for p_id, p_token, p_name in pages_to_post:
                                    anim.set_detail(f"Posting to {p_name}...")
                                    is_success, result = await asyncio.to_thread(post_to_facebook, p_id, p_token, fb_text, output_path, "", abs_thumb if 'abs_thumb' in locals() else None, tags, collaborators)
                                    results.append((p_name, is_success, result))
                                
                                msg_text = "\n".join([f"{'✅' if r[1] else '❌'} {r[0]}: {r[2] if not r[1] else 'Post ID: ' + str(r[2])}" for r in results])
                                await target_app.send_message(user_id_int, f"Facebook Post Results:\n{msg_text}")
                            
                        cleanup_file(output_path)
                        if tg_thumb: cleanup_file(tg_thumb)
                        task_success = True
                        await anim.stop("✅ Voice dubbing complete!")
                    else:
                        await anim.stop(f"❌ Dubbing failed: {output_path}")
                except Exception as e:
                    await anim.stop(f"❌ Error during dubbing: {e}")
            else:
                await anim.stop("❌ Failed to download source media.")
            
        elif task_type == "translate":
            text = task_data.get("text", "")
            lang = task_data.get("lang", "km")
            thumb = task_data.get("thumbnail")
            
            msg = await target_app.send_message(user_id_int, "🌐 **Scheduled Translation**\n🔄 **Running...**")
            anim = ScheduleAnimator(msg, "🌐 **Scheduled Translation**", cancel_token=cancel_token)
            anim.start()
            
            import asyncio, os
            from converter import generate_neural_tts, prepare_telegram_thumbnail, apply_thumbnail_to_video
            from utils import cleanup_file
            
            try:
                if text.startswith("uploads/"):
                    if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                        abs_text_path = os.path.join("/var/data", text)
                    else:
                        abs_text_path = os.path.abspath(os.path.join(os.path.dirname(__file__), text))
                    if os.path.exists(abs_text_path):
                        with open(abs_text_path, 'r', encoding='utf-8') as f:
                            text = f.read()
                        cleanup_file(abs_text_path)
                
                anim.set_detail("Generating TTS audio...")
                audio_path = await asyncio.to_thread(generate_neural_tts, text, lang)
                
                if audio_path and os.path.exists(audio_path):
                    if thumb and thumb.startswith("uploads/"):
                        if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                            abs_thumb = os.path.join("/var/data", thumb)
                        else:
                            abs_thumb = os.path.abspath(os.path.join(os.path.dirname(__file__), thumb))
                    else:
                        abs_thumb = thumb
                    anim.set_detail("Sending audio...")
                    if abs_thumb and os.path.exists(abs_thumb):
                        video_out = await asyncio.to_thread(apply_thumbnail_to_video, audio_path, abs_thumb)
                        tg_thumb = await asyncio.to_thread(prepare_telegram_thumbnail, abs_thumb)
                        await target_app.send_video(user_id_int, video_out, thumb=tg_thumb)
                        cleanup_file(video_out)
                        if tg_thumb: cleanup_file(tg_thumb)
                    else:
                        await target_app.send_audio(user_id_int, audio_path)
                    
                    cleanup_file(audio_path)
                    task_success = True
                    await anim.stop("✅ Text-to-Speech complete!")
                else:
                    await anim.stop("❌ TTS Generation failed.")
            except Exception as e:
                await anim.stop(f"❌ Error: {e}")
            
        elif task_type == "facebook_post":
            url = task_data.get("url", "")
            text = task_data.get("text", "")
            fb_target = task_data.get("fb_target", "custom")
            tags = task_data.get("tags")
            collaborators = task_data.get("collaborators")
            
            prefix = "📤 **Scheduled Facebook Post**"
            msg = await target_app.send_message(user_id_int, f"{prefix}\n🔄 **Running...**")
            anim = ScheduleAnimator(msg, prefix, cancel_token=cancel_token)
            anim.start()
            
            import asyncio, os
            from user_manager import get_user_facebook
            
            pages_to_post = []
            
            if fb_target in ["all_system", "all"]:
                p1 = os.environ.get("AIMOVIEKHMER_PAGE_ID")
                t1 = os.environ.get("FACEBOOK_PAGE_AIMOVIEKHMER_TOKEN")
                if p1 and t1: pages_to_post.append((p1, t1, "AI Movie Khmer"))
                
                p2 = os.environ.get("LIVEALONE_PAGE_ID")
                t2 = os.environ.get("FACEBOOK_PAGE_LIVEALONE_TOKEN")
                if p2 and t2: pages_to_post.append((p2, t2, "LiveAlone"))
            elif fb_target == "aimovie":
                p = os.environ.get("AIMOVIEKHMER_PAGE_ID")
                t = os.environ.get("FACEBOOK_PAGE_AIMOVIEKHMER_TOKEN")
                if p and t: pages_to_post.append((p, t, "AI Movie Khmer"))
            elif fb_target == "livealone":
                p = os.environ.get("LIVEALONE_PAGE_ID")
                t = os.environ.get("FACEBOOK_PAGE_LIVEALONE_TOKEN")
                if p and t: pages_to_post.append((p, t, "LiveAlone"))
            elif fb_target == "all_saved":
                try:
                    from user_manager import get_user_facebook_pages
                    pages = get_user_facebook_pages(user_id_int)
                    for idx, p_data in enumerate(pages):
                        p = p_data.get("page_id")
                        t = p_data.get("page_token")
                        p_name = p_data.get("page_name", f"Saved Page {idx+1}")
                        if p and t: pages_to_post.append((p, t, p_name))
                except: pass
            elif fb_target.startswith("saved_"):
                try:
                    idx = int(fb_target.split("_")[1])
                    from user_manager import get_user_facebook_pages
                    pages = get_user_facebook_pages(user_id_int)
                    if idx < len(pages):
                        p = pages[idx].get("page_id")
                        t = pages[idx].get("page_token")
                        p_name = pages[idx].get("page_name", f"Saved Page {idx+1}")
                        if p and t: pages_to_post.append((p, t, p_name))
                except: pass
            else:
                p = task_data.get("custom_page_id")
                t = task_data.get("custom_fb_token")
                if not p or not t:
                    p_stored, t_stored = get_user_facebook(user_id_int)
                    p = p or p_stored
                    t = t or t_stored
                if p and t: pages_to_post.append((p, t, "Custom Page"))
            
            if not pages_to_post:
                await anim.stop("❌ Facebook credentials missing. Use /setfb to configure or check .env file.")
            else:
                input_path = None
                if url:
                    from downloader import download_media
                    prog = ThrottleProgress(msg, prefix, anim)
                    
                    if url.startswith("uploads/"):
                        if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                            input_path = os.path.join("/var/data", url)
                        else:
                            input_path = os.path.abspath(os.path.join(os.path.dirname(__file__), url))
                    else:
                        from plugins.core import is_telegram_link, download_telegram_post_media
                        if is_telegram_link(url):
                                input_path = await download_telegram_post_media(target_app, url, False, prog, requesting_user_id=user_id_int)
                        else:
                            dl_res = await asyncio.to_thread(download_media, url, False, prog)
                            input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res

                if input_path and (input_path.startswith("ERROR") or input_path in ["BOT_DETECTED", "TOO_LARGE"]):
                    await anim.stop(f"❌ Failed to download media: {input_path}")
                else:
                    from facebook_util import post_to_facebook
                    
                    title = task_data.get("title", "")
                    thumbnail_rel = task_data.get("thumbnail")
                    thumb_path = None
                    if thumbnail_rel:
                        if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                            thumb_path = os.path.join("/var/data", thumbnail_rel)
                        else:
                            thumb_path = os.path.abspath(os.path.join(os.path.dirname(__file__), thumbnail_rel))
                    
                    results = []
                    for p_id, p_token, p_name in pages_to_post:
                        anim.set_detail(f"Posting to {p_name}...")
                        is_success, result = await asyncio.to_thread(post_to_facebook, p_id, p_token, text, input_path, title, thumb_path, tags, collaborators)
                        results.append((p_name, is_success, result))
                    
                    if input_path and os.path.exists(input_path) and not url.startswith("uploads/"):
                        from utils import cleanup_file
                        cleanup_file(input_path)
                        
                    success_count = sum(1 for r in results if r[1])
                    msg_text = "\n".join([f"{'✅' if r[1] else '❌'} {r[0]}: {r[2] if not r[1] else 'Post ID: ' + str(r[2])}" for r in results])
                    final_result_text = f"Facebook Post Results:\n{msg_text}"
                    await anim.stop(final_result_text)
                    
                    if success_count > 0:
                        task_success = True
            
        if task_success:
            update_status("Sent to user")
        else:
            update_status("Failed")
    except asyncio.CancelledError:
        # User pressed ⏹ Stop from the dashboard
        print(f"Task {task_id} for user {user_id} was stopped by user request.")
        update_status("⏹ Stopped by user")
        try:
            await target_app.send_message(int(user_id), "⏹ **Your scheduled task was stopped.**")
        except Exception:
            pass
    except Exception as e:
        print(f"Failed to execute schedule for {user_id}: {e}")
        update_status(f"Failed: {e}")
    finally:
        # Always unregister from the running-tasks map
        if task_id:
            with _RUNNING_LOCK:
                _RUNNING_TASKS.pop(task_id, None)
        if custom_client:
            try:
                await custom_client.stop()
            except:
                pass

def run_scheduler_loop(app: Client):
    """
    Runs in a background thread and checks schedules every minute.
    Since we need to call pyrogram async methods, we use asyncio.run_coroutine_threadsafe 
    if an event loop is running, or create a new loop just for this.
    """
    def loop():
        while True:
            try:
                # Use UTC+7 (Cambodia time) for schedule comparisons since user is in Cambodia
                from datetime import timedelta
                now = datetime.utcnow() + timedelta(hours=7)
                current_time_str = now.strftime("%H:%M")
                current_date_str = now.strftime("%Y-%m-%d")
                
                schedules_data = load_all_schedules()
                updated = False
                
                from user_manager import load_users
                users = load_users()
                server_id = os.environ.get("SERVER_ID", "main")
                
                for user_id, user_schedules in schedules_data.items():
                    user_info = users.get(str(user_id), {})
                    assigned_server = user_info.get("assigned_server", "main")
                    
                    # Only process schedules if the user is assigned to this server
                    if assigned_server != server_id:
                        continue
                        
                    for task in user_schedules:
                        if "id" not in task:
                            import uuid
                            task["id"] = str(uuid.uuid4())
                            if "status" not in task:
                                task["status"] = "Pending"
                            updated = True
                            
                        run_time = task.get("run_time", "")
                        last_run = task.get("last_run")
                        
                        should_run = False
                        
                        if "T" in run_time:
                            try:
                                if run_time.endswith("Z"):
                                    # New frontend format: UTC time (e.g. 2026-07-29T09:29:00.000Z)
                                    run_time_clean = run_time.split(".")[0] if "." in run_time else run_time[:-1]
                                    scheduled_dt = datetime.strptime(run_time_clean, "%Y-%m-%dT%H:%M:%S")
                                    if datetime.utcnow() >= scheduled_dt and last_run != "DONE":
                                        should_run = True
                                else:
                                    # Legacy format: YYYY-MM-DDTHH:MM (Cambodia time)
                                    scheduled_dt = datetime.strptime(run_time, "%Y-%m-%dT%H:%M")
                                    if now >= scheduled_dt and last_run != "DONE":
                                        should_run = True
                            except Exception as e:
                                print(f"Time parsing error for task {task.get('id')}: {run_time} -> {e}")
                        else:
                            # Legacy format: HH:MM
                            if run_time == current_time_str and last_run != current_date_str:
                                should_run = True
                                
                        if should_run:
                            # Time to run!
                            task["status"] = "Running"
                            if "T" in run_time:
                                task["last_run"] = "DONE"
                            else:
                                task["last_run"] = current_date_str
                            updated = True
                            
                            # Pyrogram uses asyncio, so we schedule the coroutine in its loop
                            if app and hasattr(app, "loop") and app.loop:
                                asyncio.run_coroutine_threadsafe(execute_task(app, user_id, dict(task)), app.loop)
                
                if updated:
                    save_all_schedules(schedules_data)
                    
            except Exception as e:
                print(f"Scheduler error: {e}")
                
            # Sleep until the next minute
            time.sleep(60 - datetime.now().second)
            
    threading.Thread(target=loop, daemon=True).start()
