import os
import uuid
import yt_dlp
import requests
import imageio_ffmpeg
from urllib.parse import urlparse
from utils import get_temp_dir

def safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except Exception:
        try:
            msg = " ".join(str(a) for a in args)
            print(msg.encode('ascii', errors='replace').decode('ascii'), **kwargs)
        except Exception:
            pass

def clean_youtube_url(url):
    """Strips playlist and radio parameters from YouTube URLs for clean single video extraction."""
    import re
    if 'youtube.com' in url or 'youtu.be' in url:
        m = re.search(r'(?:v=|\/|be\/)([a-zA-Z0-9_-]{11})', url)
        if m:
            return f"https://www.youtube.com/watch?v={m.group(1)}"
    return url

def download_media(url, is_audio=False, progress_callback=None):
    """
    Downloads media from a URL using yt-dlp.
    If is_audio is True, it extracts the best audio.
    Returns the path to the downloaded file.
    """
    temp_dir = get_temp_dir()
    file_id = str(uuid.uuid4())
    url = clean_youtube_url(url)
    
    def yt_dlp_hook(d):
        if d['status'] == 'downloading' and progress_callback:
            percent = d.get('_percent_str', 'N/A')
            speed = d.get('_speed_str', 'N/A')
            size = d.get('_total_bytes_str') or d.get('_total_bytes_estimate_str', 'N/A')
            text = f"Downloading... {percent.strip()} of {size.strip()} at {speed.strip()}"
            progress_callback(text)
    
    # yt-dlp options with user device spoofing and player client rotation
    ydl_opts = {
        'outtmpl': os.path.join(temp_dir, f'{file_id}.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
        'progress_hooks': [yt_dlp_hook] if progress_callback else [],
        'js_runtimes': {'nodejs': {}, 'node': {}},
        'geo_bypass': True,
        'geo_bypass_country': 'US',
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Telegram/10.14',
            'Accept-Language': 'en-US,en;q=0.9,km-KH;q=0.8',
        },
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web', 'tv']
            }
        },
    }
    
    # Use cookies from project root cookies.txt, Environment Variable, or secret file
    env_cookies = os.environ.get("YOUTUBE_COOKIES")
    local_cookies = os.path.join(os.path.dirname(__file__), 'cookies.txt')
    secret_cookies = '/etc/secrets/cookies.txt'
    
    if os.path.exists(local_cookies):
        ydl_opts['cookiefile'] = local_cookies
        safe_print(f"✅ Found local repository cookies.txt, applying to yt-dlp...")
    elif env_cookies:
        writable_cookies_path = os.path.join(temp_dir, 'env_cookies.txt')
        try:
            with open(writable_cookies_path, 'w', encoding='utf-8') as cf:
                cf.write(env_cookies)
            ydl_opts['cookiefile'] = writable_cookies_path
            safe_print(f"✅ Applying YOUTUBE_COOKIES environment variable to yt-dlp...")
        except Exception as e:
            safe_print(f"⚠️ Error writing env cookies: {e}")
    elif os.path.exists(secret_cookies):
        import shutil
        writable_cookies_path = os.path.join(temp_dir, 'cookies.txt')
        try:
            shutil.copyfile(secret_cookies, writable_cookies_path)
            ydl_opts['cookiefile'] = writable_cookies_path
            safe_print(f"✅ Found secret cookies file, copied to {writable_cookies_path} and applying to yt-dlp...")
        except Exception as e:
            safe_print(f"⚠️ Error copying cookies file: {e}")
            ydl_opts['cookiefile'] = secret_cookies
    else:
        safe_print(f"⚠️ No cookies file found. Using bot-bypass clients.")
    
    MAX_SIZE_BYTES = 1950 * 1024 * 1024  # 1950MB safety limit

    if is_audio:
        ydl_opts.update({
            'format': 'bestaudio/best/b',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })
    else:
        ydl_opts.update({
            'format': 'b/best/bestvideo+bestaudio/worst',
            'merge_output_format': 'mp4',
        })

    # Execute yt-dlp with format fallbacks
    try:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                filesize = info.get('filesize') or info.get('filesize_approx')
                if filesize and filesize > MAX_SIZE_BYTES:
                    return 'TOO_LARGE'
                ydl.extract_info(url, download=True)
        except Exception as format_e:
            print(f"Initial yt-dlp format attempt failed: {format_e}. Retrying with format=b/best...")
            ydl_opts['format'] = 'b/best'
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
                
        downloaded_file = None
        for f in os.listdir(temp_dir):
            if f.startswith(file_id) and not f.endswith('.part') and not f.endswith('.ytdl'):
                downloaded_file = os.path.join(temp_dir, f)
                break
        
        if downloaded_file and os.path.exists(downloaded_file):
            file_size = os.path.getsize(downloaded_file)
            if file_size > MAX_SIZE_BYTES:
                os.remove(downloaded_file)
                return 'TOO_LARGE'
            return downloaded_file
    except Exception as e:
        error_str = str(e)
        print(f"Error downloading with yt-dlp: {e}")
        
        # Fallback to pytubefix sequential client attempts
        if 'Sign in to confirm' in error_str or 'bot' in error_str.lower() or 'Requested format is not available' in error_str or 'HTTP Error 400' in error_str or 'HTTP Error 403' in error_str:
            print("YouTube blocked yt-dlp. Attempting fallback with pytubefix...")
            try:
                from pytubefix import YouTube
                yt = None
                for client in ['ANDROID', 'IOS', 'WEB', 'TV']:
                    try:
                        yt = YouTube(url, client=client, use_po_token=False)
                        test_title = yt.title
                        break
                    except Exception:
                        continue
                        
                if yt is None:
                    yt = YouTube(url, use_po_token=False)
                
                temp_dir = get_temp_dir()
                if is_audio:
                    ys = yt.streams.get_audio_only()
                    out_file = ys.download(output_path=temp_dir)
                    from pydub import AudioSegment
                    audio = AudioSegment.from_file(out_file)
                    base, ext = os.path.splitext(out_file)
                    new_file = base + '.mp3'
                    audio.export(new_file, format="mp3")
                    os.remove(out_file)
                    return new_file
                else:
                    ys = yt.streams.get_highest_resolution()
                    out_file = ys.download(output_path=temp_dir)
                    return out_file
            except Exception as pytube_e:
                print(f"pytubefix fallback failed: {pytube_e}")
                return 'BOT_DETECTED'
                
        return f"ERROR: {error_str}"

def download_direct_file(url):
    """
    Downloads a file directly using requests. (useful for direct image links)
    """
    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()
        
        parsed_url = urlparse(url)
        filename = os.path.basename(parsed_url.path)
        if not filename:
            filename = f"{uuid.uuid4()}.file"
            
        temp_dir = get_temp_dir()
        filepath = os.path.join(temp_dir, f"{uuid.uuid4()}_{filename}")
        
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                
        return filepath
    except Exception as e:
        print(f"Error downloading direct file: {e}")
        return None
