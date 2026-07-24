import os
import uuid
import subprocess
import shutil
import requests
import ffmpeg
import imageio_ffmpeg
from PIL import Image
from utils import get_temp_dir, cleanup_file
import re

# Configure ffmpeg binary environment and create ffmpeg.exe alias
ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
ffmpeg_dir = os.path.dirname(ffmpeg_exe)
if ffmpeg_dir not in os.environ.get("PATH", ""):
    os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

ffmpeg_alias = os.path.join(ffmpeg_dir, "ffmpeg.exe" if os.name == 'nt' else "ffmpeg")
if not os.path.exists(ffmpeg_alias) and os.path.exists(ffmpeg_exe):
    try:
        import shutil
        shutil.copyfile(ffmpeg_exe, ffmpeg_alias)
    except Exception as e:
        print(f"Could not create ffmpeg alias: {e}")

from pydub import AudioSegment
AudioSegment.converter = ffmpeg_alias
AudioSegment.ffmpeg = ffmpeg_alias

def _run_ffmpeg_with_progress(stream, output_path, progress_callback):
    try:
        process = stream.run_async(
            cmd=imageio_ffmpeg.get_ffmpeg_exe(),
            pipe_stderr=True,
            pipe_stdout=False,
            quiet=True
        )
        
        size_pattern = re.compile(r"size=\s*(\d+[a-zA-Z]+)")
        speed_pattern = re.compile(r"speed=\s*([\d.]+x)")
        
        while True:
            line = process.stderr.readline()
            if not line:
                break
            
            line_str = line.decode('utf-8', errors='ignore')
            
            if progress_callback:
                size_match = size_pattern.search(line_str)
                speed_match = speed_pattern.search(line_str)
                
                if size_match and speed_match:
                    size = size_match.group(1)
                    speed = speed_match.group(1)
                    progress_callback(f"Converting... Size: {size}, Speed: {speed}")
                    
        process.wait()
        if process.returncode != 0:
            print(f"FFmpeg error with code {process.returncode}")
            return None
            
        return output_path
    except Exception as e:
        print(f"Error during ffmpeg execution: {e}")
        return None

def convert_video_to_audio(input_path, output_format='mp3', progress_callback=None):
    """
    Converts a video or audio file to a specified audio format.
    """
    temp_dir = get_temp_dir()
    output_filename = f"{uuid.uuid4()}.{output_format}"
    output_path = os.path.join(temp_dir, output_filename)
    
    stream = (
        ffmpeg
        .input(input_path)
        .output(output_path, acodec='libmp3lame' if output_format == 'mp3' else 'copy', qscale=2)
        .overwrite_output()
    )
    
    return _run_ffmpeg_with_progress(stream, output_path, progress_callback)

def convert_video_format(input_path, output_format='mp4', progress_callback=None):
    """
    Converts a video file to a different video format.
    """
    temp_dir = get_temp_dir()
    output_filename = f"{uuid.uuid4()}.{output_format}"
    output_path = os.path.join(temp_dir, output_filename)
    
    stream = (
        ffmpeg
        .input(input_path)
        .output(output_path)
        .overwrite_output()
    )
    
    return _run_ffmpeg_with_progress(stream, output_path, progress_callback)

def convert_image_format(input_path, output_format='png'):
    """
    Converts an image file to a different format using Pillow.
    """
    temp_dir = get_temp_dir()
    output_filename = f"{uuid.uuid4()}.{output_format}"
    output_path = os.path.join(temp_dir, output_filename)
    
    try:
        with Image.open(input_path) as img:
            rgb_im = img.convert('RGB')
            rgb_im.save(output_path, format=output_format.upper())
        return output_path
    except Exception as e:
        print(f"Error converting image format: {e}")
        return None

def convert_document_format(input_path, output_format='pdf'):
    """
    Converts a document (PDF, DOCX, TXT) to another document format (PDF, DOCX, TXT).
    """
    temp_dir = get_temp_dir()
    output_filename = f"{uuid.uuid4()}.{output_format}"
    output_path = os.path.join(temp_dir, output_filename)
    
    ext = os.path.splitext(input_path)[1].lower()
    extracted_text = ""
    
    try:
        if ext == '.pdf':
            import fitz
            doc = fitz.open(input_path)
            for page in doc:
                extracted_text += page.get_text() + "\n"
        elif ext in ['.docx', '.doc']:
            import docx
            doc = docx.Document(input_path)
            extracted_text = "\n".join([p.text for p in doc.paragraphs])
        else:
            with open(input_path, 'r', encoding='utf-8', errors='ignore') as f:
                extracted_text = f.read()
                
        if not extracted_text.strip():
            extracted_text = "No text content found in original file."

        if output_format == 'txt':
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(extracted_text)
            return output_path
            
        elif output_format == 'docx':
            import docx
            doc = docx.Document()
            for line in extracted_text.split('\n'):
                doc.add_paragraph(line)
            doc.save(output_path)
            return output_path
            
        elif output_format == 'pdf':
            import fitz
            doc = fitz.open()
            page = doc.new_page()
            margin = 50
            rect = fitz.Rect(margin, margin, page.rect.width - margin, page.rect.height - margin)
            page.insert_textbox(rect, extracted_text, fontsize=11)
            doc.save(output_path)
            return output_path
            
    except Exception as e:
        print(f"Error converting document: {e}")
        return None

def detect_speech_segments(input_path, min_silence_dur=0.4, noise_threshold=-28):
    """
    Detects start and end timestamps (seconds) of spoken parts in media using FFmpeg silencedetect.
    Returns list of tuples: [(start_sec, end_sec), ...]
    """
    duration = get_video_duration(input_path)
    if duration <= 0:
        return []
        
    try:
        import subprocess
        import re
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg_exe, "-i", input_path,
            "-af", f"silencedetect=noise={noise_threshold}dB:d={min_silence_dur}",
            "-f", "null", "-"
        ]
        res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore")
        
        silence_starts = []
        silence_ends = []
        
        for line in res.stderr.split('\n'):
            if "silence_start:" in line:
                m = re.search(r"silence_start:\s*([\d.]+)", line)
                if m:
                    silence_starts.append(float(m.group(1)))
            elif "silence_end:" in line:
                m = re.search(r"silence_end:\s*([\d.]+)", line)
                if m:
                    silence_ends.append(float(m.group(1)))
                    
        silences = []
        for i in range(min(len(silence_starts), len(silence_ends))):
            s_start = silence_starts[i]
            s_end = silence_ends[i]
            if s_end > s_start:
                silences.append((s_start, s_end))
                
        if not silences:
            return [(0.0, duration)]
            
        segments = []
        curr = 0.0
        for s_start, s_end in silences:
            if s_start > curr + 0.3:
                segments.append((curr, s_start))
            curr = s_end
            
        if curr < duration - 0.3:
            segments.append((curr, duration))
            
        return segments if segments else [(0.0, duration)]
    except Exception as e:
        print(f"Speech segment detection error: {e}")
        return []

def check_audio_rms(file_path):
    """
    Checks if an audio file has non-silent content using FFmpeg volumedetect filter.
    Returns True if max_volume > -45 dB, else False.
    """
    try:
        import subprocess
        import re
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg_exe, "-i", file_path,
            "-af", "volumedetect",
            "-f", "null", "-"
        ]
        res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore")
        match = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", res.stderr)
        if match:
            max_vol = float(match.group(1))
            return max_vol > -45.0
    except Exception as e:
        print(f"Error checking RMS volume: {e}")
    return os.path.exists(file_path) and os.path.getsize(file_path) > 500

def extract_bgm_demucs(input_path, output_path=None):
    """
    Uses Demucs v4 (htdemucs) AI stem separation to cleanly extract background music & sound effects
    (no_vocals stem) regardless of audio channel layout (mono or stereo).
    Fallback to DSP notch filtering if Demucs fails or BGM RMS volume is near silence (< -45dB).
    """
    temp_dir = get_temp_dir()
    if not output_path:
        output_path = os.path.join(temp_dir, f"{uuid.uuid4()}_bgm_demucs.mp3")
        
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    ffmpeg_dir = os.path.dirname(ffmpeg_exe)
    if ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
        
    import subprocess, shutil, sys
    
    # 1. Extract audio track to temporary WAV file (ensures Demucs & sphn can load cleanly)
    temp_input_wav = os.path.join(temp_dir, f"{uuid.uuid4()}_input_track.wav")
    demucs_out_dir = os.path.join(temp_dir, f"demucs_{uuid.uuid4().hex[:8]}")
    try:
        cmd_wav = [
            ffmpeg_exe, "-y", "-i", input_path,
            "-ar", "44100", "-ac", "2", temp_input_wav
        ]
        subprocess.run(cmd_wav, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        
        if os.path.exists(temp_input_wav) and os.path.getsize(temp_input_wav) > 1000:
            cmd = [
                sys.executable, "-m", "demucs.separate", "--two-stems=vocals", "-n", "htdemucs",
                temp_input_wav, "-o", demucs_out_dir
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            
            no_vocals_file = None
            for root, _, files in os.walk(demucs_out_dir):
                for f in files:
                    if "no_vocals" in f.lower():
                        no_vocals_file = os.path.join(root, f)
                        break
                if no_vocals_file:
                    break
                    
            if no_vocals_file and os.path.exists(no_vocals_file):
                cmd_cvt = [
                    ffmpeg_exe, "-y", "-i", no_vocals_file,
                    "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "192k",
                    output_path
                ]
                subprocess.run(cmd_cvt, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                shutil.rmtree(demucs_out_dir, ignore_errors=True)
                cleanup_file(temp_input_wav)
                if check_audio_rms(output_path):
                    return output_path
    except Exception as e:
        print(f"Demucs extraction error: {e}")
        shutil.rmtree(demucs_out_dir, ignore_errors=True)
        cleanup_file(temp_input_wav)

    # 2. Fallback Method: DSP notch & filter separation
    return extract_bgm_no_vocals_dsp(input_path, output_path)

def extract_bgm_no_vocals_dsp(input_path, output_path=None):
    """
    Ultra-fast DSP audio extraction for background music & sound effects.
    Cancels center-channel panned vocals and eliminates 300Hz-3.4kHz human speech frequencies.
    Runs in < 0.3s without CPU/memory bottlenecks.
    """
    temp_dir = get_temp_dir()
    if not output_path:
        output_path = os.path.join(temp_dir, f"{uuid.uuid4()}_bgm_dsp.mp3")
        
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    import subprocess
    
    af_filter = "pan=stereo|c0=0.5*c0-0.5*c1|c1=0.5*c1-0.5*c0, equalizer=f=300:width_type=h:width=400:g=-24, equalizer=f=1200:width_type=h:width=1800:g=-30, equalizer=f=2800:width_type=h:width=1200:g=-24, volume=1.8"
    try:
        cmd = [
            ffmpeg_exe, "-y", "-i", input_path,
            "-af", af_filter, "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "192k",
            output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if check_audio_rms(output_path):
            return output_path
    except Exception:
        pass

    try:
        af_notch = "equalizer=f=300:width_type=h:width=400:g=-24, equalizer=f=1200:width_type=h:width=1800:g=-30, equalizer=f=2800:width_type=h:width=1200:g=-24, volume=1.8"
        cmd = [
            ffmpeg_exe, "-y", "-i", input_path,
            "-af", af_notch, "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "192k",
            output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if check_audio_rms(output_path):
            return output_path
    except Exception:
        pass

    return None

def extract_bgm_no_vocals(input_path, output_path=None):
    """Wrapper function to extract BGM without vocals."""
    return extract_bgm_demucs(input_path, output_path)

def mix_tts_with_bgm(tts_audio_path, bgm_audio_path, target_dur=None, bgm_volume=0.25):
    """
    Mixes translated/recap AI speech track (volume 1.0) with original background music & sound effects track (volume bgm_volume) in 44.1kHz Stereo.
    """
    temp_dir = get_temp_dir()
    mixed_path = os.path.join(temp_dir, f"{uuid.uuid4()}_mixed_speech_bgm.mp3")
    
    if not bgm_audio_path or not os.path.exists(bgm_audio_path) or not check_audio_rms(bgm_audio_path):
        return tts_audio_path
        
    try:
        tts_dur = get_video_duration(tts_audio_path)
        bgm_dur = get_video_duration(bgm_audio_path)
        dur = target_dur or tts_dur or bgm_dur
        
        if bgm_dur > 0 and tts_dur > bgm_dur:
            bgm_in = ffmpeg.input(bgm_audio_path, stream_loop=-1).audio
        else:
            bgm_in = ffmpeg.input(bgm_audio_path).audio
            
        tts_in = ffmpeg.input(tts_audio_path).audio
        
        bgm_vol = bgm_in.filter('volume', bgm_volume)
        mixed_stream = ffmpeg.filter([tts_in, bgm_vol], 'amix', inputs=2, duration='first', dropout_transition=2)
        mixed_stream = mixed_stream.filter('aresample', 44100).filter('aformat', channel_layouts='stereo')
        
        out_opts = {'acodec': 'libmp3lame', 'b:a': '192k', 'ar': '44100', 'ac': 2}
        if dur and dur > 0:
            out_opts['t'] = dur
            
        stream = ffmpeg.output(mixed_stream, mixed_path, **out_opts).overwrite_output()
        _run_ffmpeg_with_progress(stream, mixed_path, None)
        
        if os.path.exists(mixed_path) and os.path.getsize(mixed_path) > 100:
            return mixed_path
    except Exception as e:
        print(f"Error mixing TTS with BGM: {e}")
        
    return tts_audio_path

def build_atempo_filter_chain(speed_ratio):
    """
    FFmpeg atempo filter requires values between 0.5 and 2.0.
    Chains multiple atempo filters for values outside this range.
    """
    speed_ratio = max(0.25, min(4.0, speed_ratio))
    if 0.5 <= speed_ratio <= 2.0:
        return [("atempo", speed_ratio)]
    elif speed_ratio > 2.0:
        f1 = 2.0
        f2 = speed_ratio / 2.0
        return [("atempo", f1), ("atempo", f2)]
    else:
        f1 = 0.5
        f2 = speed_ratio / 0.5
        return [("atempo", f1), ("atempo", f2)]

def generate_neural_tts(text, lang='km', output_path=None, rate=None):
    """
    Generates high-quality neural voice synthesis using edge-tts with robust gTTS fallback.
    """
    if not text or not text.strip():
        return None
        
    temp_dir = get_temp_dir()
    if not output_path:
        output_path = os.path.join(temp_dir, f"{uuid.uuid4()}_neural_tts.mp3")
        
    clean_lang = lang.lower().strip()[:2]
    clean_text = text.strip()
    
    # 1. Try edge-tts for Khmer, English, Chinese, etc.
    EDGE_VOICES = {
        'km': 'km-KH-SreymomNeural',
        'en': 'en-US-AvaNeural',
        'zh': 'zh-CN-XiaoxiaoNeural',
        'vi': 'vi-VN-HoaiMyNeural',
        'th': 'th-TH-PremwadeeNeural',
        'ko': 'ko-KR-SunHiNeural',
        'ja': 'ja-JP-NanamiNeural',
        'fr': 'fr-FR-DeniseNeural',
        'es': 'es-ES-ElviraNeural',
        'de': 'de-DE-KatjaNeural',
        'ru': 'ru-RU-SvetlanaNeural'
    }
    
    voice = EDGE_VOICES.get(clean_lang, 'km-KH-SreymomNeural' if clean_lang == 'km' else 'en-US-AvaNeural')
    if voice and clean_text:
        try:
            import edge_tts
            import asyncio
            async def _run_edge():
                kwargs = {"text": clean_text, "voice": voice}
                if rate:
                    kwargs["rate"] = rate
                communicate = edge_tts.Communicate(**kwargs)
                await communicate.save(output_path)
            try:
                loop = asyncio.get_running_loop()
                asyncio.run_coroutine_threadsafe(_run_edge(), loop).result(20)
            except RuntimeError:
                asyncio.run(_run_edge())
            if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                return output_path
        except Exception as e:
            print(f"edge-tts error: {e}")
            
    # 2. Fallback to gTTS
    try:
        from gtts import gTTS
        gtts_lang = 'zh-CN' if clean_lang == 'zh' else ('km' if clean_lang == 'km' else clean_lang)
        tts = gTTS(text=clean_text, lang=gtts_lang, slow=False)
        tts.save(output_path)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
            return output_path
    except Exception as e_gtts:
        print(f"gTTS error: {e_gtts}")
        
    return None

def translate_nllb_ct2(text, src_lang="zh", tgt_lang="km"):
    """
    Translates text using fast AI API / Google Translate GTX endpoint (0.1s response time).
    """
    if not text or not text.strip():
        return text

    LANG_NAMES = {'km': 'Khmer (ភាសាខ្មែរ)', 'en': 'English', 'zh': 'Chinese (中文)', 'vi': 'Vietnamese', 'th': 'Thai'}
    tgt_name = LANG_NAMES.get(tgt_lang[:2], 'Khmer')
    src_name = LANG_NAMES.get(src_lang[:2], 'Chinese')
    
    gemini_key = os.environ.get("GEMINI_API_KEY")
    
    # 1. Primary: Fast Gemini 2.0 Flash API
    if gemini_key:
        try:
            import requests
            prompt = f"Translate the following spoken dialogue from {src_name} into natural, accurate, conversational {tgt_name}. Output ONLY the raw translated spoken text with no markdown, no quotes, no explanations:\n\n{text}"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_key}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            res = requests.post(url, json=payload, timeout=6)
            if res.status_code == 200:
                data = res.json()
                tr_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                if tr_text and len(tr_text) > 0 and tr_text != text:
                    return tr_text
        except Exception as e_gem:
            print(f"Gemini translation error: {e_gem}")

    # 2. Fast Google Translate GTX Free Endpoint (0.1s response time, 100% accurate)
    try:
        import requests, urllib.parse
        s_code = src_lang[:2] if src_lang != "auto" else "auto"
        t_code = tgt_lang[:2]
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl={s_code}&tl={t_code}&dt=t&q={urllib.parse.quote(text)}"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            translated = "".join(seg[0] for seg in data[0] if seg and seg[0])
            if translated and len(translated.strip()) > 0:
                return translated.strip()
    except Exception as e_gtx:
        print(f"GTX translation error: {e_gtx}")

    return text

def transcribe_with_whisper_timestamps(input_path, src_lang="auto"):
    """
    Transcribes audio with Faster-Whisper to extract word & segment timestamps.
    Returns list of dicts: [{'start': start_sec, 'end': end_sec, 'text': text}, ...]
    """
    segments_list = []
    clean_src = None if src_lang == "auto" else src_lang[:2]
    
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("small", device="cpu", compute_type="int8")
        segments, info = model.transcribe(input_path, language=clean_src, beam_size=5)
        for segment in segments:
            text = segment.text.strip()
            if text:
                segments_list.append({
                    "start": round(segment.start, 2),
                    "end": round(segment.end, 2),
                    "text": text
                })
        if segments_list:
            return segments_list
    except Exception as e:
        print(f"Faster-Whisper error: {e}")
        
    return None

def translate_and_dub_media(input_path, target_lang='km', src_lang='auto', is_video=True, progress_callback=None):
    """
    Full AI Video Localization & Dubbing Pipeline (v3 - Mouth-Tracking Edition):
    1. Uses Faster-Whisper to extract exact per-segment timestamps from original speech.
    2. Translates each segment using Gemini/Google Translate.
    3. Synthesizes Neural TTS (edge-tts) per segment.
    4. Speed-compresses each TTS clip to FIT inside its original time slot (tts_dur / seg_dur as atempo).
    5. Delays each clip to its original start time so speech lines up with mouth movements.
    6. Mixes all delayed clips into a single full-length audio timeline.
    7. Skips BGM mixing if DSP vocal removal produces near-silence (avoids polluting audio).
    8. Applies loudnorm to match original audio level.
    9. Merges dubbed audio track with original video stream, locked to original duration.
    """
    temp_dir = get_temp_dir()
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    orig_dur = get_video_duration(input_path)

    if progress_callback: progress_callback("⚡ Transcribing speech with Faster-Whisper (timestamp alignment)...")

    # Step 1: Get timestamped segments from Whisper
    whisper_segs = transcribe_with_whisper_timestamps(input_path, src_lang=src_lang)

    # Fallback: use silencedetect-based segments if Whisper fails
    if not whisper_segs:
        if progress_callback: progress_callback("⚡ Falling back to silence-detect for timestamps...")
        raw_segments = detect_speech_segments(input_path)
        if raw_segments:
            from plugins.document_parser import transcribe_audio_video
            whisper_segs = []
            for (ss, se) in raw_segments:
                dur = se - ss
                if dur < 0.2: continue
                seg_slice = os.path.join(temp_dir, f"{uuid.uuid4()}_slice.mp3")
                try:
                    subprocess.run([ffmpeg_exe, "-y", "-ss", str(ss), "-t", str(dur), "-i", input_path, "-acodec", "libmp3lame", seg_slice], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                    txt = transcribe_audio_video(seg_slice, src_lang=src_lang)
                    cleanup_file(seg_slice)
                    if txt and len(txt.strip()) > 1 and "Error" not in txt:
                        whisper_segs.append({"start": ss, "end": se, "text": txt.strip()})
                except Exception:
                    cleanup_file(seg_slice)

    if not whisper_segs:
        # Last resort: full transcript, single segment spanning entire video
        from plugins.document_parser import transcribe_audio_video
        full_transcript = transcribe_audio_video(input_path, src_lang=src_lang)
        if not full_transcript or "Error" in full_transcript or len(full_transcript.strip()) < 2:
            return "ERROR: Could not extract speech from media to translate."
        whisper_segs = [{"start": 0.0, "end": orig_dur if orig_dur > 0 else 60.0, "text": full_transcript.strip()}]

    LANG_NAMES_MAP = {
        'km': 'Khmer (ភាសាខ្មែរ)', 'en': 'English', 'zh': 'Chinese (中文)',
        'vi': 'Vietnamese (Tiếng Việt)', 'th': 'Thai (ภាษាไទย)', 'ko': 'Korean (한국어)',
        'ja': 'Japanese (日本語)', 'fr': 'French (Français)', 'es': 'Spanish (Español)',
        'de': 'German (Deutsch)', 'ru': 'Russian (Русский)', 'ar': 'Arabic (العربية)',
        'hi': 'Hindi (हिन्दी)'
    }
    target_lang_name = LANG_NAMES_MAP.get(target_lang[:2], 'Khmer')

    if progress_callback: progress_callback(f"🧠 Translating & dubbing {len(whisper_segs)} speech segments to {target_lang_name}...")

    # Step 2: Per-segment translate → TTS → speed-compress → delay-pad
    delayed_segment_files = []
    for idx, seg in enumerate(whisper_segs):
        seg_start = seg["start"]
        seg_end = seg["end"]
        seg_dur = seg_end - seg_start
        src_text = seg.get("text", "").strip()

        if seg_dur < 0.15 or not src_text:
            continue

        try:
            # Translate
            if progress_callback and idx % 3 == 0:
                progress_callback(f"🔄 Segment {idx+1}/{len(whisper_segs)}: translating & dubbing...")
            clean_src = src_lang if src_lang != "auto" else "zh"
            txt_tr = translate_nllb_ct2(src_text, src_lang=clean_src, tgt_lang=target_lang)
            txt_tr = re.sub(r'[*#_~`>\[\]\(\)]', ' ', txt_tr or src_text).strip()
            if not txt_tr:
                continue

            # Generate Neural TTS
            raw_tts = generate_neural_tts(txt_tr, lang=target_lang[:2])
            if not raw_tts or not os.path.exists(raw_tts):
                continue

            tts_dur = get_video_duration(raw_tts)
            if tts_dur <= 0:
                cleanup_file(raw_tts)
                continue

            # Speed factor: atempo = tts_dur / seg_dur
            # atempo > 1 = speed UP (shorter output), atempo < 1 = slow DOWN (longer output)
            # Goal: compress TTS to fit within seg_dur → atempo = tts_dur / seg_dur
            speed_factor = tts_dur / max(0.15, seg_dur)
            speed_factor = max(0.4, min(3.0, speed_factor))

            delay_ms = int(seg_start * 1000)
            delayed_file = os.path.join(temp_dir, f"{uuid.uuid4()}_delayed_seg_{idx}.mp3")

            stream_in = ffmpeg.input(raw_tts).audio
            for f_name, f_val in build_atempo_filter_chain(speed_factor):
                stream_in = stream_in.filter(f_name, f_val)
            # Pad silence before segment to align with original mouth timing
            stream_in = stream_in.filter('adelay', delays=f"{delay_ms}|{delay_ms}")
            stream_in = stream_in.filter('aresample', 44100).filter('aformat', channel_layouts='stereo')

            ffmpeg.output(stream_in, delayed_file, acodec='libmp3lame', ar='44100', ac=2, **{'b:a': '192k'}).overwrite_output().run(
                cmd=ffmpeg_exe, capture_stdout=True, capture_stderr=True
            )
            cleanup_file(raw_tts)

            if os.path.exists(delayed_file) and os.path.getsize(delayed_file) > 100:
                delayed_segment_files.append(delayed_file)

        except Exception as e_seg:
            print(f"Error dubbing segment {idx}: {e_seg}")

    # Step 3: Mix all delayed segments into a single full-duration speech track
    final_speech_track = None
    if delayed_segment_files:
        if progress_callback: progress_callback("🎛️ Mixing all dubbed segments into full audio timeline...")
        combined_speech = os.path.join(temp_dir, f"{uuid.uuid4()}_dubbed_speech.mp3")
        try:
            if len(delayed_segment_files) == 1:
                shutil.copy(delayed_segment_files[0], combined_speech)
            else:
                inputs = [ffmpeg.input(f).audio for f in delayed_segment_files]
                ffmpeg.filter(inputs, 'amix', inputs=len(inputs), normalize=False, duration='longest').output(
                    combined_speech, acodec='libmp3lame', ar='44100', ac=2, **{'b:a': '192k'}
                ).overwrite_output().run(cmd=ffmpeg_exe, capture_stdout=True, capture_stderr=True)

                final_speech_track = combined_speech
        except Exception as e_mix:
            print(f"Error mixing delayed segments: {e_mix}")
            if delayed_segment_files:
                final_speech_track = delayed_segment_files[0]

    if not final_speech_track:
        return "ERROR: Failed to generate dubbed speech track."

    # Step 4: Try BGM isolation — skip if DSP produces near-silence (avoids polluting audio)
    final_audio = final_speech_track
    try:
        if progress_callback: progress_callback("🎵 Attempting background music isolation...")
        bgm_path = extract_bgm_no_vocals_dsp(input_path)
        if bgm_path and os.path.exists(bgm_path) and check_audio_rms(bgm_path):
            if progress_callback: progress_callback("🎧 Mixing voice with background music (35% BGM)...")
            mixed = mix_tts_with_bgm(final_speech_track, bgm_path, bgm_volume=0.35)
            if mixed and os.path.exists(mixed):
                final_audio = mixed
            cleanup_file(bgm_path)
        else:
            if bgm_path:
                cleanup_file(bgm_path)
            if progress_callback: progress_callback("ℹ️ No background music detected — using pure voice dub.")
    except Exception as e_bgm:
        print(f"BGM isolation skipped: {e_bgm}")

    # Step 5: Apply loudnorm to match original audio loudness (-16 LUFS broadcast standard)
    if progress_callback: progress_callback("🔊 Normalizing audio loudness to match original video...")
    loudnorm_path = os.path.join(temp_dir, f"{uuid.uuid4()}_loudnorm.mp3")
    try:
        subprocess.run(
            [ffmpeg_exe, "-y", "-i", final_audio,
             "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "192k",
             loudnorm_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30
        )
        if os.path.exists(loudnorm_path) and os.path.getsize(loudnorm_path) > 100:
            final_audio = loudnorm_path
    except Exception as e_ln:
        print(f"Loudnorm failed: {e_ln}")

    # Step 6: Merge dubbed audio with original video (locked to original duration)
    output_ext = "mp4" if is_video else "mp3"
    output_path = os.path.join(temp_dir, f"{uuid.uuid4()}_dubbed.{output_ext}")

    if is_video:
        if progress_callback: progress_callback("🎬 Merging dubbed audio with original video...")
        try:
            out_opts = {'vcodec': 'copy', 'acodec': 'aac', 'b:a': '192k', 'ar': '44100', 'ac': 2}
            if orig_dur and orig_dur > 0:
                out_opts['t'] = orig_dur

            video_in = ffmpeg.input(input_path).video
            audio_in = ffmpeg.input(final_audio).audio
            stream = ffmpeg.output(video_in, audio_in, output_path, **out_opts).overwrite_output()
            res = _run_ffmpeg_with_progress(stream, output_path, progress_callback)
        except Exception as e_merge:
            print(f"FFmpeg merge error: {e_merge}")
            res = None
    else:
        shutil.copy(final_audio, output_path)
        res = output_path

    # Cleanup temp files
    for sf in delayed_segment_files:
        cleanup_file(sf)
    if final_speech_track != final_audio:
        cleanup_file(final_speech_track)
    cleanup_file(final_audio)

    if res and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
        return output_path

    return "ERROR: Video dubbing process failed."

def generate_neural_tts(text, lang='km', rate=None, output_path=None):
    """
    Generates high-quality neural voice synthesis. Uses edge-tts (km-KH-SreymomNeural) for Khmer with gTTS fallback.
    Supports optional rate control (e.g. rate="-8%" for dramatic narration).
    """
    temp_dir = get_temp_dir()
    if not output_path:
        output_path = os.path.join(temp_dir, f"{uuid.uuid4()}_neural_tts.mp3")
        
    clean_lang = lang.lower().strip()[:2]
    
    EDGE_VOICES = {
        'km': 'km-KH-SreymomNeural',
        'en': 'en-US-AvaNeural',
        'zh': 'zh-CN-XiaoxiaoNeural',
        'vi': 'vi-VN-HoaiMyNeural',
        'th': 'th-TH-PremwadeeNeural',
        'ko': 'ko-KR-SunHiNeural',
        'ja': 'ja-JP-NanamiNeural',
        'fr': 'fr-FR-DeniseNeural',
        'es': 'es-ES-ElviraNeural',
        'de': 'de-DE-KatjaNeural',
        'ru': 'ru-RU-SvetlanaNeural'
    }
    
    voice = EDGE_VOICES.get(clean_lang, 'km-KH-SreymomNeural' if clean_lang == 'km' else None)
    if voice:
        try:
            import edge_tts
            import asyncio
            async def _run_edge():
                kwargs = {"text": text, "voice": voice}
                if rate:
                    kwargs["rate"] = rate
                communicate = edge_tts.Communicate(**kwargs)
                await communicate.save(output_path)
            try:
                loop = asyncio.get_running_loop()
                asyncio.run_coroutine_threadsafe(_run_edge(), loop).result(20)
            except RuntimeError:
                asyncio.run(_run_edge())
            if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                return output_path
        except Exception as e:
            print(f"edge-tts error: {e}")
            
    # Fallback to gTTS
    try:
        from gtts import gTTS
        gtts_lang = 'zh-CN' if clean_lang == 'zh' else ('km' if clean_lang == 'km' else clean_lang)
        tts = gTTS(text=text, lang=gtts_lang, slow=False)
        tts.save(output_path)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
            return output_path
    except Exception as e_gtts:
        print(f"gTTS error: {e_gtts}")
        
    return None

def mix_recap_sidechain_ducking(bgm_wav_path, narration_wav_path, output_audio_path=None):
    """
    Applies dynamic FFmpeg sidechaincompress audio ducking to automatically lower background music & sound effects (no_vocals stem)
    whenever the narrator speaks, then mixes narrator audio with ducked BGM at 44.1kHz Stereo.
    """
    temp_dir = get_temp_dir()
    if not output_audio_path:
        output_audio_path = os.path.join(temp_dir, f"{uuid.uuid4()}_ducked_recap.mp3")
        
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    import subprocess
    
    filter_graph = (
        "[0:a]volume=0.85[bgm]; "
        "[1:a]asplit[narrator][sc]; "
        "[bgm][sc]sidechaincompress=threshold=0.08:ratio=4:attack=100:release=400[ducked_bgm]; "
        "[ducked_bgm][narrator]amix=inputs=2:duration=first,aresample=44100,aformat=channel_layouts=stereo[outa]"
    )
    
    cmd = [
        ffmpeg_exe, "-y",
        "-i", bgm_wav_path,
        "-i", narration_wav_path,
        "-filter_complex", filter_graph,
        "-map", "[outa]",
        "-acodec", "libmp3lame",
        "-ar", "44100",
        "-ac", "2",
        "-b:a", "192k",
        output_audio_path
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
        if os.path.exists(output_audio_path) and os.path.getsize(output_audio_path) > 100:
            return output_audio_path
    except Exception as e:
        print(f"Sidechain ducking error: {e}")
        
    return mix_tts_with_bgm(narration_wav_path, bgm_wav_path, bgm_volume=0.25)

def clean_recap_speech_text(text):
    """Strips markdown, emojis, bullet points, numbers, and section headers to leave clean spoken recap text only."""
    t = re.sub(r'[\*\_~`>#]', ' ', text)
    t = re.sub(r'[📌💡🎯🎬▶✔✅⭐🏆•\-]\s*', ' ', t)
    t = re.sub(r'^\s*\d+[\.\)]\s*', ' ', t, flags=re.MULTILINE)
    t = re.sub(r'(Core Concept|Main Story Meaning|Detailed Scene-by-Scene|Key Point Breakdown|Key Highlights|Main Points|Core Topic|Story Overview|Discussion|Important Insights|Lessons|Final Conclusion|Main Takeaway|Summary|Executive Overview|Chronological Scene|Deep Analytical Insights|សង្ខេបលម្អិត|ខ្លឹមសារ|សេចក្តីសន្និដ្ឋាន)\s*[:\&]*', ' ', t, flags=re.IGNORECASE)
    t = re.sub(r'^(Here is|Summary|Recap|Khmer|English|Note):.*$', '', t, flags=re.MULTILINE | re.IGNORECASE)
    lines = [line.strip() for line in t.split('\n') if line.strip()]
    cleaned = ". ".join(lines)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def recap_video_audio(input_path, target_lang='km', src_lang='auto', is_video=True, voiceover=False, progress_callback=None):
    """
    Automated Movie Recap Generation Pipeline:
    1. Transcribes media with Faster-Whisper ASR to extract scene timestamps and transcript.
    2. Uses Gemini 2.0 Flash to generate a dramatic 3rd-person narrator recap script & executive breakdown in target language.
    3. Synthesizes dramatic cinematic voiceover using edge-tts (km-KH-SreymomNeural, rate="-8%").
    4. Separates vocals and ambient BGM with Demucs v4 stem separation, discarding original vocals.
    5. Applies dynamic FFmpeg sidechaincompress audio ducking so BGM lowers whenever the narrator speaks.
    6. Cuts key scenes & matches video speed to narration.
    """
    temp_dir = get_temp_dir()
    
    if progress_callback: progress_callback("⚡ Transcribing scenes with Faster-Whisper ASR...")
    whisper_segs = transcribe_with_whisper_timestamps(input_path, src_lang=src_lang)
    
    from plugins.document_parser import transcribe_audio_video
    transcript = transcribe_audio_video(input_path, src_lang=src_lang)
    
    if not transcript or "Error" in transcript or "Unsupported" in transcript or len(transcript.strip()) < 3:
        return "ERROR: Could not extract speech from media to generate recap.", None
        
    LANG_NAMES_MAP = {
        'km': 'Khmer (ភាសាខ្មែរ)',
        'en': 'English',
        'zh': 'Chinese (中文)',
        'vi': 'Vietnamese (Tiếng Việt)',
        'th': 'Thai (ภาษาไทย)',
        'ko': 'Korean (한국어)',
        'ja': 'Japanese (日本語)',
        'fr': 'French (Français)',
        'es': 'Spanish (Español)',
        'de': 'German (Deutsch)',
        'ru': 'Russian (Русский)',
        'ar': 'Arabic (العربية)',
        'hi': 'Hindi (हिन्दी)'
    }
    target_lang_name = LANG_NAMES_MAP.get(target_lang[:2], 'Khmer')
    
    if progress_callback: progress_callback(f"🧠 Generating Cinematic Movie Recap Script in {target_lang_name} (Gemini 2.0 Flash)...")
    
    import asyncio
    from plugins.core import get_ai_response
    prompt = (
        f"You are Udom AI, an elite film director and movie recap narrator.\n"
        f"Generate a HIGHLY DETAILED, EXECUTIVE-GRADE PROFESSIONAL MOVIE RECAP & SYNTHESIS of the following video transcript in {target_lang_name}.\n\n"
        f"CRITICAL REQUIREMENTS:\n"
        f"1. Explain the FULL STORY context, background setting, character motivations, plot twists, and main message with extreme narrative flair.\n"
        f"2. Detail EVERY MAJOR SCENE in chronological sequence so the audience grasps every plot point without watching the original.\n"
        f"3. Provide deep analytical insights, core takeaways, and professional summary conclusions.\n\n"
        f"Structure your analysis in {target_lang_name} using these exact sections:\n\n"
        f"🏆 **1. EXECUTIVE OVERVIEW & CORE MEANING (ខ្លឹមសារ និងអត្ថន័យដើមនៃវីដេអូ)**\n"
        f"• Complete breakdown of what the movie is about, the main theme, background context, and core story purpose.\n\n"
        f"🎬 **2. CHRONOLOGICAL SCENE & HIGHLIGHT BREAKDOWN (សង្ខេបលម្អិតតាមចំនុច និងសាច់រឿង)**\n"
        f"• Detailed bullet points explaining key scenes, actions, plot progression, and climax.\n\n"
        f"💡 **3. DEEP ANALYTICAL INSIGHTS & LESSONS (ការវិភាគយ៉ាងស៊ីជម្រៅ និងមេរៀន)**\n"
        f"• Expert cinematic analysis of character arcs, philosophical messages, and hidden details.\n\n"
        f"🎯 **4. FINAL CONCLUSION & KEY TAKEAWAYS (សេចក្តីសន្និដ្ឋាន និងសារៈសំខាន់)**\n"
        f"• Final executive conclusion and summary takeaway.\n\n"
        f"Transcript:\n{transcript}"
    )
    
    try:
        try:
            loop = asyncio.get_running_loop()
            recap_text = asyncio.run_coroutine_threadsafe(get_ai_response(0, prompt), loop).result(45)
        except RuntimeError:
            recap_text = asyncio.run(get_ai_response(0, prompt))
    except Exception as e:
        print(f"AI Recap Error: {e}")
        recap_text = f"Failed to generate recap: {e}"
        
    if not voiceover:
        return recap_text, None
        
    # Generate Dramatic 3rd-Person Voiceover Script
    if progress_callback: progress_callback("🎙 Scripting Dramatic 3rd-Person Narrator Voiceover...")
    vo_script_prompt = (
        f"You are Udom AI, a world-class documentary and movie recap narrator.\n"
        f"Write a smooth, highly engaging, dramatic 3rd-person spoken RECAP VOICE-OVER NARRATION in {target_lang_name} based on this transcript.\n\n"
        f"GUIDELINES:\n"
        f"1. Narrative Arc: Start with a captivating hook, describe scene developments in dramatic chronological order, and finish with a memorable conclusion.\n"
        f"2. Style: Write in natural, fluent, cinematic spoken narration like a professional YouTube movie recap narrator.\n"
        f"3. Pure Spoken Text: Output ONLY pure spoken sentences in {target_lang_name}. Do NOT include section titles, numbers, bullet symbols, emojis, markdown asterisks, or quotes.\n\n"
        f"Transcript:\n{transcript}"
    )
    
    try:
        try:
            loop = asyncio.get_running_loop()
            raw_script = asyncio.run_coroutine_threadsafe(get_ai_response(0, vo_script_prompt), loop).result(30)
        except RuntimeError:
            raw_script = asyncio.run(get_ai_response(0, vo_script_prompt))
    except Exception:
        raw_script = recap_text
        
    tts_speech = clean_recap_speech_text(raw_script)
    if not tts_speech:
        tts_speech = clean_recap_speech_text(recap_text)
        
    if progress_callback: progress_callback("🎙 Synthesizing Cinematic Neural Narrator Voice (edge-tts -8% speed)...")
    raw_tts = generate_neural_tts(tts_speech, lang=target_lang[:2], rate="-8%")
    if not raw_tts or not os.path.exists(raw_tts):
        return recap_text, None
        
    # Isolate original BGM & Sound effects using Demucs v4 (discarding original vocals)
    if progress_callback: progress_callback("🎵 Isolating Ambient BGM & SFX with Demucs v4 (discarding original vocals)...")
    bgm_path = extract_bgm_demucs(input_path)
    
    orig_dur = get_video_duration(input_path)
    tts_dur = get_video_duration(raw_tts)
    target_dur = tts_dur if tts_dur > 0 else (orig_dur if orig_dur > 0 else 60.0)
    
    # Dynamic Sidechain Audio Ducking
    final_recap_audio = raw_tts
    if bgm_path and os.path.exists(bgm_path) and check_audio_rms(bgm_path):
        if progress_callback: progress_callback("🎧 Applying Dynamic Sidechain Audio Ducking (narrator ducking BGM volume)...")
        ducked = mix_recap_sidechain_ducking(bgm_path, raw_tts)
        if ducked and os.path.exists(ducked):
            final_recap_audio = ducked
        
    if is_video and orig_dur > 0 and target_dur > 0:
        if progress_callback: progress_callback("🎬 Edit & Speed Engine: Syncing video speed & scenes to narration...")
        
        output_path = os.path.join(temp_dir, f"{uuid.uuid4()}_movie_recap.mp4")
        
        speech_segments = whisper_segs if whisper_segs else detect_speech_segments(input_path)
        cut_video_path = None
        
        try:
            if speech_segments and len(speech_segments) > 1:
                segment_clips = []
                segs = speech_segments if isinstance(speech_segments[0], tuple) else [(s["start"], s["end"]) for s in speech_segments]
                for s_idx, (s_start, s_end) in enumerate(segs[:12]):
                    c_dur = s_end - s_start
                    if c_dur < 0.3: continue
                    c_out = os.path.join(temp_dir, f"{uuid.uuid4()}_recap_clip_{s_idx}.mp4")
                    try:
                        (
                            ffmpeg
                            .input(input_path, ss=s_start, t=c_dur)
                            .output(c_out, vcodec='copy', an=None)
                            .overwrite_output()
                            .run(cmd=imageio_ffmpeg.get_ffmpeg_exe(), capture_stdout=True, capture_stderr=True)
                        )
                        if os.path.exists(c_out) and os.path.getsize(c_out) > 100:
                            segment_clips.append(c_out)
                    except Exception:
                        pass
                
                if segment_clips:
                    concat_txt = os.path.join(temp_dir, f"{uuid.uuid4()}_recap_list.txt")
                    with open(concat_txt, "w", encoding="utf-8") as f_lst:
                        for clp in segment_clips:
                            f_lst.write(f"file '{clp}'\n")
                            
                    concat_raw = os.path.join(temp_dir, f"{uuid.uuid4()}_recap_concat.mp4")
                    try:
                        (
                            ffmpeg
                            .input(concat_txt, format='concat', safe=0)
                            .output(concat_raw, vcodec='copy', an=None)
                            .overwrite_output()
                            .run(cmd=imageio_ffmpeg.get_ffmpeg_exe(), capture_stdout=True, capture_stderr=True)
                        )
                        if os.path.exists(concat_raw) and os.path.getsize(concat_raw) > 100:
                            cut_video_path = concat_raw
                    except Exception:
                        pass
                    finally:
                        cleanup_file(concat_txt)
                        for clp in segment_clips:
                            cleanup_file(clp)

            source_v = cut_video_path if cut_video_path else input_path
            source_v_dur = get_video_duration(source_v) or orig_dur
            pts_factor = max(0.5, min(2.0, target_dur / max(1.0, source_v_dur)))
            
            video_input = ffmpeg.input(source_v).video.filter('setpts', f'{pts_factor}*PTS')
            audio_input = ffmpeg.input(final_recap_audio).audio
            
            out_opts = {'vcodec': 'libx264', 'acodec': 'aac', 'pix_fmt': 'yuv420p', 'ar': '44100', 'b:a': '192k', 't': target_dur}
            stream = (
                ffmpeg
                .output(video_input, audio_input, output_path, **out_opts)
                .overwrite_output()
            )
            res_media = _run_ffmpeg_with_progress(stream, output_path, progress_callback)
            if cut_video_path:
                cleanup_file(cut_video_path)
        except Exception as e_edit:
            print(f"Edit & Speed Engine Error: {e_edit}")
            try:
                video_input = ffmpeg.input(input_path, stream_loop=-1).video if (orig_dur > 0 and target_dur > orig_dur) else ffmpeg.input(input_path).video
                audio_input = ffmpeg.input(final_recap_audio).audio
                out_opts_fb = {'vcodec': 'copy', 'acodec': 'aac', 'ar': '44100', 'b:a': '192k', 't': target_dur}
                stream = ffmpeg.output(video_input, audio_input, output_path, **out_opts_fb).overwrite_output()
                res_media = _run_ffmpeg_with_progress(stream, output_path, progress_callback)
            except Exception:
                res_media = final_recap_audio
    else:
        res_media = final_recap_audio
        
    cleanup_file(raw_tts)
    if bgm_path:
        cleanup_file(bgm_path)
        
    return recap_text, res_media

def get_video_duration(file_path):
    """Accurately extracts video or audio duration in seconds using imageio_ffmpeg or ffprobe."""
    try:
        import subprocess
        import re
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ffmpeg_exe, "-i", file_path]
        res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore")
        match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", res.stderr)
        if match:
            hours, minutes, seconds = match.groups()
            return float(hours) * 3600 + float(minutes) * 60 + float(seconds)
    except Exception as e:
        print(f"Error getting duration with imageio_ffmpeg: {e}")
        
    try:
        probe = ffmpeg.probe(file_path)
        return float(probe['format']['duration'])
    except Exception as e:
        print(f"Error getting video duration: {e}")
        return 0.0

def clip_video_into_parts(input_path, num_clips=3, progress_callback=None):
    """
    Splits a video file into num_clips equal segment files using FFmpeg.
    """
    temp_dir = get_temp_dir()
    duration = get_video_duration(input_path)
    
    if duration <= 0:
        return []
        
    clip_duration = duration / max(1, num_clips)
    output_files = []
    
    for i in range(num_clips):
        start_time = i * clip_duration
        if progress_callback:
            progress_callback(f"✂️ Cutting video clip {i+1} of {num_clips}...")
            
        out_file = os.path.join(temp_dir, f"{uuid.uuid4()}_clip_{i+1}.mp4")
        try:
            stream = (
                ffmpeg
                .input(input_path, ss=start_time, t=clip_duration)
                .output(out_file, c='copy')
                .overwrite_output()
            )
            ffmpeg.run(stream, cmd=imageio_ffmpeg.get_ffmpeg_exe(), capture_stdout=True, capture_stderr=True)
            if os.path.exists(out_file) and os.path.getsize(out_file) > 0:
                output_files.append(out_file)
        except Exception as e:
            print(f"Error creating clip {i+1}: {e}")
            
    return output_files
