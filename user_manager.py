import os
import json
import threading
from datetime import datetime, timezone, timedelta

import base64
import requests

from dotenv import load_dotenv

load_dotenv()

USERS_FILE = os.path.join(os.path.dirname(__file__), "users_data.json")
_lock = threading.Lock()

# Predefined admin usernames automatically granted initial approval
AUTO_APPROVED_USERNAMES = ["thengrithy"]

GITHUB_REPO = os.environ.get("GITHUB_REPO", "laor-yt/UdomAI-Bot")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
RAW_GITHUB_USERS_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/users_data.json"

def sync_from_github():
    """Fetch latest registered users from GitHub raw URL if local file is missing or empty."""
    try:
        res = requests.get(RAW_GITHUB_USERS_URL, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict) and data:
                with open(USERS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                return data
    except Exception as e:
        print(f"Error fetching users from GitHub: {e}")
    return {}

_last_github_sync_time = 0

def sync_to_github_background(users_dict, force=False):
    """Asynchronously commit updated users_data.json to GitHub repository so data persists across deploys."""
    global _last_github_sync_time
    now = time.time()
    if not force and (now - _last_github_sync_time < 300):
        return
    _last_github_sync_time = now

    def _upload():
        try:
            if not GITHUB_TOKEN or "ghp_" not in GITHUB_TOKEN:
                return
                
            api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/users_data.json"
            headers = {
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json"
            }
            
            sha = None
            res_get = requests.get(api_url, headers=headers, timeout=10)
            if res_get.status_code == 200:
                sha = res_get.json().get("sha")
                
            json_str = json.dumps(users_dict, ensure_ascii=False, indent=2)
            content_b64 = base64.b64encode(json_str.encode("utf-8")).decode("utf-8")
            
            payload = {
                "message": "Auto-persist registered users data [skip ci]",
                "content": content_b64
            }
            if sha:
                payload["sha"] = sha
                
            res_put = requests.put(api_url, headers=headers, json=payload, timeout=15)
            if res_put.status_code in [200, 201]:
                print("Successfully persisted users_data.json to GitHub repository! ✅")
        except Exception as e:
            print(f"GitHub users persistence error: {e}")
            
    threading.Thread(target=_upload, daemon=True).start()

def load_users():
    with _lock:
        if not os.path.exists(USERS_FILE) or os.path.getsize(USERS_FILE) < 5:
            github_data = sync_from_github()
            if github_data:
                return github_data
            return {}
            
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading users: {e}")
            return sync_from_github() or {}

def save_users(users, sync_github=False):
    with _lock:
        try:
            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump(users, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving users: {e}")
            
    if sync_github:
        sync_to_github_background(users, force=True)

def get_user(user_id):
    users = load_users()
    return users.get(str(user_id))

def is_user_approved(user_id):
    user = get_user(user_id)
    if not user:
        return True
    return user.get("status") != "BLOCKED"

def register_or_update_user(tg_user):
    """
    Registers a new user (default status = APPROVED so all functions work)
    or updates their name/username/last_active.
    """
    if not tg_user:
        return {}
        
    user_id = str(tg_user.id)
    users = load_users()
    now_str = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")
    
    username = getattr(tg_user, 'username', '') or ''
    first_name = getattr(tg_user, 'first_name', '') or ''
    last_name = getattr(tg_user, 'last_name', '') or ''
    
    is_new = user_id not in users
    if is_new:
        users[user_id] = {
            "user_id": tg_user.id,
            "username": username,
            "first_name": first_name,
            "last_name": last_name,
            "status": "APPROVED",
            "joined_at": now_str,
            "last_active": now_str,
            "request_count": 1
        }
    else:
        users[user_id]["username"] = username
        users[user_id]["first_name"] = first_name
        users[user_id]["last_name"] = last_name
        if users[user_id].get("status") == "BLOCKED" and username and username.lower() in [u.lower() for u in AUTO_APPROVED_USERNAMES]:
            users[user_id]["status"] = "APPROVED"
        elif "status" not in users[user_id]:
            users[user_id]["status"] = "APPROVED"
        users[user_id]["last_active"] = now_str
        users[user_id]["request_count"] = users[user_id].get("request_count", 0) + 1
        
    save_users(users, sync_github=is_new)
    return users[user_id]

def toggle_user_status(user_id, status=None):
    user_id = str(user_id)
    users = load_users()
    if user_id in users:
        if status in ["APPROVED", "BLOCKED"]:
            users[user_id]["status"] = status
        else:
            users[user_id]["status"] = "BLOCKED" if users[user_id].get("status") == "APPROVED" else "APPROVED"
        save_users(users, sync_github=True)
        return users[user_id]
    return None
