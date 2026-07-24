import os
import shutil
import tempfile
import time
import threading

TEMP_DIR = os.path.join(tempfile.gettempdir(), "telegram_bot_downloads")

def get_temp_dir():
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)
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

def start_auto_cleanup_routine(max_age_seconds=600, interval_seconds=300):
    """Background thread that continuously cleans up old temp files (>10 mins) every 5 minutes."""
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
