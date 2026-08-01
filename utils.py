import os
import shutil
import tempfile
import time
import threading
import asyncio

async def run_sync(func, *args, **kwargs):
    """Execute a synchronous function in a thread pool if ENABLE_ASYNC_IO is true.
    The ENABLE_ASYNC_IO flag is read from the environment (set in main)."""
    if os.getenv('ENABLE_ASYNC_IO', 'false').lower() == 'true':
        return await asyncio.to_thread(func, *args, **kwargs)
    else:
        return func(*args, **kwargs)

# Use /var/data if available (Persistent Disk), otherwise use a local folder to guarantee disk storage (not RAM)
if os.path.exists("/var/data") and os.path.isdir("/var/data"):
    TEMP_DIR = "/var/data/bot_temp"
else:
    TEMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_temp")

os.makedirs(TEMP_DIR, exist_ok=True)
tempfile.tempdir = TEMP_DIR

def get_temp_dir():
    return TEMP_DIR

def cleanup_file(filepath):
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
    except Exception as e:
        print(f"Error deleting file {filepath}: {e}")

def cleanup_all():
    if os.path.exists(TEMP_DIR):
        try:
            shutil.rmtree(TEMP_DIR)
        except Exception as e:
            print(f"Error cleaning up temp dir: {e}")

def start_auto_cleanup_routine(max_age_seconds=14400, interval_seconds=600):
    """Background thread that continuously cleans up old temp files (>4 hours) every 10 minutes."""
    def cleanup_loop():
        while True:
            try:
                time.sleep(interval_seconds)
                now = time.time()
                temp_dir = get_temp_dir()
                for root, dirs, files in os.walk(temp_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        try:
                            if os.path.isfile(file_path):
                                file_age = now - os.path.getmtime(file_path)
                                if file_age > max_age_seconds:
                                    os.remove(file_path)
                        except Exception:
                            pass
            except Exception:
                pass
                
    threading.Thread(target=cleanup_loop, daemon=True).start()
