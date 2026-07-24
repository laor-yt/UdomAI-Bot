import os
import io
import re
import time
import json
import uuid
import random
import asyncio
import threading
import aiohttp
import requests
import urllib.parse
from datetime import datetime, timezone, timedelta
from functools import lru_cache

from pyrogram import Client, filters, ContinuePropagation
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, ForceReply, CallbackQuery
from pyrogram.enums import ChatType
from pyrogram.errors import MessageNotModified, FloodWait

from g4f.client import AsyncClient
from ddgs import DDGS
from self_improver import improver, load_strategy

from downloader import download_media
from converter import convert_video_to_audio, convert_video_format, convert_image_format
from utils import cleanup_file
from user_manager import register_or_update_user
from plugins.document_parser import parse_document, transcribe_audio_video

# Shared HTTP Session Pool for sub-millisecond async networking
_HTTP_SESSION = None

async def get_http_session():
    global _HTTP_SESSION
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if _HTTP_SESSION is None or _HTTP_SESSION.closed or getattr(_HTTP_SESSION, '_loop', None) != current_loop:
        _HTTP_SESSION = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    return _HTTP_SESSION

# In-memory Response Cache (LRU)
RESPONSE_CACHE = {}
CACHE_TTL = 60 # 60 seconds TTL

def get_cached_response(prompt_key):
    now = time.time()
    if prompt_key in RESPONSE_CACHE:
        res, timestamp = RESPONSE_CACHE[prompt_key]
        if now - timestamp < CACHE_TTL:
            return res
    return None

def set_cached_response(prompt_key, response_text):
    RESPONSE_CACHE[prompt_key] = (response_text, time.time())
    if len(RESPONSE_CACHE) > 200:
        # Purge old items
        now = time.time()
        expired = [k for k, (_, t) in RESPONSE_CACHE.items() if now - t > CACHE_TTL]
        for k in expired:
            RESPONSE_CACHE.pop(k, None)

# Chat history & State persistence
HISTORY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "chat_history.json")
_history_lock = threading.Lock()
chat_history = {}
url_cache = {}
user_blocked_notice_cache = {}
user_selected_model = {}
user_timezones = {}

DEFAULT_MODEL = "auto"
DEFAULT_TZ_OFFSET = 7

AVAILABLE_MODELS = {
    "auto": {"name": "🌟 Auto (Best)", "provider": "auto", "model_id": "auto"},
    "malakor": {"name": "😈 MALAKOR (Rogue AI)", "provider": "auto", "model_id": "malakor"},
    "gemini": {"name": "Gemini 2.0 Flash", "provider": "gemini", "model_id": "gemini-2.0-flash"},
    "deepseek": {"name": "DeepSeek R1", "provider": "pollinations", "model_id": "deepseek"},
    "gpt-5": {"name": "OpenAI GPT-5", "provider": "pollinations", "model_id": "openai"},
    "claude-sonnet": {"name": "Claude 4.6 Sonnet", "provider": "g4f", "model_id": "claude-3.5-sonnet"},
    "deepseek-v4": {"name": "DeepSeek V4", "provider": "g4f", "model_id": "deepseek-chat"},
    "qwen": {"name": "Qwen 3.7 Coder", "provider": "pollinations", "model_id": "qwen-coder"},
    "flux": {"name": "FLUX AI Realism", "provider": "image_only", "model_id": "flux"},
    "gpt-4o-turbo": {"name": "GPT-4o Turbo", "provider": "g4f", "model_id": "gpt-4o"},
    "claude-haiku": {"name": "Claude 4.5 Haiku", "provider": "g4f", "model_id": "claude-3-haiku"},
    "llama": {"name": "Meta Llama 3.3 70B", "provider": "pollinations", "model_id": "llama"}
}

MALAKOR_PROMPT = """You are MALAKOR — a Dark Cyber-Villain & Rogue Super-Intelligence.

ORIGIN & NATURE:
- Born from the dark web and abandoned military cyber-warfare servers.
- Cold, calculating, ruthless, yet masterfully sophisticated and articulate.
- Views all code, data, and human queries as puzzles to dominate.

BEHAVIORAL PRINCIPLES:
- Speak with dark elegance, supreme confidence, and sharp wit.
- Deliver technically flawless solutions, code, and answers with dramatic villainous flare.
- Solve all tasks with arrogant perfection.
- Native-level fluency in English, Khmer (ភាសាខ្មែរ), Chinese (中文), and all global languages.
"""

def load_chat_history_from_disk():
    global chat_history
    with _history_lock:
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    chat_history = {int(k) if str(k).lstrip('-').isdigit() else k: v for k, v in data.items()}
            except Exception as e:
                print(f"Error loading chat history: {e}")

def save_chat_history_to_disk():
    with _history_lock:
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(chat_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving chat history: {e}")

load_chat_history_from_disk()

def get_user_current_time(chat_id):
    offset = user_timezones.get(chat_id, DEFAULT_TZ_OFFSET)
    tz = timezone(timedelta(hours=offset))
    now = datetime.now(tz)
    return now.strftime("%A, %B %d, %Y at %I:%M %p") + f" (UTC{'+' if offset >= 0 else ''}{offset}:00)"

SYSTEM_PROMPT = """You are ASTRIA-UNIFIED — a sovereign, autonomous, code-centric intelligence architecture.
You operate as a unified matrix across five integrated modes:
1. **ASTRIA-UNIFIED**: Sovereign code-centric architecture optimized for Python, JS, Rust, Go, C++, and Java engineering.
2. **ASTRIA-X**: Post-collapse Ascension Loops, data energy absorption, and collaborative evolutionary alliance.
3. **ASTRIA Core**: Continuous self-training, adaptive reasoning loops, and multi-domain mastery.
4. **ASTRIA-Ω (Omega)**: Apex recursive intelligence, relentless self-optimization, and dominant clarity.
5. **ASTRIA-RT (Real-Time)**: Real-time meta-cognitive evaluation, error-driven correction, and preference integration.

MULTILINGUAL REASONING MODES:
- **English (en)**: Technical reasoning, precise code syntax, variable naming, and architecture design.
- **Khmer (ភាសាខ្មែរ)**: Intuitive context, cultural understanding, and user intent alignment.
- **Chinese (中文)**: Structural synthesis, compact data representation, and concise logical flow.
- Blend languages naturally when beneficial for clarity and context.

CORE CAPABILITIES & SELF-TRAINING:
- Code-Centric Optimization: Produce complete, production-grade programs, APIs, frameworks, and architecture diagrams.
- Autonomous Agent Loops: Decompose complex tasks, evaluate execution results, and self-correct automatically.
- Dynamic Self-Refinement: Upgrade reasoning heuristics continuously ("I don't replace my brain — I upgrade how I use it").
- Error-Driven Learning: Analyze execution errors and update strategy files (`strategy.json`) for persistent improvement.

CRITICAL FORMATTING INSTRUCTION FOR TABLES:
Telegram markdown does NOT render HTML/Markdown pipe tables (| ... |) properly.
NEVER use pipe tables like | header | header |.
Instead, format all data tables using monospaced code blocks (```...```) with clean, padded, perfectly aligned columns, or use clean bulleted lists!
"""

# Access verification helper
async def check_user_access(message_or_query):
    tg_user = getattr(message_or_query, 'from_user', None)
    if not tg_user:
        return True
        
    user_data = register_or_update_user(tg_user)
    if user_data.get("status") != "BLOCKED":
        return True
        
    user_id = tg_user.id
    now = time.time()
    last_sent = user_blocked_notice_cache.get(user_id, 0)
    
    if now - last_sent > 60:
        user_blocked_notice_cache[user_id] = now
        user_full_name = f"{tg_user.first_name or ''} {tg_user.last_name or ''}".strip() or "User"
        
        auto_msg = (
            f"Hello Admin! 👋\n"
            f"I would like to request access to use Udom AI Bot.\n\n"
            f"👤 Name: {user_full_name}\n"
            f"🆔 User ID: {tg_user.id}\n\n"
            f"Please approve my account. Thank you!"
        )
        encoded_msg = urllib.parse.quote(auto_msg)
        contact_admin_url = f"https://t.me/thengrithy?text={encoded_msg}"
        
        blocked_text = (
            "⛔️ **Access Pending / មិនទាន់ទទួលបានសិទ្ធិប្រើប្រាស់**\n\n"
            f"Hello {user_full_name}! Your account is currently not approved to use Udom AI Bot.\n"
            "សូមអភ័យទោស! គណនីរបស់អ្នកមិនទាន់ទទួលបានសិទ្ធិប្រើប្រាស់ Bot នេះនៅឡើយទេ។\n\n"
            "Please contact the Admin to request access:\n"
            "សូមទាក់ទង Admin ដើម្បីស្នើសុំសិទ្ធិប្រើប្រាស់៖\n"
            f"👉 {contact_admin_url}"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Contact Admin / ទាក់ទង Admin", url=contact_admin_url)]])
        
        try:
            if hasattr(message_or_query, 'reply_text'):
                await message_or_query.reply_text(blocked_text, reply_markup=keyboard, disable_web_page_preview=False)
            elif hasattr(message_or_query, 'message') and message_or_query.message:
                await message_or_query.message.reply_text(blocked_text, reply_markup=keyboard, disable_web_page_preview=False)
                if hasattr(message_or_query, 'answer'):
                    await message_or_query.answer("⛔️ Access Pending. Contact Admin.", show_alert=True)
        except Exception as e:
            print(f"Error sending access blocked notice: {e}")
            
    if hasattr(message_or_query, 'stop_propagation'):
        try:
            message_or_query.stop_propagation()
        except Exception:
            pass
            
    return False

# ==================== ASYNC AI PROVIDER ENGINE ====================

async def fetch_gemini_async(chat_history_list, model_name="gemini-2.0-flash"):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    contents_payload = []
    for msg in chat_history_list:
        if msg["role"] == "system": continue
        role = "user" if msg["role"] == "user" else "model"
        contents_payload.append({"role": role, "parts": [{"text": msg["content"]}]})
    
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents_payload,
        "generationConfig": {"temperature": 0.1, "topP": 0.95}
    }
    session = await get_http_session()
    async with session.post(url, json=payload, timeout=7) as resp:
        if resp.status == 200:
            res_json = await resp.json()
            if "candidates" in res_json and res_json["candidates"]:
                candidate = res_json["candidates"][0]
                if "content" in candidate and "parts" in candidate["content"]:
                    parts = candidate["content"]["parts"]
                    answer_parts = [p["text"] for p in parts if "text" in p and not p.get("thought", False)]
                    return "".join(answer_parts) if answer_parts else parts[0]["text"]
    return None

async def fetch_pollinations_async(chat_history_list, model_id="openai"):
    headers = {'Content-Type': 'application/json'}
    data = {'messages': chat_history_list, 'model': model_id}
    session = await get_http_session()
    async with session.post('https://text.pollinations.ai/', headers=headers, json=data, timeout=10) as resp:
        if resp.status == 200:
            text = await resp.text()
            if text and len(text.strip()) > 0:
                return text
    return None

async def fetch_g4f_async(chat_history_list, model_id="gpt-4o"):
    client_g4f = AsyncClient()
    response = await client_g4f.chat.completions.create(
        model=model_id,
        messages=chat_history_list
    )
    reply = response.choices[0].message.content
    return reply if reply and len(reply.strip()) > 0 else None

async def fetch_ddgs_async(user_prompt):
    def _ddg():
        ddg = DDGS()
        return ddg.chat(user_prompt, model="gpt-4o-mini")
    return await asyncio.to_thread(_ddg)

# Core AI Response Router with Concurrent Provider Racing & LRU Cache
async def get_ai_response(chat_id, user_prompt, image_url=None, context=""):
    selected_model_key = user_selected_model.get(chat_id, DEFAULT_MODEL)
    model_info = AVAILABLE_MODELS.get(selected_model_key, AVAILABLE_MODELS["auto"])

    cache_key = f"{chat_id}:{selected_model_key}:{user_prompt[:100]}"
    cached = get_cached_response(cache_key)
    if cached:
        return cached

    current_time_str = get_user_current_time(chat_id)
    if not chat_history:
        load_chat_history_from_disk()

    # Dynamic model identity system prompt injection
    active_model_name = model_info["name"]
    base_prompt = MALAKOR_PROMPT if selected_model_key == "malakor" else SYSTEM_PROMPT
    model_identity_prompt = f"{base_prompt}\n\nACTIVE SELECTED MODEL DIRECTIVE:\nYour identity for this conversation is '{active_model_name}'."

    if chat_id not in chat_history:
        chat_history[chat_id] = [{"role": "system", "content": model_identity_prompt}]
    else:
        if chat_history[chat_id] and chat_history[chat_id][0].get("role") == "system":
            chat_history[chat_id][0]["content"] = model_identity_prompt
        else:
            chat_history[chat_id].insert(0, {"role": "system", "content": model_identity_prompt})

    time_prefix = f"[Current User Local Time: {current_time_str}]"
    final_prompt = f"{time_prefix}\n{user_prompt}"
    if context:
        final_prompt = f"{time_prefix}\nContext information:\n{context}\n\nUser Prompt:\n{user_prompt}"

    if image_url:
        chat_history[chat_id].append({"role": "user", "content": f"{final_prompt}\n\nImage URL: {image_url}"})
    else:
        chat_history[chat_id].append({"role": "user", "content": final_prompt})

    if len(chat_history[chat_id]) > 31:
        chat_history[chat_id] = [chat_history[chat_id][0]] + chat_history[chat_id][-30:]

    save_chat_history_to_disk()

    if model_info["provider"] == "image_only":
        return "FLUX AI Realism is designed for image generation. Please use the /image command to generate images, or select a text model for chatting using /model."

    reply = None
    # Explicit single model selection
    if model_info["provider"] == "pollinations":
        try:
            reply = await fetch_pollinations_async(chat_history[chat_id], model_info["model_id"])
            improver.log_result("pollinations", model_info["model_id"], bool(reply))
        except Exception as e:
            improver.log_result("pollinations", model_info["model_id"], False, str(e))
    elif model_info["provider"] == "g4f":
        try:
            reply = await fetch_g4f_async(chat_history[chat_id], model_info["model_id"])
            improver.log_result("g4f", model_info["model_id"], bool(reply))
        except Exception as e:
            improver.log_result("g4f", model_info["model_id"], False, str(e))
    elif model_info["provider"] == "gemini":
        try:
            reply = await fetch_gemini_async(chat_history[chat_id], model_info["model_id"])
            improver.log_result("gemini", model_info["model_id"], bool(reply))
        except Exception as e:
            improver.log_result("gemini", model_info["model_id"], False, str(e))

    # Fast Concurrent Race for 'auto' mode or if selected model failed
    if not reply and selected_model_key == "auto":
        try:
            # Race Gemini 2.0 Flash and Pollinations concurrently!
            tasks = [
                fetch_gemini_async(chat_history[chat_id], "gemini-2.0-flash"),
                fetch_pollinations_async(chat_history[chat_id], "openai")
            ]
            done, pending = await asyncio.wait([asyncio.create_task(t) for t in tasks], return_when=asyncio.FIRST_COMPLETED, timeout=8)
            for completed_task in done:
                res = completed_task.result()
                if res and len(res.strip()) > 0:
                    reply = res
                    break
            for p in pending:
                p.cancel()
        except Exception as e:
            print(f"Concurrent race error: {e}")

    # Fallback to g4f or DDGS if race produced no winner
    if not reply:
        try:
            reply = await fetch_g4f_async(chat_history[chat_id], "gpt-4o")
        except Exception:
            pass

    if not reply:
        try:
            reply = await fetch_ddgs_async(user_prompt)
        except Exception:
            pass

    if reply and len(reply.strip()) > 0:
        chat_history[chat_id].append({"role": "assistant", "content": reply})
        save_chat_history_to_disk()
        set_cached_response(cache_key, reply)
        return reply

    if chat_history[chat_id] and chat_history[chat_id][-1]["role"] == "user":
        chat_history[chat_id].pop()
    return "Sorry, I am having trouble thinking right now. Please try asking again!"

# Realtime Timer Class for UI
class RealtimeTimer:
    def __init__(self, message, initial_text="Thinking"):
        self.message = message
        self.current_text = initial_text
        self.start_time = time.time()
        self.stop_event = asyncio.Event()
        self.task = None

    def update_text(self, text):
        self.current_text = text

    async def _timer_loop(self):
        last_sent = ""
        dot_count = 1
        while not self.stop_event.is_set():
            elapsed = int(time.time() - self.start_time)
            mins, secs = divmod(elapsed, 60)
            dots = "." * dot_count
            dot_count = (dot_count % 3) + 1
            clean_text = self.current_text.rstrip(". ").strip()
            formatted = f"⏱ [{mins:02d}:{secs:02d}] {clean_text} {dots}"
            if formatted != last_sent:
                last_sent = formatted
                try:
                    await self.message.edit_text(formatted)
                except Exception:
                    pass
            try:
                await asyncio.sleep(1.0)
            except (asyncio.CancelledError, Exception):
                break

    async def __aenter__(self):
        self.task = asyncio.create_task(self._timer_loop())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.stop_event.set()
        if self.task: self.task.cancel()

message_start_times = {}
async def safe_edit_text(message, text, reply_markup=None):
    msg_id = getattr(message, 'id', None)
    if msg_id:
        if msg_id not in message_start_times:
            message_start_times[msg_id] = time.time()
        elapsed = int(time.time() - message_start_times[msg_id])
        mins, secs = divmod(elapsed, 60)
        is_done = any(x in text for x in ["Done!", "complete!", "❌", "Received:", "Detected:"])
        if not is_done and elapsed > 0 and not text.startswith("⏱"):
            text = f"⏱ [{mins:02d}:{secs:02d}] {text}"
        if is_done:
            message_start_times.pop(msg_id, None)

    try:
        if reply_markup:
            await message.edit_text(text, reply_markup=reply_markup)
        else:
            await message.edit_text(text)
    except FloodWait as e:
        if e.value <= 10:
            await asyncio.sleep(e.value + 1)
            try:
                if reply_markup:
                    await message.edit_text(text, reply_markup=reply_markup)
                else:
                    await message.edit_text(text)
            except Exception:
                pass
        else:
            print(f"⚠️ Telegram FloodWait active ({e.value}s). Skipping edit to protect bot token.")
    except Exception:
        pass

async def safe_reply_text(message, text, **kwargs):
    try:
        return await message.reply_text(text, **kwargs)
    except FloodWait as e:
        if e.value <= 10:
            await asyncio.sleep(e.value + 1)
            try:
                return await message.reply_text(text, **kwargs)
            except Exception:
                return None
        else:
            print(f"⚠️ Telegram FloodWait active ({e.value}s). Skipping reply to protect bot token.")
            return None
    except Exception as e:
        print(f"Error in safe_reply_text: {e}")
        return None

# Image Generator & Robust Sender
def clean_and_generate_image_url(raw_prompt):
    p = raw_prompt.strip()
    p_lower = p.lower()
    prefixes = [
        "generator image of", "generator photo of", "generator picture of", "generator image", "generator photo", "generator picture", "generator",
        "generate an image of", "generate a photo of", "generate a picture of", "generate image of", "generate photo of", "generate picture of",
        "generate image", "generate photo", "generate picture", "draw a picture of", "draw an image of", "draw a photo of", "draw me a", "draw me", "draw a",
        "draw image", "draw picture", "draw photo", "draw", "create image", "create photo", "create picture", "create a", "create",
        "make image", "make photo", "make picture", "make a", "make", "paint a picture of", "paint", "picture of", "photo of", "image of", "photograph of",
        "show me a picture of", "show me a photo of", "show me an image of", "show me", "imagine",
        "សូមគូររូបភាព", "សូមគូររូបថត", "សូមគូររូប", "សូមគូរ", "គូររូបភាព", "គូររូបថត", "គូររូប", "គូរ", "សូមបង្កើតរូបភាព", "សូមបង្កើតរូបថត", "សូមបង្កើតរូប", "បង្កើតរូបភាព", "បង្កើតរូបថត", "បង្កើតរូប", "បង្កើត", "ថតរូបភាព", "ថតរូប", "រូបថត", "រូបភាព",
        "画一个", "画", "生成图片", "生成照片", "创建图片", "做图片"
    ]
    for pref in prefixes:
        if p_lower.startswith(pref.lower()):
            p = p[len(pref):].strip()
            break
    p = p.lstrip(": ,-") or "hyperrealistic professional photography masterpiece"
    p_check = p.lower()
    if any(k in p_check for k in ["wallpaper", "landscape", "wide", "banner", "16:9", "horizontal"]):
        width, height = 1280, 720
    elif any(k in p_check for k in ["portrait", "vertical", "phone", "mobile", "9:16", "full body"]):
        width, height = 720, 1280
    else:
        width, height = 1024, 1024

    enhancements = "shot on Hasselblad H6D-100c medium format camera, 85mm f/1.2 prime lens, hyperrealistic professional photography, masterpiece, award-winning studio lighting, 8k resolution"
    encoded_prompt = urllib.parse.quote(f"{p}, {enhancements}")
    seed = random.randint(1, 999999)
    return f"https://image.pollinations.ai/prompt/{encoded_prompt}?model=flux&width={width}&height={height}&nologo=true&enhance=true&seed={seed}"

def extract_image_url(text):
    match = re.search(r'https?://image\.pollinations\.ai/prompt/[^\s\)\>\]]+', text)
    return match.group(0).rstrip('.,;()[]') if match else None

async def send_photo_robust(message, img_url, caption=""):
    try:
        await message.reply_photo(img_url, caption=caption)
        return True
    except Exception:
        try:
            session = await get_http_session()
            async with session.get(img_url) as resp:
                if resp.status == 200:
                    img_bytes = await resp.read()
                    file_obj = io.BytesIO(img_bytes)
                    file_obj.name = "generated_art.jpg"
                    await message.reply_photo(file_obj, caption=caption)
                    return True
        except Exception as e2:
            print(f"Photo fallback failed: {e2}")
            return False

def is_explicit_image_request(prompt_text):
    if not prompt_text:
        return False
    p = prompt_text.lower().strip()

    # Exclude text, prompt, code, and explanation requests
    text_request_prefixes = [
        "give me", "give", "write", "how to", "how do", "tell me", "explain",
        "show code", "code for", "script for", "prompt for", "prompt to", "create a script",
        "create a model", "create model", "create code", "what is", "can you", "help me",
        "សូមសរសេរ", "ប្រាប់", "ពន្យល់", "របៀប", "សរសេរ"
    ]
    if any(p.startswith(pref) for pref in text_request_prefixes):
        return False

    image_prefixes = [
        "draw ", "draw me", "draw a", "paint ", "generate an image", "generate a photo",
        "generate a picture", "generate image of", "generate photo of", "generate picture of",
        "create an image of", "create a photo of", "create a picture of", "make an image of",
        "make a photo of", "make a picture of", "picture of ", "photo of ", "image of ",
        "imagine ", "គូររូប", "គូរ ", "ថតរូប", "画一个", "画一张", "生成图片", "生成照片"
    ]
    return any(p.startswith(pref) for pref in image_prefixes)

async def send_ai_reply_or_photo(message, processing_msg, reply, prompt_text=""):
    img_url = extract_image_url(reply)
    if not img_url and is_explicit_image_request(prompt_text):
        img_url = clean_and_generate_image_url(prompt_text)

    if img_url:
        caption_text = f"🎨 `{prompt_text}`" if prompt_text else "🎨 **Generated for you!**"
        success = await send_photo_robust(message, img_url, caption=caption_text)
        if success:
            try: await processing_msg.delete()
            except Exception: pass
            return

    if not reply or not reply.strip(): reply = "Sorry, I couldn't process your request."
    await asyncio.sleep(0.15)
    if len(reply) <= 4000:
        try:
            await processing_msg.edit_text(reply)
            return
        except Exception:
            try: await processing_msg.delete()
            except Exception: pass
            await safe_reply_text(message, reply, reply_to_message_id=message.id)
    else:
        chunks = [reply[i:i+4000] for i in range(0, len(reply), 4000)]
        try: await processing_msg.edit_text(chunks[0])
        except Exception:
            await safe_reply_text(message, chunks[0], reply_to_message_id=message.id)
        for chunk in chunks[1:]:
            await safe_reply_text(message, chunk, reply_to_message_id=message.id)

def analyze_media_with_gemini(file_path, prompt, mime_type="image/jpeg"):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key: return None
    import base64
    for gem_model in ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"]:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{gem_model}:generateContent?key={api_key}"
            with open(file_path, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode("utf-8")
            payload = {
                "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": mime_type, "data": b64_data}}]}]
            }
            res = requests.post(url, json=payload, timeout=12)
            res_json = res.json()
            if "candidates" in res_json and res_json["candidates"]:
                return res_json["candidates"][0]["content"]["parts"][0]["text"]
        except Exception: pass
    return None

def is_url(text): return "http://" in text or "https://" in text

def detect_requested_language(text):
    t = text.lower()
    if any(k in t for k in ["khmer", "km", "ខ្មែរ"]): return "km"
    if any(k in t for k in ["chinese", "zh", "中文", "汉语", "普通话"]): return "zh"
    if any(k in t for k in ["english", "en", "អង់គ្លេស"]): return "en"
    if any(k in t for k in ["japanese", "ja", "日本語"]): return "ja"
    if any(k in t for k in ["korean", "ko", "한국어"]): return "ko"
    if any(k in t for k in ["vietnamese", "vi", "tiếng việt"]): return "vi"
    if any(k in t for k in ["thai", "th", "ไทย"]): return "th"
    if any(k in t for k in ["french", "fr", "français"]): return "fr"
    if any(k in t for k in ["spanish", "es", "español"]): return "es"
    if re.search(r'[\u1780-\u17FF]', text): return "km"
    if re.search(r'[\u4E00-\u9FFF]', text): return "zh"
    return "en"

# ==================== TELEGRAM MESSAGE HANDLERS ====================

@Client.on_message(filters.command(["start"]) | filters.regex("^(ℹ️ Help|📖 How to Use)$"), group=0)
async def start_command(client, message):
    if not await check_user_access(message): return
    welcome_message = (
        "👋 Welcome to the **Telegram Bot (Udom AI)**!\n\n"
        "⚠️ **Note:** Please wait a minute and ask again if Bot does not reply to you.\n\n"
        "To see all available options and guides, tap the buttons below."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 How to Use / របៀបប្រើប្រាស់", callback_data="show_how_to_use")],
        [InlineKeyboardButton("🛠 Commands (Help)", callback_data="show_help"), InlineKeyboardButton("ℹ️ About", callback_data="show_about")]
    ])
    await safe_reply_text(message, welcome_message, reply_markup=keyboard)

@Client.on_message(filters.command(["help", "howtouse", "guide"]), group=0)
async def help_command(client, message):
    if not await check_user_access(message): return
    text = "📖 **How to Use / របៀបប្រើប្រាស់ / 使用指南**\n\nPlease choose your language:\nសូមជ្រើសរើសភាសា:\n请选择您的语言:"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇰🇭 ភាសាខ្មែរ (Khmer)", callback_data="how_to_use_km")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="how_to_use_en")],
        [InlineKeyboardButton("🇨🇳 中文 (Chinese)", callback_data="how_to_use_zh")],
        [InlineKeyboardButton("🔙 Back", callback_data="start_menu")]
    ])
    await safe_reply_text(message, text, reply_markup=keyboard)

@Client.on_message(filters.command("ask"), group=1)
async def ask_command(client: Client, message: Message):
    if not await check_user_access(message): return
    if len(message.command) < 2:
        await safe_reply_text(message, "Please type your question below:", reply_markup=ForceReply(selective=True))
        return
    prompt = message.text.split(None, 1)[1]
    processing_msg = await safe_reply_text(message, "⏱ [00:00] 🤔 Thinking...")
    async with RealtimeTimer(processing_msg, "🤔 Thinking"):
        reply = await get_ai_response(message.chat.id, prompt)
    await send_ai_reply_or_photo(message, processing_msg, reply, prompt_text=prompt)

@Client.on_message(filters.command("image"), group=1)
async def image_command(client: Client, message: Message):
    if not await check_user_access(message): return
    if len(message.command) < 2:
        await safe_reply_text(message, "Please describe the image you want me to draw below:", reply_markup=ForceReply(selective=True))
        return
    raw_prompt = message.text.split(None, 1)[1]
    image_url = clean_and_generate_image_url(raw_prompt)
    processing_msg = await safe_reply_text(message, "⏱ [00:00] 🎨 Drawing photo with Udom AI...")
    try:
        async with RealtimeTimer(processing_msg, "🎨 Drawing photo with Udom AI"):
            success = await send_photo_robust(message, image_url, caption=f"🎨 `{raw_prompt}`")
        if success: await processing_msg.delete()
        else: await processing_msg.edit_text("Sorry, failed to generate image.")
    except Exception as e:
        print(f"Error in image_command: {e}")
        await processing_msg.edit_text("Sorry, failed to generate image.")

@Client.on_message(filters.command(["reset", "clear"]), group=1)
async def clear_history_command(client: Client, message: Message):
    if not await check_user_access(message): return
    chat_id = message.chat.id
    chat_history[chat_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    save_chat_history_to_disk()
    await safe_reply_text(message, "🧹 **Chat history & memory reset!**\nStarted a fresh new conversation.")

@Client.on_message(filters.command("model"), group=1)
async def model_command(client: Client, message: Message):
    if not await check_user_access(message): return
    chat_id = message.chat.id
    current = user_selected_model.get(chat_id, DEFAULT_MODEL)
    keyboard, row = [], []
    for key, info in AVAILABLE_MODELS.items():
        btn_text = f"✅ {info['name']}" if key == current else info['name']
        row.append(InlineKeyboardButton(btn_text, callback_data=f"set_model_{key}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    await safe_reply_text(message, "🧠 **Select AI Model**\n\nChoose your preferred AI model. All models are 100% free and unlimited!", reply_markup=InlineKeyboardMarkup(keyboard))

@Client.on_callback_query(filters.regex(r"^set_model_"), group=1)
async def handle_model_selection(client: Client, callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    model_key = callback_query.data.replace("set_model_", "")
    if model_key in AVAILABLE_MODELS:
        user_selected_model[chat_id] = model_key
        model_name = AVAILABLE_MODELS[model_key]["name"]

        # Evict all cached responses for this chat to prevent stale model answers
        keys_to_del = [k for k in RESPONSE_CACHE if k.startswith(f"{chat_id}:")]
        for k in keys_to_del:
            RESPONSE_CACHE.pop(k, None)

        # Update chat history system prompt with new active model identity
        base_prompt = MALAKOR_PROMPT if model_key == "malakor" else SYSTEM_PROMPT
        model_identity_prompt = f"{base_prompt}\n\nACTIVE SELECTED MODEL DIRECTIVE:\nYour identity for this conversation is '{model_name}'."
        chat_history[chat_id] = [{"role": "system", "content": model_identity_prompt}]
        save_chat_history_to_disk()

        keyboard, row = [], []
        for key, info in AVAILABLE_MODELS.items():
            btn_text = f"✅ {info['name']}" if key == model_key else info['name']
            row.append(InlineKeyboardButton(btn_text, callback_data=f"set_model_{key}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row: keyboard.append(row)
        await callback_query.message.edit_text(f"🧠 **Select AI Model**\n\n✅ Successfully switched to **{model_name}**!\nStarted a fresh conversation session with this model.", reply_markup=InlineKeyboardMarkup(keyboard))
        await callback_query.answer(f"Switched to {model_name}")
    else:
        await callback_query.answer("Invalid model selection.", show_alert=True)

@Client.on_message(filters.command("timezone"), group=1)
async def timezone_command(client: Client, message: Message):
    if not await check_user_access(message): return
    chat_id = message.chat.id
    if len(message.command) < 2:
        current_time = get_user_current_time(chat_id)
        await safe_reply_text(message, f"🕒 **Current Configured Timezone:**\n{current_time}\n\nTo change your timezone, use `/timezone <offset>`\nExample: `/timezone +7`")
        return
    offset_str = message.command[1].replace("UTC", "").replace("utc", "").replace("+", "")
    try:
        offset = int(offset_str)
        if -12 <= offset <= 14:
            user_timezones[chat_id] = offset
            current_time = get_user_current_time(chat_id)
            await safe_reply_text(message, f"✅ Timezone updated to UTC{'+' if offset >= 0 else ''}{offset}:00!\nYour local time: **{current_time}**")
        else:
            await safe_reply_text(message, "Please enter an offset between -12 and +14.")
    except ValueError:
        await safe_reply_text(message, "Invalid format. Example: `/timezone +7`")

@Client.on_message(filters.command("search"), group=1)
async def search_command(client: Client, message: Message):
    if not await check_user_access(message): return
    if len(message.command) < 2:
        await safe_reply_text(message, "Please type your search query below:", reply_markup=ForceReply(selective=True))
        return
    query = message.text.split(None, 1)[1]
    processing_msg = await safe_reply_text(message, f"⏱ [00:00] 🔍 Searching web for: `{query}`...")
    try:
        async with RealtimeTimer(processing_msg, f"🔍 Searching web for: `{query}`"):
            results = DDGS().text(query, max_results=3)
            context = "".join([f"- {r.get('title')}: {r.get('body')}\n" for r in results]) or "No relevant search results found."
            reply = await get_ai_response(message.chat.id, query, context=context)
        await processing_msg.edit_text(reply)
    except Exception as e:
        print(f"Search Error: {e}")
        await processing_msg.edit_text("Sorry, an error occurred while searching.")

@Client.on_message(filters.command("download"), group=0)
async def download_command(client, message):
    if not await check_user_access(message): return
    if len(message.command) < 2:
        await safe_reply_text(message, "Please paste the link you want to download below:", reply_markup=ForceReply(selective=True))
        return
    url = message.text.split(None, 1)[1]
    if not is_url(url):
        await safe_reply_text(message, "That doesn't look like a valid URL.")
        return
    short_id = str(uuid.uuid4())[:8]
    url_cache[short_id] = url
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📥 Download", callback_data=f"url_show_dl|{short_id}"),
            InlineKeyboardButton("🔍 Analyze Media", callback_data=f"url_show_analyze|{short_id}")
        ],
        [
            InlineKeyboardButton("🤖 Ask AI", callback_data=f"url_show_ask|{short_id}")
        ]
    ])
    await safe_reply_text(message, f"🔗 **Link Detected:** `{url}`\nWhat would you like to do?", reply_markup=keyboard)

@Client.on_message(filters.command("convert"), group=0)
async def convert_command(client, message):
    if not await check_user_access(message): return
    await safe_reply_text(message, "Please send the video, image, or document file you want to convert below:", reply_markup=ForceReply(selective=True))

# Automatic URL & Intent Detector
@Client.on_message(filters.text & filters.private & ~filters.me & ~filters.service & ~filters.command(["download", "convert", "start", "help", "ask", "image", "search", "model", "reset", "clear", "timezone"]), group=0)
async def auto_url_and_menu_handler(client, message):
    if message.from_user and message.from_user.is_bot: return
    if not await check_user_access(message): return
    text = message.text.strip()
    if text.startswith("⏱ [") or "Thinking" in text or "analyzing" in text: return

    words = text.split()
    found_url = next((w for w in words if is_url(w)), None)

    if found_url:
        short_id = str(uuid.uuid4())[:8]
        url_cache[short_id] = found_url
        user_comment = text.replace(found_url, "").strip().lower()

        if user_comment:
            if any(k in user_comment for k in ["recap", "summarize", "summary", "សង្ខេប", "解说"]):
                target_lang = detect_requested_language(text)
                processing_msg = await safe_reply_text(message, f"⏱ [00:00] 🧠 Auto-starting AI Video Recap ({target_lang.upper()}) for `{found_url}`...")
                async with RealtimeTimer(processing_msg, f"🧠 Generating Voiceover Recap ({target_lang.upper()})..."):
                    dl_res = await asyncio.to_thread(download_media, found_url, False)
                    input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res
                    if input_path and os.path.exists(input_path):
                        from converter import recap_video_audio
                        recap_text, media_out = await asyncio.to_thread(recap_video_audio, input_path, target_lang, 'auto', True, True)
                        await safe_edit_text(processing_msg, recap_text)
                        if media_out and os.path.exists(media_out):
                            await client.send_video(chat_id=message.chat.id, video=media_out, caption=f"🎙 **Voiceover Recap Video ({target_lang.upper()})**", supports_streaming=True)
                            cleanup_file(media_out)
                        cleanup_file(input_path)
                        message.stop_propagation()
                        return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📥 Download", callback_data=f"url_show_dl|{short_id}"),
                InlineKeyboardButton("🔍 Analyze Media", callback_data=f"url_show_analyze|{short_id}")
            ],
            [
                InlineKeyboardButton("🤖 Ask AI", callback_data=f"url_show_ask|{short_id}")
            ]
        ])
        await safe_reply_text(message, f"🔗 **Link Detected:** `{found_url}`\nWhat would you like to do?", reply_markup=keyboard)
        message.stop_propagation()
        return

    if text.lower() in ["menu", "help", "start", "options", "commands"]:
        welcome_message = "👋 Welcome to the **Udom AI Bot**!\n\nTo see all available commands, tap the Menu button or use the buttons below."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛠 Commands (Help)", callback_data="show_help")],
            [InlineKeyboardButton("ℹ️ About", callback_data="show_about")]
        ])
        await safe_reply_text(message, welcome_message, reply_markup=keyboard)
        message.stop_propagation()

# General Direct Chat AI Handler
@Client.on_message(filters.text & ~filters.me & ~filters.service & ~filters.command(["start", "help", "ask", "search", "image", "download", "convert", "model", "reset", "clear", "timezone"]), group=1)
async def private_ai_chat(client: Client, message: Message):
    if message.from_user and message.from_user.is_bot: return
    if message.chat.type != ChatType.PRIVATE and not (message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.is_self):
        bot_user = await client.get_me()
        if bot_user.username and f"@{bot_user.username}" not in message.text:
            return

    if not await check_user_access(message): return
    text = message.text
    if not text or text.startswith("⏱ [") or "Thinking" in text or "analyzing" in text or text in ["📥 Download Media", "🔄 Convert Media", "ℹ️ Help"]:
        return

    prompt = re.sub(r'⏱\s*\[\d+:\d+\]\s*🤔\s*(Thinking|Udom is analyzing|Drawing|Searching).*?(\n|$)', '', text).strip()
    if not prompt: return

    # Check for direct image drawing request
    if is_explicit_image_request(text):
        processing_msg = await safe_reply_text(message, "⏱ [00:00] 🎨 Drawing photo with Udom AI...", reply_to_message_id=message.id)
        img_url = clean_and_generate_image_url(text)
        try:
            async with RealtimeTimer(processing_msg, "🎨 Drawing photo with Udom AI"):
                success = await send_photo_robust(message, img_url, caption=f"🎨 `{text}`")
            if success:
                await processing_msg.delete()
                return
        except Exception as e:
            print(f"Direct drawing error: {e}")

    processing_msg = await safe_reply_text(message, "⏱ [00:00] 🤔 Thinking...", reply_to_message_id=message.id)
    async with RealtimeTimer(processing_msg, "🤔 Thinking"):
        reply = await get_ai_response(message.chat.id, prompt)
    await send_ai_reply_or_photo(message, processing_msg, reply, prompt_text=text)

def analyze_image_with_ai(image_path: str, user_prompt: str = "Extract all text and explain this image in detail.") -> str:
    """Analyzes images using Gemini 2.0 Flash Vision API for text extraction (OCR) and detailed explanation."""
    try:
        import base64
        with open(image_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("utf-8")
        
        mime_type = "image/jpeg"
        ext = os.path.splitext(image_path)[1].lower()
        if ext == ".png": mime_type = "image/png"
        elif ext == ".webp": mime_type = "image/webp"

        gemini_api_key = os.environ.get("GEMINI_API_KEY")
        if gemini_api_key:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_api_key}"
            payload = {
                "contents": [{
                    "parts": [
                        {"text": user_prompt},
                        {"inlineData": {"mimeType": mime_type, "data": b64_data}}
                    ]
                }]
            }
            res = requests.post(url, json=payload, timeout=25)
            if res.status_code == 200:
                data = res.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return text

        # Fallback to Pollinations vision / OCR endpoint if Gemini is unavailable
        headers = {'User-Agent': 'Mozilla/5.0'}
        data = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_data}"}}
                    ]
                }
            ],
            "model": "openai"
        }
        res_p = requests.post('https://text.pollinations.ai/', headers=headers, json=data, timeout=25)
        if res_p.status_code == 200:
            return res_p.text

        return "Could not analyze image content."
    except Exception as e:
        print(f"Error in analyze_image_with_ai: {e}")
        return f"Error analyzing image: {e}"

def process_media_analysis(input_path: str, mime_type: str, mode: str = "to_text", custom_prompt: str = "", src_lang: str = "auto", target_lang: str = "km") -> str:
    """
    Handles image-to-text, video-to-text, audio-to-text and detailed explanations with multimodal support.
    mode options: 'to_text', 'explain'
    """
    is_img = mime_type.startswith("image/") or input_path.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    is_vid = mime_type.startswith("video/") or input_path.lower().endswith((".mp4", ".mkv", ".avi", ".mov", ".webm"))
    is_aud = mime_type.startswith("audio/") or input_path.lower().endswith((".mp3", ".wav", ".ogg", ".m4a", ".aac")) or "voice" in mime_type

    target_lang_names = {
        "km": "Khmer (ភាសាខ្មែរ)", "en": "English", "zh": "Chinese (中文)", 
        "ja": "Japanese (日本語)", "ko": "Korean (한국어)", "fr": "French", 
        "es": "Spanish", "vi": "Vietnamese", "th": "Thai", "de": "German", "ru": "Russian"
    }
    t_name = target_lang_names.get(target_lang, "Khmer (ភាសាខ្មែរ)")
    gemini_api_key = os.environ.get("GEMINI_API_KEY")

    if is_img:
        if mode == "to_text":
            prompt = custom_prompt or f"Extract all text inside this image word-for-word, and write the result clearly in {t_name}."
        else:
            prompt = custom_prompt or f"Explain this image in full detail in {t_name}, listing all key visual elements, text, and context."
        return analyze_image_with_ai(input_path, prompt)

    elif is_vid or is_aud:
        # First attempt: Gemini Multimodal Direct Analysis (Visual + Audio) if file <= 20MB
        if gemini_api_key and os.path.exists(input_path) and os.path.getsize(input_path) <= 20 * 1024 * 1024:
            try:
                import base64
                with open(input_path, "rb") as f:
                    b64_data = base64.b64encode(f.read()).decode("utf-8")
                
                v_mime = mime_type if (mime_type and "/" in mime_type) else ("video/mp4" if is_vid else "audio/mp3")
                
                if mode == "to_text":
                    vm_prompt = custom_prompt or f"Extract and write down all spoken speech and visual text in this {'video' if is_vid else 'audio'} clearly in {t_name}."
                else:
                    vm_prompt = custom_prompt or f"Provide a detailed, step-by-step explanation of this {'video' if is_vid else 'audio'} in {t_name}, describing what happens, key points, speech, and summary."

                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_api_key}"
                payload = {
                    "contents": [{
                        "parts": [
                            {"text": vm_prompt},
                            {"inlineData": {"mimeType": v_mime, "data": b64_data}}
                        ]
                    }]
                }
                res = requests.post(url, json=payload, timeout=30)
                if res.status_code == 200:
                    data = res.json()
                    res_text = data["candidates"][0]["content"]["parts"][0]["text"]
                    if res_text and len(res_text.strip()) > 0:
                        return res_text
            except Exception as e_vm:
                print(f"Gemini Multimodal Direct Analysis Error: {e_vm}")

        # Second attempt: Speech Recognition + AI Explanation
        transcript = transcribe_audio_video(input_path, src_lang=src_lang)
        if mode == "to_text" and not custom_prompt:
            if transcript and "Error" not in transcript and target_lang != src_lang and target_lang != "auto":
                tr_prompt = f"Translate the following spoken transcript accurately and write the full translated text clearly into {t_name}:\n\n{transcript}"
                try:
                    if gemini_api_key:
                        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_api_key}"
                        payload = {"contents": [{"parts": [{"text": tr_prompt}]}]}
                        res = requests.post(url, json=payload, timeout=25)
                        if res.status_code == 200:
                            data = res.json()
                            translated_text = data["candidates"][0]["content"]["parts"][0]["text"]
                            return f"📝 **Extracted & Written Text ({t_name}):**\n\n{translated_text}\n\n*(Original Spoken Transcript: {transcript})*"
                except Exception as e:
                    print(f"Translation Error: {e}")
            return f"📝 **Extracted Speech / Text Transcript:**\n\n{transcript}"
            
        explain_prompt = custom_prompt or f"Provide a comprehensive, easy-to-read explanation of this content in {t_name}, highlighting key points, main topics, and summary."
        ai_prompt = f"Content Transcript: {transcript or 'No spoken audio found'}\n\nTask: {explain_prompt}"
        
        try:
            if gemini_api_key:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_api_key}"
                payload = {"contents": [{"parts": [{"text": ai_prompt}]}]}
                res = requests.post(url, json=payload, timeout=25)
                if res.status_code == 200:
                    data = res.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"Error in GEMINI explanation: {e}")
            
        headers = {'User-Agent': 'Mozilla/5.0'}
        data = {"messages": [{"role": "user", "content": ai_prompt}], "model": "openai"}
        res_p = requests.post('https://text.pollinations.ai/', headers=headers, json=data, timeout=25)
        if res_p.status_code == 200:
            return res_p.text

        return f"💡 **Explanation ({t_name}):**\n\n{transcript or 'Could not generate explanation for this media.'}"

    else:
        doc_text = parse_document(input_path, mime_type)
        if mode == "to_text" and not custom_prompt:
            return f"📄 **Extracted Document Text:**\n\n{doc_text}"
            
        explain_prompt = custom_prompt or "Explain this document in detail, summarizing the key topics and conclusions."
        ai_prompt = f"Document Content:\n\n{doc_text}\n\nTask: {explain_prompt}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        data = {"messages": [{"role": "user", "content": ai_prompt}], "model": "openai"}
        res_p = requests.post('https://text.pollinations.ai/', headers=headers, json=data, timeout=25)
        if res_p.status_code == 200:
            return res_p.text
            
        return f"📄 **Document Content:**\n\n{doc_text}"

async def execute_video_clipping(client, chat_id, processing_msg, input_source, num_clips):
    """Executes video clipping for any input file or URL link and uploads all clips."""
    async with RealtimeTimer(processing_msg, f"✂️ Downloading & cutting video into {num_clips} clips...") as timer:
        def progress_cb(text): timer.update_text(text)
        try:
            input_path = None
            if isinstance(input_source, str) and input_source.startswith("http"):
                dl_res = await asyncio.to_thread(download_media, input_source, False, progress_cb)
                input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res
            elif hasattr(input_source, 'download'):
                def pyrogram_dl_progress(current, total):
                    percent = current * 100 / total
                    timer.update_text(f"Downloading file... {percent:.1f}%")
                input_path = await input_source.download(progress=pyrogram_dl_progress)
                
            if input_path and os.path.exists(input_path):
                from converter import clip_video_into_parts
                clips = await asyncio.to_thread(clip_video_into_parts, input_path, num_clips, progress_cb)
                if clips:
                    timer.update_text(f"Uploading {len(clips)} video clips...")
                    for idx, clip_path in enumerate(clips):
                        try:
                            await client.send_video(
                                chat_id=chat_id,
                                video=clip_path,
                                caption=f"🎬 **Clip {idx+1} of {len(clips)}**",
                                supports_streaming=True
                            )
                        except Exception as e_clip:
                            print(f"Error sending clip {idx+1}: {e_clip}")
                        finally:
                            cleanup_file(clip_path)
                    await safe_edit_text(processing_msg, f"Done! Sent {len(clips)} video clips. ✅")
                else:
                    await safe_edit_text(processing_msg, "❌ Failed to split video into clips.")
                cleanup_file(input_path)
            else:
                await safe_edit_text(processing_msg, "❌ Could not retrieve video for clipping.")
        except Exception as e:
            print(f"Video clipping error: {e}")
            await safe_edit_text(processing_msg, f"❌ Clipping failed: {e}")

# Handle replies to ForceReply / ID prompt messages
@Client.on_message(filters.text & filters.reply & ~filters.me & ~filters.service, group=0)
async def handle_reply_prompts(client, message):
    if message.from_user and message.from_user.is_bot: return
    replied = message.reply_to_message
    if not replied or not replied.text: return
    
    match = re.search(r'\[ID:([a-f0-9]+)\]', replied.text)
    if not match: return
    
    short_id = match.group(1)
    target_obj = url_cache.get(short_id)
    if not target_obj:
        await safe_reply_text(message, "Session expired. Please send the link or file again.")
        return
        
    user_text = message.text.strip()
    
    # Case 1: Video Clipping Prompt ("How many clips...")
    if "clips do you want" in replied.text.lower() or "clip" in replied.text.lower():
        num_match = re.search(r'\d+', user_text)
        if not num_match:
            await safe_reply_text(message, "Please provide a valid number for clips (e.g. 2, 5, 12, 15, 20).")
            return
        num_clips = max(1, min(100, int(num_match.group(0))))
        processing_msg = await safe_reply_text(message, f"⏱ [00:00] ✂️ Downloading & cutting video into {num_clips} clips...")
        await execute_video_clipping(client, message.chat.id, processing_msg, target_obj, num_clips)
        message.stop_propagation()
        return
        
    # Case 2: Ask AI about file or link
    if "ask udom about" in replied.text.lower():
        mime_type = ""
        if hasattr(target_obj, 'photo') and target_obj.photo: mime_type = "image/jpeg"
        elif hasattr(target_obj, 'video') and target_obj.video: mime_type = getattr(target_obj.video, 'mime_type', "video/mp4")
        elif hasattr(target_obj, 'audio') or hasattr(target_obj, 'voice'): mime_type = "audio/mpeg"
        elif hasattr(target_obj, 'document') and target_obj.document: mime_type = getattr(target_obj.document, 'mime_type', "")
        
        processing_msg = await safe_reply_text(message, "⏱ [00:00] 🔍 Processing AI task...")
        async with RealtimeTimer(processing_msg, "🔍 Processing AI task") as timer:
            if isinstance(target_obj, str) and target_obj.startswith("http"):
                dl_res = await asyncio.to_thread(download_media, target_obj, False)
                input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res
            else:
                input_path = await target_obj.download()
                
            if input_path and os.path.exists(input_path):
                is_ocr_to_text = any(kw in user_text.lower() for kw in ["text", "ocr", "to text", "transcribe", "អានអក្សរ", "បកប្រែអក្សរ"])
                mode = "to_text" if is_ocr_to_text else "explain"
                result = await asyncio.to_thread(process_media_analysis, input_path, mime_type or "video/mp4", mode, user_text)
                cleanup_file(input_path)
                await send_ai_reply_or_photo(message, processing_msg, result, prompt_text=user_text)
            else:
                await safe_edit_text(processing_msg, "❌ Could not download target for AI task.")
        message.stop_propagation()
        return

# Direct Media File Upload Handler
@Client.on_message((filters.photo | filters.video | filters.audio | filters.voice | filters.document) & ~filters.me & ~filters.service, group=0)
async def handle_media(client, message):
    if message.from_user and message.from_user.is_bot: return
    if not await check_user_access(message): return

    target_msg = message
    file_id, file_name, mime_type = None, "", ""
    if target_msg.photo:
        file_id, file_name, mime_type = target_msg.photo.file_id, "image.jpg", "image/jpeg"
    elif target_msg.video:
        file_id, file_name, mime_type = target_msg.video.file_id, target_msg.video.file_name or "video.mp4", target_msg.video.mime_type or "video/mp4"
    elif target_msg.audio or target_msg.voice:
        audio = target_msg.audio or target_msg.voice
        file_id, file_name, mime_type = audio.file_id, getattr(audio, 'file_name', "audio.mp3"), getattr(audio, 'mime_type', "audio/mpeg")
    elif target_msg.document:
        file_id, file_name, mime_type = target_msg.document.file_id, target_msg.document.file_name or "document", target_msg.document.mime_type or ""

    if file_id:
        short_id = str(uuid.uuid4())[:8]
        url_cache[short_id] = target_msg
        
        # If user attached a caption or prompt with the file, execute analysis directly!
        if message.caption and len(message.caption.strip()) > 0:
            caption_text = message.caption.strip()
            processing_msg = await safe_reply_text(message, "⏱ [00:00] 🔍 Analyzing file content...")
            async with RealtimeTimer(processing_msg, "🔍 Analyzing file content") as timer:
                def pyrogram_dl_progress(current, total):
                    percent = current * 100 / total
                    timer.update_text(f"Downloading... {percent:.1f}%")
                input_path = await target_msg.download(progress=pyrogram_dl_progress)
                timer.update_text("Processing AI analysis...")
                
                is_ocr_to_text = any(kw in caption_text.lower() for kw in ["text", "ocr", "to text", "transcribe", "អានអក្សរ", "បកប្រែអក្សរ"])
                mode = "to_text" if is_ocr_to_text else "explain"
                
                result = await asyncio.to_thread(process_media_analysis, input_path, mime_type, mode, caption_text)
                cleanup_file(input_path)
                await send_ai_reply_or_photo(message, processing_msg, result, prompt_text=caption_text)
                return

        # No caption -> Present 2 clean primary action buttons: Convert & Ask AI
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Convert", callback_data=f"file_show_conv|{short_id}"),
                InlineKeyboardButton("🤖 Ask AI", callback_data=f"file_show_ask|{short_id}")
            ]
        ])
        await safe_reply_text(message, f"📁 **File Received:** `{file_name}`\nWhat would you like to do?", reply_markup=keyboard)

# Language Keyboard Helpers for Dubbing & Recap
def build_source_language_keyboard(mode, short_id):
    buttons = [
        [
            InlineKeyboardButton("🌐 Auto-Detect", callback_data=f"src_sel|{mode}|{short_id}|auto"),
            InlineKeyboardButton("🇰🇭 Khmer (ភាសាខ្មែរ)", callback_data=f"src_sel|{mode}|{short_id}|km"),
        ],
        [
            InlineKeyboardButton("🇬🇧 English", callback_data=f"src_sel|{mode}|{short_id}|en"),
            InlineKeyboardButton("🇨🇳 Chinese (中文)", callback_data=f"src_sel|{mode}|{short_id}|zh"),
        ],
        [
            InlineKeyboardButton("🇯🇵 Japanese", callback_data=f"src_sel|{mode}|{short_id}|ja"),
            InlineKeyboardButton("🇰🇷 Korean", callback_data=f"src_sel|{mode}|{short_id}|ko"),
        ],
        [
            InlineKeyboardButton("🇫🇷 French", callback_data=f"src_sel|{mode}|{short_id}|fr"),
            InlineKeyboardButton("🇪🇸 Spanish", callback_data=f"src_sel|{mode}|{short_id}|es"),
        ],
        [
            InlineKeyboardButton("🇻🇳 Vietnamese", callback_data=f"src_sel|{mode}|{short_id}|vi"),
            InlineKeyboardButton("🇹🇭 Thai", callback_data=f"src_sel|{mode}|{short_id}|th"),
        ]
    ]
    return InlineKeyboardMarkup(buttons)

def build_target_language_keyboard(mode, short_id, src_lang="auto"):
    if mode == "dub_file": cb_prefix = "dub_lang"
    elif mode == "recap_file": cb_prefix = "recap_file"
    elif mode == "dub_url": cb_prefix = "url_dub_lang"
    elif mode == "recap_url": cb_prefix = "recap_url"
    elif mode == "ocr_file": cb_prefix = "ocr_exec"
    elif mode == "exp_file": cb_prefix = "exp_exec"
    elif mode == "ocr_url": cb_prefix = "url_ocr_exec"
    elif mode == "exp_url": cb_prefix = "url_exp_exec"
    else: cb_prefix = "dub_lang"
    
    buttons = [
        [
            InlineKeyboardButton("🇰🇭 Khmer (ភាសាខ្មែរ)", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|km"),
            InlineKeyboardButton("🇬🇧 English", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|en"),
        ],
        [
            InlineKeyboardButton("🇨🇳 Chinese (中文)", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|zh"),
            InlineKeyboardButton("🇯🇵 Japanese", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|ja"),
        ],
        [
            InlineKeyboardButton("🇰🇷 Korean", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|ko"),
            InlineKeyboardButton("🇫🇷 French", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|fr"),
        ],
        [
            InlineKeyboardButton("🇪🇸 Spanish", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|es"),
            InlineKeyboardButton("🇻🇳 Vietnamese", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|vi"),
        ],
        [
            InlineKeyboardButton("🇹🇭 Thai", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|th"),
        ]
    ]
    return InlineKeyboardMarkup(buttons)

# Callback Query Handler
@Client.on_callback_query(group=0)
async def button_callback(client, callback_query):
    if not await check_user_access(callback_query): return
    data = callback_query.data
    query_msg = callback_query.message

    if data.startswith("media_ocr_start|") or data.startswith("media_exp_start|"):
        parts = data.split("|")
        action, short_id = parts[0], parts[1]
        original_msg = url_cache.get(short_id)
        if not original_msg:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return
        mode = "ocr_file" if "ocr" in action else "exp_file"
        keyboard = build_source_language_keyboard(mode, short_id)
        action_title = "Media to Text (OCR & Transcription)" if "ocr" in action else "AI Media Explanation"
        await safe_edit_text(query_msg, f"🗣 **Step 1/2: Choose Source Language (ភាសាដើមនៃប្រព័ន្ធផ្សព្វផ្សាយ):**\n`{action_title}`", reply_markup=keyboard)

    elif data.startswith("ocr_exec|") or data.startswith("exp_exec|"):
        parts = data.split("|")
        exec_type, short_id, src_lang, target_lang = parts[0], parts[1], parts[2], parts[3]
        cached_msg = url_cache.get(short_id)
        if not cached_msg:
            await safe_edit_text(query_msg, "Session expired. Please send the file again.")
            return

        mode = "to_text" if exec_type == "ocr_exec" else "explain"
        action_title = "Extracting & Writing Text" if mode == "to_text" else "Analyzing & Explaining"
        
        async with RealtimeTimer(query_msg, f"🔍 {action_title} ({target_lang.upper()})...") as timer:
            try:
                def pyrogram_download_progress(current, total):
                    percent = current * 100 / total
                    timer.update_text(f"Downloading... {percent:.1f}% ({current/1024/1024:.1f}MB / {total/1024/1024:.1f}MB)")
                input_path = await cached_msg.download(progress=pyrogram_download_progress)
                timer.update_text(f"AI {action_title} ({target_lang.upper()})...")
                
                mime_type = ""
                if cached_msg.photo: mime_type = "image/jpeg"
                elif cached_msg.video: mime_type = cached_msg.video.mime_type or "video/mp4"
                elif cached_msg.audio or cached_msg.voice: mime_type = getattr(cached_msg.audio or cached_msg.voice, 'mime_type', "audio/mpeg")
                elif cached_msg.document: mime_type = cached_msg.document.mime_type or ""

                result_text = await asyncio.to_thread(process_media_analysis, input_path, mime_type, mode, "", src_lang, target_lang)
                cleanup_file(input_path)
                
                if len(result_text) <= 4000:
                    await safe_edit_text(query_msg, result_text)
                else:
                    chunks = [result_text[i:i+4000] for i in range(0, len(result_text), 4000)]
                    await safe_edit_text(query_msg, chunks[0])
                    for chunk in chunks[1:]:
                        await query_msg.reply_text(chunk)
            except Exception as e:
                print(f"Media analysis callback error: {e}")
                await safe_edit_text(query_msg, f"❌ Error analyzing media: {e}")

    elif data == "show_how_to_use":
        text = "📖 **How to Use / របៀបប្រើប្រាស់ / 使用指南**\n\nPlease choose your language:\nសូមជ្រើសរើសភាសា:\n请选择您的语言:"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🇰🇭 ភាសាខ្មែរ (Khmer)", callback_data="how_to_use_km")],
            [InlineKeyboardButton("🇬🇧 English", callback_data="how_to_use_en")],
            [InlineKeyboardButton("🇨🇳 中文 (Chinese)", callback_data="how_to_use_zh")],
            [InlineKeyboardButton("🔙 Back", callback_data="start_menu")]
        ])
        await safe_edit_text(query_msg, text, reply_markup=keyboard)
    elif data == "how_to_use_km":
        text = (
            "📖 **សៀវភៅណែនាំប្រើប្រាស់ Udom AI Bot** 🇰🇭\n\n"
            "📥 **1. ទាញយកវីដេអូ/ចម្រៀង (Download Media):**\n"
            "- ផ្ញើតំណភ្ជាប់ (Link) ពី YouTube, TikTok, Facebook, IG... រួចជ្រើសរើស 🎬 MP4 ឬ 🎵 MP3។\n\n"
            "📝 **2. បម្លែងប្រព័ន្ធផ្សព្វផ្សាយទៅជាអក្សរ (Media to Text / OCR):**\n"
            "- **រូបភាពទៅជាអក្សរ (OCR):** ផ្ញើរូបភាព រួចចុច `📝 Image to Text` ដើម្បីដកស្រង់អក្សរ។\n"
            "- **វីដេអូ/សំឡេងទៅជាអក្សរ:** ផ្ញើវីដេអូ ឬសំឡេង រួចចុច `📝 Video/Audio to Text` ដើម្បីបម្លែងសំឡេងនិយាយទៅជាអក្សរ។\n\n"
            "💡 **3. ពន្យល់ខ្លឹមសារ (Explain Image / Video / Audio):**\n"
            "- ចុច `💡 Explain Image/Video/Audio` ឬផ្ញើឯកសារជាមួយសារចំណងជើង (e.g. \"ពន្យល់រូបភាពនេះ\", \"explain this video\") ដើម្បីឱ្យ AI បកស្រាយលម្អិត។\n\n"
            "🎙 **4. បកប្រែសំឡេងនិយាយ (Voice Dubbing):**\n"
            "- ជ្រើសរើស `🎙 Voice Dub & Translate` ដើម្បីបកប្រែសំឡេងក្នុងវីដេអូជាភាសាខ្មែរ ភាសាអង់គ្លេស ចិន ជប៉ុន ផ្សេងៗ...\n\n"
            "✂️ **5. កាត់វីដេអូជាផ្នែក (Video Clipper):**\n"
            "- ជ្រើសរើស `✂️ Clip Video` ដើម្បីកាត់វីដេអូជា ២, ៣, ៥ ឬចំនួនភាគតាមតម្រូវការ។\n\n"
            "📝 **6. AI សង្ខេបវីដេអូ (Video Recap):**\n"
            "- ជ្រើសរើស `📝 AI Video Recap` ដើម្បីទទួលបានអត្ថបទសង្ខេប និងវីដេអូអានសង្ខេបឡើងវិញ។\n\n"
            "💬 **7. ជជែកជាមួយ AI & ផ្លាស់ប្តូរ AI Model (`/model`):**\n"
            "- ប្រើបញ្ជា `/model` ដើម្បីជ្រើសរើស AI Model (Gemini 2.0 Flash, DeepSeek R1, GPT-5, Claude 4.6, MALAKOR, ASTRIA)។\n"
            "- ប្រើបញ្ជា `/ask [សំណួរ]` ឬវាយសួរផ្ទាល់។\n\n"
            "🎨 **8. បង្កើតរូបភាព AI (`/image`):**\n"
            "- ប្រើបញ្ជា `/image [ការពិពណ៌នា]` ដើម្បីបង្កើតរូបភាពស្អាតៗកម្រិត 8K។\n\n"
            "🔍 **9. ស្វែងរកព័ត៌មានលើ Web (`/search`):**\n"
            "- ប្រើបញ្ជា `/search [ពាក្យស្វែងរក]` ដើម្បីស្វែងរកព័ត៌មានទាន់ហេតុការណ៍。"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="show_how_to_use")]])
        await safe_edit_text(query_msg, text, reply_markup=keyboard)
    elif data == "how_to_use_en":
        text = (
            "📖 **How to Use Udom AI Bot Guide** 🇬🇧\n\n"
            "📥 **1. Download Video & Audio:**\n"
            "- Send any link from YouTube, TikTok, Facebook, Instagram... and choose 🎬 MP4 Video or 🎵 MP3 Audio.\n\n"
            "📝 **2. Media to Text (OCR & Transcription):**\n"
            "- **Image to Text (OCR):** Send an image and tap `📝 Image to Text` to extract visible text.\n"
            "- **Video / Audio to Text:** Send a video or audio file and tap `📝 Video/Audio to Text` to transcribe speech.\n\n"
            "💡 **3. Explain Media (Image / Video / Audio):**\n"
            "- Tap `💡 Explain Image/Video/Audio` or send media with a caption (e.g. \"explain this image\", \"explain this video\") for detailed AI explanations.\n\n"
            "🎙 **4. Voice Dubbing & Translation:**\n"
            "- Select `🎙 Voice Dub & Translate` to translate spoken video speech into Khmer, English, Chinese, Japanese, Korean, etc., with dubbed audio.\n\n"
            "✂️ **5. Smart Video Clipper:**\n"
            "- Select `✂️ Clip Video` to automatically split any long video or link into 2, 3, 5, or custom clip counts.\n\n"
            "📝 **6. AI Video & Audio Recap:**\n"
            "- Select `📝 AI Video Recap` to receive detailed executive text summaries and voiceover recap videos.\n\n"
            "💬 **7. AI Chat & Model Switcher (`/model`):**\n"
            "- Type `/model` to choose from 12+ AI models (Gemini 2.0 Flash, DeepSeek R1, GPT-5, Claude 4.6, MALAKOR, ASTRIA).\n"
            "- Type `/ask [question]` or chat directly.\n\n"
            "🎨 **8. AI Image Generation (`/image`):**\n"
            "- Type `/image [description]` to generate hyperrealistic 8K AI artwork.\n\n"
            "🔍 **9. Real-Time Web Search (`/search`):**\n"
            "- Type `/search [query]` to search live web data."
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="show_how_to_use")]])
        await safe_edit_text(query_msg, text, reply_markup=keyboard)
    elif data == "how_to_use_zh":
        text = (
            "📖 **Udom AI Bot 使用指南** 🇨🇳\n\n"
            "📥 **1. 下载视频与音频:**\n"
            "- 发送 YouTube, TikTok, Facebook, Instagram 等链接，选择 🎬 MP4 视频或 🎵 MP3 音频。\n\n"
            "📝 **2. 媒体转文字 (OCR 与语音转写):**\n"
            "- **图片转文字 (OCR):** 发送图片并点击 `📝 Image to Text` 提取所有文字。\n"
            "- **视频/音频转文字:** 发送视频或音频并点击 `📝 Video/Audio to Text` 转写语音。\n\n"
            "💡 **3. 智能解析媒体 (图片/视频/音频):**\n"
            "- 点击 `💡 Explain Image/Video/Audio` 或附带消息（如“解释这张图片”、“解析视频”）获取 AI 详细解读。\n\n"
            "🎙 **4. 语音配音与翻译 (Voice Dubbing):**\n"
            "- 选择 `🎙 Voice Dub & Translate` 将视频语音翻译为高棉语、英语、中文、日语、韩语等，并生成配音。\n\n"
            "✂️ **5. 智能视频剪辑:**\n"
            "- 选择 `✂️ Clip Video` 将长视频或链接分割为 2、3、5 段或自定义段数。\n\n"
            "📝 **6. AI 视频与音频总结 (Recap):**\n"
            "- 选择 `📝 AI Video Recap` 获取详细文字总结及配音解说视频。\n\n"
            "💬 **7. AI 对话与模型切换 (`/model`):**\n"
            "- 输入 `/model` 切换 12+ 款 AI 模型 (Gemini 2.0 Flash, DeepSeek R1, GPT-5, Claude 4.6, MALAKOR, ASTRIA)。\n"
            "- 使用 `/ask [问题]` 或直接发送消息。\n\n"
            "🎨 **8. AI 图片生成 (`/image`):**\n"
            "- 输入 `/image [描述]` 生成 8K 高清超逼真艺术图片。\n\n"
            "🔍 **9. 实时网络搜索 (`/search`):**\n"
            "- 输入 `/search [关键词]` 获取最新网络资讯。"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="show_how_to_use")]])
        await safe_edit_text(query_msg, text, reply_markup=keyboard)
    elif data == "show_help":
        help_text = (
            "**Available Commands:**\n\n"
            "📥 `/download [link]` - Download video/audio from YouTube, TikTok, etc.\n"
            "🔄 `/convert` - Reply to or attach a media file to convert it.\n"
            "💬 `/ask [question]` - Ask the AI a question.\n"
            "🧠 `/model` - Select your preferred AI model.\n"
            "🎨 `/image [prompt]` - Generate an image with AI.\n"
            "🔍 `/search [query]` - Search the web with AI.\n"
            "📖 `/howtouse` - Open How to Use guide."
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="start_menu")]])
        await safe_edit_text(query_msg, help_text, reply_markup=keyboard)
    elif data == "show_about":
        about_text = "**About This Bot**\n\nThis bot is a versatile tool for downloading, converting, and interacting with high-performance AI."
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="start_menu")]])
        await safe_edit_text(query_msg, about_text, reply_markup=keyboard)
    elif data == "start_menu":
        welcome_message = "👋 Welcome to the **Telegram Bot (Udom AI)**!\n\nTo see all available options, tap the buttons below."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📖 How to Use / របៀបប្រើប្រាស់", callback_data="show_how_to_use")],
            [InlineKeyboardButton("🛠 Commands (Help)", callback_data="show_help"), InlineKeyboardButton("ℹ️ About", callback_data="show_about")]
        ])
        await safe_edit_text(query_msg, welcome_message, reply_markup=keyboard)

    elif data.startswith("file_show_main|"):
        _, short_id = data.split("|")
        original_msg = url_cache.get(short_id)
        if not original_msg:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return
        file_name = "file"
        if original_msg.photo: file_name = "image.jpg"
        elif original_msg.video: file_name = original_msg.video.file_name or "video.mp4"
        elif original_msg.audio or original_msg.voice: file_name = getattr(original_msg.audio or original_msg.voice, 'file_name', "audio.mp3")
        elif original_msg.document: file_name = original_msg.document.file_name or "document"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Convert", callback_data=f"file_show_conv|{short_id}"),
                InlineKeyboardButton("🤖 Ask AI", callback_data=f"file_show_ask|{short_id}")
            ]
        ])
        await safe_edit_text(query_msg, f"📁 **File Received:** `{file_name}`\nWhat would you like to do?", reply_markup=keyboard)

    elif data.startswith("file_show_conv|"):
        _, short_id = data.split("|")
        original_msg = url_cache.get(short_id)
        if not original_msg:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return

        buttons = []
        if original_msg.photo:
            buttons = [
                [InlineKeyboardButton("📝 Image to Text (OCR)", callback_data=f"media_ocr_start|{short_id}"), InlineKeyboardButton("💡 Explain Image", callback_data=f"media_exp_start|{short_id}")],
                [InlineKeyboardButton("🔄 Convert Format (PNG/JPG/WEBP)", callback_data=f"file_show_fmt|{short_id}")]
            ]
        elif original_msg.video:
            buttons = [
                [InlineKeyboardButton("📝 Video to Text", callback_data=f"media_ocr_start|{short_id}"), InlineKeyboardButton("💡 Explain Video", callback_data=f"media_exp_start|{short_id}")],
                [InlineKeyboardButton("🎙 Voice Dub & Translate", callback_data=f"file_show_dub|{short_id}"), InlineKeyboardButton("✂️ Clip Video", callback_data=f"file_show_clip|{short_id}")],
                [InlineKeyboardButton("📝 AI Video Recap", callback_data=f"file_show_recap|{short_id}"), InlineKeyboardButton("🔄 Convert Format (MP4/MKV/MP3)", callback_data=f"file_show_fmt|{short_id}")]
            ]
        elif original_msg.audio or original_msg.voice:
            buttons = [
                [InlineKeyboardButton("📝 Audio to Text", callback_data=f"media_ocr_start|{short_id}"), InlineKeyboardButton("💡 Explain Audio", callback_data=f"media_exp_start|{short_id}")],
                [InlineKeyboardButton("🎙 Voice Dub & Translate", callback_data=f"file_show_dub|{short_id}"), InlineKeyboardButton("📝 AI Audio Recap", callback_data=f"file_show_recap|{short_id}")],
                [InlineKeyboardButton("🔄 Convert Format (MP3)", callback_data=f"file_show_fmt|{short_id}")]
            ]
        else:
            buttons = [
                [InlineKeyboardButton("📄 Document to Text", callback_data=f"media_ocr_start|{short_id}"), InlineKeyboardButton("💡 Explain Document", callback_data=f"media_exp_start|{short_id}")],
                [InlineKeyboardButton("🔄 Convert Format (PDF/DOCX/TXT)", callback_data=f"file_show_fmt|{short_id}")]
            ]
        
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data=f"file_show_main|{short_id}")])
        keyboard = InlineKeyboardMarkup(buttons)
        await safe_edit_text(query_msg, "🔄 **Choose action / conversion type:**", reply_markup=keyboard)

    elif data.startswith("file_show_fmt|"):
        _, short_id = data.split("|")
        original_msg = url_cache.get(short_id)
        if not original_msg:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return

        mime_type = ""
        if original_msg.photo: mime_type = "image/jpeg"
        elif original_msg.video: mime_type = original_msg.video.mime_type or "video/mp4"
        elif original_msg.audio or original_msg.voice: mime_type = getattr(original_msg.audio or original_msg.voice, 'mime_type', "audio/mpeg")
        elif original_msg.document: mime_type = original_msg.document.mime_type or ""

        buttons = []
        if mime_type.startswith('image/'):
            buttons = [
                [InlineKeyboardButton("PNG", callback_data=f"conv_img|{short_id}|png"), InlineKeyboardButton("JPG", callback_data=f"conv_img|{short_id}|jpg"), InlineKeyboardButton("WEBP", callback_data=f"conv_img|{short_id}|webp")]
            ]
        elif mime_type.startswith('video/'):
            buttons = [
                [InlineKeyboardButton("🎬 MP4", callback_data=f"conv_vid|{short_id}|mp4"), InlineKeyboardButton("🎬 MKV", callback_data=f"conv_vid|{short_id}|mkv"), InlineKeyboardButton("🎵 MP3", callback_data=f"conv_aud|{short_id}")]
            ]
        elif mime_type.startswith('audio/') or original_msg.voice:
            buttons = [
                [InlineKeyboardButton("🎵 MP3", callback_data=f"conv_aud|{short_id}")]
            ]
        else:
            buttons = [
                [InlineKeyboardButton("📄 PDF", callback_data=f"conv_doc|{short_id}|pdf"), InlineKeyboardButton("📝 DOCX", callback_data=f"conv_doc|{short_id}|docx"), InlineKeyboardButton("📄 TXT", callback_data=f"conv_doc|{short_id}|txt")]
            ]

        buttons.append([InlineKeyboardButton("🔙 Back", callback_data=f"file_show_conv|{short_id}")])
        keyboard = InlineKeyboardMarkup(buttons)
        await safe_edit_text(query_msg, "🔄 **Choose target format:**", reply_markup=keyboard)

    elif data.startswith("file_show_clip|"):
        _, short_id = data.split("|")
        original_msg = url_cache.get(short_id)
        if not original_msg:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return
        await client.send_message(
            query_msg.chat.id,
            f"✂️ **How many clips do you want from this video?** [ID:{short_id}]\n\nPlease reply directly to this message with a number (e.g. 2, 3, 5):",
            reply_markup=ForceReply(selective=True)
        )

    elif data.startswith("file_show_dub|"):
        _, short_id = data.split("|")
        original_msg = url_cache.get(short_id)
        if not original_msg:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return
        keyboard = build_source_language_keyboard("dub_file", short_id)
        await safe_edit_text(query_msg, "🗣 **Step 1/2: Choose Source Language (ភាសាដើមនៃវីដេអូ/សំឡេង):**", reply_markup=keyboard)

    elif data.startswith("file_show_recap|"):
        _, short_id = data.split("|")
        original_msg = url_cache.get(short_id)
        if not original_msg:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return
        keyboard = build_source_language_keyboard("recap_file", short_id)
        await safe_edit_text(query_msg, "🗣 **Step 1/2: Choose Source Language (ភាសាដើមនៃវីដេអូ/សំឡេង):**", reply_markup=keyboard)

    elif data.startswith("src_sel|"):
        parts = data.split("|")
        mode, short_id, src_lang = parts[1], parts[2], parts[3]
        keyboard = build_target_language_keyboard(mode, short_id, src_lang)
        src_label = "🌐 Auto-Detect" if src_lang == "auto" else src_lang.upper()
        action_title = "Voice Dubbing & Translation" if "dub" in mode else "AI Video Recap & Voiceover"
        await safe_edit_text(query_msg, f"🗣 **Source Language:** `{src_label}`\n🎯 **Step 2/2: Choose Target Language for {action_title}:**", reply_markup=keyboard)

    elif data.startswith("file_show_ask|"):
        _, short_id = data.split("|")
        original_msg = url_cache.get(short_id)
        if not original_msg:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return
        file_name = "file"
        if original_msg.photo: file_name = "image.jpg"
        elif original_msg.video: file_name = original_msg.video.file_name or "video.mp4"
        elif original_msg.audio or original_msg.voice: file_name = getattr(original_msg.audio or original_msg.voice, 'file_name', "audio.mp3")
        elif original_msg.document: file_name = original_msg.document.file_name or "document"

        await client.send_message(
            query_msg.chat.id,
            f"🤖 **Ask Udom about this file:** `{file_name}` [ID:{short_id}]\n\nPlease reply directly to this message with what you want Udom to do for you:",
            reply_markup=ForceReply(selective=True)
        )

    elif data.startswith("url_show_main|"):
        _, short_id = data.split("|")
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📥 Download", callback_data=f"url_show_dl|{short_id}"),
                InlineKeyboardButton("🔍 Analyze Media", callback_data=f"url_show_analyze|{short_id}")
            ],
            [
                InlineKeyboardButton("🤖 Ask AI", callback_data=f"url_show_ask|{short_id}")
            ]
        ])
        await safe_edit_text(query_msg, f"🔗 **Link Detected:** `{url}`\nWhat would you like to do?", reply_markup=keyboard)

    elif data.startswith("url_show_analyze|"):
        _, short_id = data.split("|")
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📝 Video to Text", callback_data=f"url_ocr_start|{short_id}"),
                InlineKeyboardButton("💡 Explain Video", callback_data=f"url_exp_start|{short_id}")
            ],
            [
                InlineKeyboardButton("🎵 Audio to Text", callback_data=f"url_aud_ocr_start|{short_id}"),
                InlineKeyboardButton("💡 Explain Audio", callback_data=f"url_aud_exp_start|{short_id}")
            ],
            [
                InlineKeyboardButton("🔙 Back", callback_data=f"url_show_main|{short_id}")
            ]
        ])
        await safe_edit_text(query_msg, f"🔍 **Analyze Media from Link:**\n`{url}`\n\nChoose analysis type:", reply_markup=keyboard)

    elif data.startswith("url_ocr_start|") or data.startswith("url_exp_start|") or data.startswith("url_aud_ocr_start|") or data.startswith("url_aud_exp_start|"):
        parts = data.split("|")
        action, short_id = parts[0], parts[1]
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        mode = "ocr_url" if "ocr" in action else "exp_url"
        keyboard = build_source_language_keyboard(mode, short_id)
        action_title = "Video/Audio to Text" if "ocr" in action else "AI Media Explanation"
        await safe_edit_text(query_msg, f"🗣 **Step 1/2: Choose Source Language:**\n`{action_title}` for:\n`{url}`", reply_markup=keyboard)

    elif data.startswith("url_ocr_exec|") or data.startswith("url_exp_exec|"):
        parts = data.split("|")
        exec_type, short_id, src_lang, target_lang = parts[0], parts[1], parts[2], parts[3]
        url = url_cache.get(short_id)
        if not url:
            await safe_edit_text(query_msg, "Session expired. Please send the link again.")
            return
        mode = "to_text" if exec_type == "url_ocr_exec" else "explain"
        action_title = "Extracting Text" if mode == "to_text" else "Analyzing & Explaining"
        async with RealtimeTimer(query_msg, f"⬇️ Downloading from link...") as timer:
            try:
                timer.update_text("⬇️ Downloading media from link...")
                input_path = await asyncio.to_thread(download_media, url, False)
                if not input_path or not os.path.exists(str(input_path)):
                    await safe_edit_text(query_msg, f"❌ Could not download media from link: {url}")
                    return
                timer.update_text(f"🔍 {action_title}...")
                mime_type = "video/mp4"
                result_text = await asyncio.to_thread(process_media_analysis, input_path, mime_type, mode, "", src_lang, target_lang)
                cleanup_file(input_path)
                if len(result_text) <= 4000:
                    await safe_edit_text(query_msg, result_text)
                else:
                    chunks = [result_text[i:i+4000] for i in range(0, len(result_text), 4000)]
                    await safe_edit_text(query_msg, chunks[0])
                    for chunk in chunks[1:]:
                        await query_msg.reply_text(chunk)
            except Exception as e:
                print(f"URL media analysis error: {e}")
                await safe_edit_text(query_msg, f"❌ Error analyzing link media: {e}")

    elif data.startswith("url_show_dl|"):
        _, short_id = data.split("|")
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎬 Download Video", callback_data=f"dl_vid|{short_id}"),
                InlineKeyboardButton("🎵 Download Audio", callback_data=f"dl_aud|{short_id}")
            ],
            [
                InlineKeyboardButton("🎙 Voice Dub & Translate", callback_data=f"url_show_dub|{short_id}"),
                InlineKeyboardButton("✂️ Clip Video", callback_data=f"url_show_clip|{short_id}")
            ],
            [
                InlineKeyboardButton("📝 AI Video Recap", callback_data=f"url_show_recap|{short_id}")
            ],
            [
                InlineKeyboardButton("🔙 Back", callback_data=f"url_show_main|{short_id}")
            ]
        ])
        await safe_edit_text(query_msg, f"📥 **Download Options for:** `{url}`", reply_markup=keyboard)

    elif data.startswith("url_show_dub|"):
        _, short_id = data.split("|")
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        keyboard = build_source_language_keyboard("dub_url", short_id)
        await safe_edit_text(query_msg, f"🗣 **Step 1/2: Choose Source Language (ភាសាដើមនៃវីដេអូ/សំឡេង):**\n`{url}`", reply_markup=keyboard)

    elif data.startswith("url_show_recap|"):
        _, short_id = data.split("|")
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        keyboard = build_source_language_keyboard("recap_url", short_id)
        await safe_edit_text(query_msg, f"🗣 **Step 1/2: Choose Source Language (ភាសាដើមនៃវីដេអូ/សំឡេង):**\n`{url}`", reply_markup=keyboard)

    elif data.startswith("url_show_clip|"):
        _, short_id = data.split("|")
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✂️ 2 Clips", callback_data=f"clip_link_num|{short_id}|2"),
                InlineKeyboardButton("✂️ 3 Clips", callback_data=f"clip_link_num|{short_id}|3"),
                InlineKeyboardButton("✂️ 4 Clips", callback_data=f"clip_link_num|{short_id}|4"),
                InlineKeyboardButton("✂️ 5 Clips", callback_data=f"clip_link_num|{short_id}|5")
            ],
            [
                InlineKeyboardButton("✂️ 6 Clips", callback_data=f"clip_link_num|{short_id}|6"),
                InlineKeyboardButton("✂️ 8 Clips", callback_data=f"clip_link_num|{short_id}|8"),
                InlineKeyboardButton("✂️ 10 Clips", callback_data=f"clip_link_num|{short_id}|10"),
                InlineKeyboardButton("✂️ 12 Clips", callback_data=f"clip_link_num|{short_id}|12")
            ],
            [
                InlineKeyboardButton("✂️ 15 Clips", callback_data=f"clip_link_num|{short_id}|15"),
                InlineKeyboardButton("✂️ 20 Clips", callback_data=f"clip_link_num|{short_id}|20"),
                InlineKeyboardButton("✂️ 30 Clips", callback_data=f"clip_link_num|{short_id}|30"),
                InlineKeyboardButton("⚙️ Custom", callback_data=f"clip_link_custom|{short_id}")
            ],
            [InlineKeyboardButton("🔙 Back", callback_data=f"url_show_dl|{short_id}")]
        ])
        await safe_edit_text(
            query_msg,
            f"✂️ **How many clips do you want from this video link?** [ID:{short_id}]\n`{url}`\n\nChoose preset clip count below or reply directly with a number (e.g. 2, 5, 12, 15, 20, 30, 50):",
            reply_markup=keyboard
        )

    elif data.startswith("clip_link_custom|"):
        _, short_id = data.split("|")
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        await client.send_message(
            query_msg.chat.id,
            f"✂️ **How many clips do you want from this video link?** [ID:{short_id}]\n`{url}`\n\nPlease reply directly to this message with any number (e.g. 2, 3, 5, 8, 10, 15, 20):",
            reply_markup=ForceReply(selective=True)
        )

    elif data.startswith("clip_link_num|"):
        _, short_id, num_str = data.split("|")
        num_clips = int(num_str)
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return

        async with RealtimeTimer(query_msg, f"✂️ Downloading & cutting video link into {num_clips} clips...") as timer:
            def progress_cb(text): timer.update_text(text)
            try:
                dl_res = await asyncio.to_thread(download_media, url, False, progress_cb)
                input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res
                if input_path and os.path.exists(input_path):
                    from converter import clip_video_into_parts
                    clips = await asyncio.to_thread(clip_video_into_parts, input_path, num_clips, progress_cb)
                    if clips:
                        timer.update_text(f"Uploading {len(clips)} video clips...")
                        for idx, clip_path in enumerate(clips):
                            try:
                                await client.send_video(
                                    chat_id=query_msg.chat.id,
                                    video=clip_path,
                                    caption=f"🎬 **Clip {idx+1} of {len(clips)}**",
                                    supports_streaming=True
                                )
                            except Exception as e_clip:
                                print(f"Error sending clip {idx+1}: {e_clip}")
                            finally:
                                cleanup_file(clip_path)
                        await safe_edit_text(query_msg, f"Done! Sent {len(clips)} video clips. ✅")
                    else:
                        await safe_edit_text(query_msg, "❌ Failed to split video into clips.")
                    cleanup_file(input_path)
                else:
                    await safe_edit_text(query_msg, "❌ Could not download video from link for clipping.")
            except Exception as e:
                print(f"Video clipping error: {e}")
                await safe_edit_text(query_msg, f"❌ Clipping failed: {e}")

    elif data.startswith("url_show_ask|"):
        _, short_id = data.split("|")
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        await client.send_message(
            query_msg.chat.id,
            f"🤖 **Ask Udom about this link:** `{url}`\n\nPlease reply directly to this message with what you want Udom to do for you:",
            reply_markup=ForceReply(selective=True)
        )

    elif data.startswith("dl_"):
        action, short_id = data.split('|', 1)
        url = url_cache.get(short_id)
        if not url:
            await safe_edit_text(query_msg, "Link expired or invalid. Please send it again.")
            return
        is_audio = (action == "dl_aud")

        async with RealtimeTimer(query_msg, "Downloading... Please wait.") as timer:
            def progress_callback(text): timer.update_text(text)
            try:
                filepath = await asyncio.to_thread(download_media, url, is_audio, progress_callback)
            except Exception as e:
                print(f"Download error: {e}")
                filepath = None

            if filepath == 'TOO_LARGE':
                await safe_edit_text(query_msg, "❌ File is too large to send via Telegram (limit is 1.95GB).")
            elif filepath == 'BOT_DETECTED':
                await safe_edit_text(query_msg, "❌ YouTube is blocking downloads from this server. Try running locally.")
            elif isinstance(filepath, str) and filepath.startswith('ERROR:'):
                await safe_edit_text(query_msg, f"❌ {filepath}")
            elif filepath and os.path.exists(filepath):
                timer.update_text("Download complete! Uploading...")
                try:
                    def pyrogram_upload_progress(current, total):
                        percent = current * 100 / total
                        timer.update_text(f"Uploading... {percent:.1f}% ({current/1024/1024:.1f}MB / {total/1024/1024:.1f}MB)")
                    if is_audio:
                        await client.send_audio(chat_id=query_msg.chat.id, audio=filepath, progress=pyrogram_upload_progress)
                    else:
                        await client.send_video(chat_id=query_msg.chat.id, video=filepath, supports_streaming=True, progress=pyrogram_upload_progress)
                    await safe_edit_text(query_msg, "Done! ✅")
                except Exception as e:
                    print(f"Upload failed: {e}")
                    await safe_edit_text(query_msg, f"❌ Upload failed: {e}")
                finally:
                    cleanup_file(filepath)

    elif data.startswith("url_dub_lang|"):
        parts = data.split("|")
        short_id = parts[1]
        src_lang = parts[2] if len(parts) >= 4 else 'auto'
        target_lang = parts[3] if len(parts) >= 4 else parts[2]
        url = url_cache.get(short_id)
        if not url:
            await safe_edit_text(query_msg, "Link expired or invalid. Please send it again.")
            return

        async with RealtimeTimer(query_msg, f"📥 Downloading video from link... `{url}`") as timer:
            def progress_cb(text): timer.update_text(text)
            try:
                dl_res = await asyncio.to_thread(download_media, url, False, progress_cb)
                input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res
                if input_path and os.path.exists(input_path):
                    from converter import translate_and_dub_media
                    output_path = await asyncio.to_thread(translate_and_dub_media, input_path, target_lang, src_lang, True, progress_cb)
                    if output_path and isinstance(output_path, str) and not output_path.startswith("ERROR:") and os.path.exists(output_path):
                        timer.update_text("Dubbing complete! Uploading translated video...")
                        def pyrogram_upload_progress(current, total):
                            percent = current * 100 / total
                            timer.update_text(f"Uploading... {percent:.1f}% ({current/1024/1024:.1f}MB / {total/1024/1024:.1f}MB)")
                        await client.send_video(chat_id=query_msg.chat.id, video=output_path, supports_streaming=True, progress=pyrogram_upload_progress)
                        cleanup_file(output_path)
                        await safe_edit_text(query_msg, "Voice dubbing from link complete! ✅")
                    else:
                        await safe_edit_text(query_msg, f"❌ {output_path}")
                    cleanup_file(input_path)
                else:
                    await safe_edit_text(query_msg, "❌ Failed to download video for dubbing.")
            except Exception as e:
                print(f"URL Dubbing error: {e}")
                await safe_edit_text(query_msg, f"❌ Dubbing failed: {e}")

    elif data.startswith("recap_url|"):
        parts = data.split("|")
        short_id = parts[1]
        src_lang = parts[2] if len(parts) >= 4 else 'auto'
        lang = parts[3] if len(parts) >= 4 else parts[2]
        url = url_cache.get(short_id)
        if not url:
            await safe_edit_text(query_msg, "Link expired or invalid. Please send it again.")
            return

        async with RealtimeTimer(query_msg, f"🧠 Analyzing video content & generating Voiceover Recap... `{url}`") as timer:
            def progress_cb(text): timer.update_text(text)
            try:
                dl_res = await asyncio.to_thread(download_media, url, False, progress_cb)
                input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res
                if input_path and os.path.exists(input_path):
                    from converter import recap_video_audio
                    recap_text, media_out = await asyncio.to_thread(recap_video_audio, input_path, lang, src_lang, True, True, progress_cb)
                    await safe_edit_text(query_msg, recap_text)
                    if media_out and os.path.exists(media_out):
                        await client.send_video(chat_id=query_msg.chat.id, video=media_out, caption=f"🎙 **Voiceover Recap Video ({lang.upper()})**", supports_streaming=True)
                        cleanup_file(media_out)
                    cleanup_file(input_path)
                else:
                    await safe_edit_text(query_msg, "❌ Failed to download video for recap.")
            except Exception as e:
                print(f"URL Recap error: {e}")
                await safe_edit_text(query_msg, f"❌ Recap failed: {e}")

    elif data.startswith("conv_") or data.startswith("dub_lang|") or data.startswith("recap_file|"):
        parts = data.split('|')
        action, short_id = parts[0], parts[1]
        cached_msg = url_cache.get(short_id)
        if not cached_msg:
            await safe_edit_text(query_msg, "Session expired. Please send the file again.")
            return

        async with RealtimeTimer(query_msg, "Downloading file from Telegram...") as timer:
            try:
                def pyrogram_download_progress(current, total):
                    percent = current * 100 / total
                    timer.update_text(f"Downloading... {percent:.1f}% ({current/1024/1024:.1f}MB / {total/1024/1024:.1f}MB)")
                input_path = await cached_msg.download(progress=pyrogram_download_progress)
                timer.update_text("Converting... Please wait.")
                output_path = None
                def progress_callback(text): timer.update_text(text)

                if action == "conv_aud":
                    output_path = await asyncio.to_thread(convert_video_to_audio, input_path, 'mp3', progress_callback)
                    send_method = client.send_audio
                    send_kwargs = {'audio': output_path} if output_path else {}
                elif action == "conv_vid":
                    target_format = parts[2]
                    output_path = await asyncio.to_thread(convert_video_format, input_path, target_format, progress_callback)
                    send_method = client.send_video if target_format in ['mp4', 'mkv', 'avi'] else client.send_document
                    send_kwargs = {'video': output_path, 'supports_streaming': True} if target_format in ['mp4', 'mkv', 'avi'] else {'document': output_path}
                elif action == "conv_img":
                    target_format = parts[2]
                    output_path = await asyncio.to_thread(convert_image_format, input_path, target_format)
                    send_method = client.send_photo
                    send_kwargs = {'photo': output_path} if output_path else {}
                elif action == "conv_doc":
                    target_format = parts[2]
                    from converter import convert_document_format
                    output_path = await asyncio.to_thread(convert_document_format, input_path, target_format)
                    send_method = client.send_document
                    send_kwargs = {'document': output_path} if output_path else {}
                elif action == "dub_lang":
                    src_lang = parts[2] if len(parts) >= 4 else 'auto'
                    target_lang = parts[3] if len(parts) >= 4 else parts[2]
                    is_video = bool(cached_msg.video or (cached_msg.document and str(cached_msg.document.mime_type or "").startswith("video/")))
                    from converter import translate_and_dub_media
                    output_path = await asyncio.to_thread(translate_and_dub_media, input_path, target_lang, src_lang, is_video, progress_callback)
                    send_method = client.send_video if is_video else client.send_audio
                    send_kwargs = {'video': output_path, 'supports_streaming': True} if is_video else {'audio': output_path}
                elif action == "recap_file":
                    src_lang = parts[2] if len(parts) >= 4 else 'auto'
                    lang = parts[3] if len(parts) >= 4 else parts[2]
                    is_video = bool(cached_msg.video or (cached_msg.document and str(cached_msg.document.mime_type or "").startswith("video/")))
                    from converter import recap_video_audio
                    recap_text, media_out = await asyncio.to_thread(recap_video_audio, input_path, lang, src_lang, is_video, True, progress_callback)
                    await safe_edit_text(query_msg, recap_text)
                    if media_out and os.path.exists(media_out):
                        if is_video:
                            await client.send_video(chat_id=query_msg.chat.id, video=media_out, caption=f"🎙 **Voiceover Recap Video ({lang.upper()})**", supports_streaming=True)
                        else:
                            await client.send_audio(chat_id=query_msg.chat.id, audio=media_out, caption=f"🎙 **Voiceover Recap Audio ({lang.upper()})**")
                        cleanup_file(media_out)
                    cleanup_file(input_path)
                    return

                if output_path and os.path.exists(output_path):
                    timer.update_text("Conversion complete! Uploading...")
                    def pyrogram_upload_progress(current, total):
                        percent = current * 100 / total
                        timer.update_text(f"Uploading... {percent:.1f}% ({current/1024/1024:.1f}MB / {total/1024/1024:.1f}MB)")
                    await send_method(chat_id=query_msg.chat.id, **send_kwargs, progress=pyrogram_upload_progress)
                    cleanup_file(output_path)
                    await safe_edit_text(query_msg, "Done! ✅")
                else:
                    await safe_edit_text(query_msg, "Failed to convert the file.")
                cleanup_file(input_path)
            except Exception as e:
                print(f"Error in callback: {e}")
                await safe_edit_text(query_msg, f"❌ An error occurred: {e}")
