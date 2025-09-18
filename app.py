import os
import re
import json
import logging
import subprocess
import threading
import time
import random
import signal
import uuid

from collections import deque
from urllib.parse import urlparse

import redis
from flask import Flask, request, render_template, jsonify

# --------------------------
# Configuration
# --------------------------

DEBUG_MODE = os.getenv('DEBUG', 'False').lower() == 'true'
MAX_LOG_LINES = int(os.getenv('MAX_LOG_LINES', '500'))
VIDEO_FOLDER = os.getenv('VIDEO_FOLDER', '/app/videos')
MAX_RESTARTS = int(os.getenv('MAX_RESTARTS', '10'))
STALL_TIMEOUT = int(os.getenv('STALL_TIMEOUT', '30'))  # seconds of no log activity => restart

# Duplicate the shuffled order this many times to make a very long run
PLAYLIST_REPEAT = int(os.getenv('PLAYLIST_REPEAT', '25'))

# Redis config (optional persistence of form config)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
STREAM_CONFIG_KEY = "rerun_stream_config"

# Create video folder if it doesn't exist
os.makedirs(VIDEO_FOLDER, exist_ok=True)

# Setup logging
logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Flask app
app = Flask(__name__)

# --------------------------
# Runtime state and logs
# --------------------------

log_buffer = deque(maxlen=MAX_LOG_LINES)

class StreamState:
    """Thread-safe stream state management"""

    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.restarts = 0
        self.start_time = None
        self.last_error = None
        self.process_id = None
        self.uptime = 0

    def set_running(self, running):
        with self._lock:
            self.running = running
            if running:
                self.start_time = time.time()
            else:
                if self.start_time:
                    self.uptime += time.time() - self.start_time
                self.start_time = None

    def increment_restarts(self):
        with self._lock:
            self.restarts += 1

    def set_error(self, error):
        with self._lock:
            self.last_error = error

    def reset(self):
        with self._lock:
            self.running = False
            self.start_time = None
            self.last_error = None
            self.process_id = None

    def get_status(self):
        with self._lock:
            current_uptime = self.uptime
            if self.running and self.start_time:
                current_uptime += time.time() - self.start_time
            return {
                'running': self.running,
                'restarts': self.restarts,
                'uptime': current_uptime,
                'last_error': self.last_error,
                'process_id': self.process_id
            }

# Global process/supervisor state
stream_state = StreamState()
ffmpeg_process = None
ffmpeg_thread = None
stop_requested = threading.Event()

# --------------------------
# Business rules and presets
# --------------------------

VALID_EXTS = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm')
X264_PRESETS = ["ultrafast","superfast","veryfast","faster","fast","medium","slow","slower","veryslow","placebo"]
NVENC_PRESETS = ["p1","p2","p3","p4","p5","p6","p7"]
VAAPI_CL_RANGE = [str(i) for i in range(1, 8)]

DEFAULTS = {
    "stream_key": "",
    "bitrate": "2500k",
    "input_type": "file",     # "file" or "srt"
    "video": "",
    "srt_url": "",
    "encoder": "libx264",     # "libx264", "h264_nvenc", "h264_vaapi"
    "preset": "medium",
    "category": "",
    "shuffle": "off",
}

# Old-to-new field name synonyms (compat with older templates)
FIELD_SYNONYMS = {
    "stream_key": ["stream_key", "streamkey"],
    "bitrate": ["bitrate"],
    "input_type": ["input_type", "inputtype"],
    "video": ["video"],
    "srt_url": ["srt_url", "srturl"],
    "encoder": ["encoder"],
    "preset": ["preset"],
    "category": ["category"],
    "shuffle": ["shuffle"],
}

# --------------------------
# Validators and helpers
# --------------------------

def parse_form_from_request(req_form):
    data = {}
    for new_name, aliases in FIELD_SYNONYMS.items():
        val = None
        for name in aliases:
            if name in req_form:
                val = req_form.get(name)
                break
        if val is None:
            val = DEFAULTS.get(new_name, "")
        if isinstance(val, str):
            val = val.strip()
        data[new_name] = val
    return data

def validate_video_filename(filename):
    if not filename:
        return False
    return filename.lower().endswith(VALID_EXTS)

def validate_bitrate(bitrate):
    if not bitrate:
        return False
    return bool(re.match(r'^\d+k?$', bitrate.strip()))

def validate_srt_url(url):
    if not url:
        return False
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme.lower() in ['srt', 'rtmp', 'rtmps', 'udp']
    except Exception as e:
        logger.debug(f"URL validation failed for {url}: {e}")
        return False

def validate_encoder(encoder):
    return encoder in ['libx264', 'h264_nvenc', 'h264_vaapi']

def _is_valid_video_file(path):
    try:
        return os.path.isfile(path) and validate_video_filename(os.path.basename(path))
    except Exception:
        return False

def _category_root(category):
    return os.path.join(VIDEO_FOLDER, category)

def _safe_resolve_under(root, relpath):
    root_abs = os.path.abspath(root)
    full = os.path.abspath(os.path.normpath(os.path.join(root_abs, relpath)))
    if full != root_abs and not full.startswith(root_abs + os.sep):
        raise ValueError("Path traversal detected")
    return full

def _iter_category_files_recursive(category):
    root = _category_root(category)
    if not os.path.isdir(root):
        return
    for dirpath, _, filenames in os.walk(root):
        for f in filenames:
            if f.lower().endswith(VALID_EXTS):
                full = os.path.join(dirpath, f)
                if os.path.isfile(full):
                    rel = os.path.relpath(full, root)
                    yield rel

def list_categories():
    cats = []
    if os.path.isdir(VIDEO_FOLDER):
        for name in sorted(os.listdir(VIDEO_FOLDER)):
            if os.path.isdir(os.path.join(VIDEO_FOLDER, name)):
                try:
                    for _ in _iter_category_files_recursive(name):
                        cats.append(name)
                        break
                except Exception:
                    pass
    return cats

def list_videos_in_category(category):
    try:
        files = list(_iter_category_files_recursive(category))
        files.sort()
        return files
    except Exception:
        return []

def total_video_count():
    cats = list_categories()
    return sum(len(list_videos_in_category(c)) for c in cats)

def parse_bitrate_k(bitrate):
    bitrate = bitrate.strip()
    if bitrate.endswith('k') and bitrate[:-1].isdigit():
        return int(bitrate[:-1])
    elif bitrate.isdigit():
        return int(bitrate)
    return None

def check_hardware_encoder_availability(encoder):
    if encoder == "h264_vaapi":
        if not os.path.exists("/dev/dri/renderD128"):
            return False, "Intel VAAPI device (/dev/dri/renderD128) not found"
    return True, None

def normalize_preset(encoder, preset_value):
    p = (preset_value or "").strip().lower()
    if encoder == "libx264":
        if p not in X264_PRESETS:
            p = "medium"
        return (["-preset", p], f"x264:{p}")
    elif encoder == "h264_nvenc":
        if p not in NVENC_PRESETS:
            p = "p5"
        return (["-preset", p], f"nvenc:{p}")
    elif encoder == "h264_vaapi":
        if p not in VAAPI_CL_RANGE:
            p = "4"
        return (["-compression_level", p], f"vaapi:cl{p}")
    else:
        return ([], "unknown")

def _unique_playlist_path():
    return f"/tmp/playlist_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}.txt"

def _write_concat_playlist(paths, playlist_path=None):
    if playlist_path is None:
        playlist_path = _unique_playlist_path()
    with open(playlist_path, "w", encoding="utf-8") as f:
        for p in paths:
            safe_p = p.replace("'", r"'\''")
            f.write(f"file '{safe_p}'\n")
    return playlist_path

def _cleanup_old_playlists():
    try:
        now = time.time()
        for name in os.listdir("/tmp"):
            if name.startswith("playlist_") and name.endswith(".txt"):
                path = os.path.join("/tmp", name)
                try:
                    st = os.stat(path)
                    if now - st.st_mtime > 3600:  # older than 1 hour
                        os.remove(path)
                except Exception:
                    pass
    except Exception:
        pass

def _output_format_for_url(url):
    scheme = urlparse(url).scheme.lower()
    if scheme.startswith("rtmp"):
        return "flv"
    elif scheme in ("srt", "udp"):
        return "mpegts"
    return "flv"

def build_cmd(input_args, encoder, preset, bitrate, out_url):
    if not validate_bitrate(bitrate):
        raise ValueError(f"Invalid bitrate format: {bitrate}")
    if not validate_encoder(encoder):
        raise ValueError(f"Invalid encoder: {encoder}")

    available, error_msg = check_hardware_encoder_availability(encoder)
    if not available:
        raise ValueError(error_msg)

    bk = parse_bitrate_k(bitrate)
    buf = f"{bk*2}k" if bk else f"{int(bitrate)*2}" if bitrate.isdigit() else bitrate
    enc_args, preset_label = normalize_preset(encoder, preset)

    common_rc = [
        "-b:v", bitrate,
        "-maxrate", bitrate,
        "-bufsize", buf,
        "-g", "120",
        "-force_key_frames", "expr:gte(t,n_forced*2)"
    ]

    audio = [
        "-af", "aresample=async=1:min_hard_comp=0.100:first_pts=0",
        "-c:a", "aac",
        "-b:a", "160k",
        "-ar", "48000",
        "-ac", "2"
    ]

    ts_flags = [
        "-fflags", "+genpts",
        "-avoid_negative_ts", "make_zero",
        "-use_wallclock_as_timestamps", "1",
        "-muxdelay", "0",
        "-muxpreload", "0",
    ]

    pre = []
    vf_chain = []
    vcodec = []

    if encoder == "h264_nvenc":
        vcodec = ["-c:v", "h264_nvenc", "-rc", "cbr", "-pix_fmt", "yuv420p"] + enc_args
        vf_chain.append("fps=30")
    elif encoder == "h264_vaapi":
        pre = ["-vaapi_device", "/dev/dri/renderD128"]
        vf_chain += ["format=nv12", "hwupload", "fps=30"]
        vcodec = ["-c:v", "h264_vaapi", "-bf", "2"] + enc_args
    else:  # libx264
        vcodec = ["-c:v", "libx264", "-pix_fmt", "yuv420p"] + enc_args
        vf_chain.append("fps=30")

    vf = ["-vf", ",".join(vf_chain)] if vf_chain else []

    out_fmt = _output_format_for_url(out_url)
    flv_live = ["-flvflags", "no_duration_filesize"] if out_fmt == "flv" else []

    cmd = (["ffmpeg", "-loglevel", "verbose"] +
           pre + input_args + vcodec + common_rc + vf + audio + ts_flags + flv_live +
           ["-f", out_fmt, out_url])

    logger.info(f"Using preset mapping: {preset_label}")
    logger.info(f"Built FFmpeg command: {' '.join(cmd[:14])}...")
    return cmd

# --------------------------
# Supervisor with stall watchdog and fast EOF restart
# --------------------------

def start_supervised(cmd, loop_forever=True):
    global ffmpeg_process
    backoff = 2
    restarts = 0

    while not stop_requested.is_set():
        stream_state.set_running(True)
        stalled = False
        last_line_ts = time.time()

        try:
            logger.info(f"Starting FFmpeg (attempt {restarts + 1})")
            ffmpeg_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
                preexec_fn=os.setsid if os.name != 'nt' else None  # separate process group
            )
            stream_state.process_id = ffmpeg_process.pid
            log_buffer.append(f"[supervisor] Started FFmpeg (PID: {ffmpeg_process.pid})")

            # Read combined stdout/stderr lines
            for line in iter(ffmpeg_process.stdout.readline, ''):
                if stop_requested.is_set():
                    break
                line = line.rstrip("\n")
                if line:
                    log_buffer.append(line)
                    last_line_ts = time.time()

                # Watchdog: restart if no progress
                if (time.time() - last_line_ts) > STALL_TIMEOUT:
                    stalled = True
                    log_buffer.append(f"[watchdog] No log activity for {STALL_TIMEOUT}s, restarting")
                    try:
                        os.killpg(ffmpeg_process.pid, signal.SIGKILL)
                    except Exception:
                        pass
                    break

            rc = ffmpeg_process.wait()
            stream_state.set_running(False)

            if stop_requested.is_set():
                log_buffer.append("[supervisor] Stop requested; exiting")
                break

            # Fast restart on normal EOF when looping
            if loop_forever and rc == 0 and not stalled:
                restarts += 1
                stream_state.increment_restarts()
                backoff = 2  # reset backoff for EOF
                log_buffer.append("[supervisor] EOF reached; immediate restart (no backoff)")
                _cleanup_old_playlists()
                continue

            # Abnormal exit or stalled: backoff then restart (if allowed)
            restarts += 1
            stream_state.increment_restarts()
            if not loop_forever and restarts >= MAX_RESTARTS:
                log_buffer.append(f"[supervisor] Max restarts ({MAX_RESTARTS}) reached; stopping")
                stream_state.set_error(f"Max restarts ({MAX_RESTARTS}) reached")
                break

            delay = min(backoff, 60)
            log_buffer.append(f"[supervisor] FFmpeg exited code={rc}; restarting in {delay}s")
            time.sleep(delay)
            backoff = min(int(backoff * 1.5), 60)

        except Exception as e:
            stream_state.set_running(False)
            stream_state.set_error(str(e))
            if stop_requested.is_set():
                break
            restarts += 1
            stream_state.increment_restarts()
            log_buffer.append(f"[supervisor] Exception: {e}; retry in {backoff}s")
            logger.error(f"[supervisor] Exception: {e}")
            time.sleep(backoff)
            backoff = min(int(backoff * 1.5), 60)

    stream_state.set_running(False)
    logger.info("Supervisor thread exiting")

# --------------------------
# Routes
# --------------------------

@app.route("/", methods=["GET"])
def index():
    try:
        saved_config_json = redis_client.get(STREAM_CONFIG_KEY)
        saved_config = json.loads(saved_config_json) if saved_config_json else {}
        form = {**DEFAULTS, **saved_config}

        cats = list_categories()
        if not form.get("category") or form["category"] not in cats:
            form["category"] = cats[0] if cats else ""
        vids = list_videos_in_category(form["category"]) if form["category"] else []
        if vids and (not form.get("video") or form["video"] not in vids):
            form["video"] = vids[0]

        return render_template("index.html", categories=cats, videos=vids, logs="", form=form)
    except Exception as e:
        logger.error(f"Error loading index page: {e}")
        return f"Error loading page: {str(e)}", 500

@app.route("/status")
def status():
    try:
        s = stream_state.get_status()
        s["video_count"] = total_video_count()
        s["tail"] = list(log_buffer)[-50:]
        return jsonify(s)
    except Exception as e:
        logger.error(f"Error getting status: {e}")
        return jsonify({"error": "Failed to get status"}), 500

@app.route("/logs")
def logs_api():
    try:
        tail = list(log_buffer)[-200:]
        return jsonify({"lines": tail})
    except Exception as e:
        logger.error(f"Error getting logs: {e}")
        return jsonify({"lines": [f"Error getting logs: {e}"]}), 500

@app.route("/logs/clear", methods=["POST"])
def logs_clear():
    try:
        log_buffer.clear()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/videos", methods=["GET"])
def videos_api():
    category = request.args.get("category", "").strip()
    return jsonify({"videos": list_videos_in_category(category)})

@app.route("/categories", methods=["GET"])
def categories_api():
    return jsonify({"categories": list_categories()})

@app.route("/start", methods=["POST"])
def start_stream():
    global ffmpeg_thread
    try:
        if stream_state.get_status()["running"]:
            return jsonify({"running": True, "message": "Already running"}), 200

        form = parse_form_from_request(request.form)

        # Save config
        config_to_save = {
            k: form[k] for k in ["stream_key", "bitrate", "input_type", "video", "srt_url", "encoder", "preset", "category", "shuffle"]
        }
        redis_client.set(STREAM_CONFIG_KEY, json.dumps(config_to_save))

        if not form["stream_key"]:
            return jsonify({"running": False, "error": "Stream key / output URL is required"}), 400
        if not validate_bitrate(form["bitrate"]):
            return jsonify({"running": False, "error": "Invalid bitrate format (e.g., 2500k)"}), 400
        if not validate_encoder(form["encoder"]):
            return jsonify({"running": False, "error": "Invalid encoder selection"}), 400

        input_desc = ""
        loop_forever = False

        # Build input args
        if form["input_type"] == "file":
            category = form.get("category", "").strip()
            cats = list_categories()
            if category not in cats:
                return jsonify({"running": False, "error": "Choose a valid category"}), 400

            shuffle_enabled = str(form.get("shuffle", "off")).lower() in ("on", "true", "1", "yes")
            root = _category_root(category)

            if shuffle_enabled:
                files = list_videos_in_category(category)
                if not files:
                    return jsonify({"running": False, "error": f"No videos in category: {category}"}), 400

                abs_paths = []
                for rel in files:
                    try:
                        full = _safe_resolve_under(root, rel)
                    except ValueError:
                        continue
                    if _is_valid_video_file(full):
                        abs_paths.append(full)

                if not abs_paths:
                    return jsonify({"running": False, "error": f"No valid videos in category: {category}"}), 400

                # Shuffle once, then duplicate the sequence PLAYLIST_REPEAT times
                random.shuffle(abs_paths)
                if PLAYLIST_REPEAT > 1:
                    abs_paths = abs_paths * PLAYLIST_REPEAT

                plist = _write_concat_playlist(abs_paths)  # unique per start

                # IMPORTANT: no -stream_loop with concat; supervisor loops on EOF (far in the future)
                input_args = ["-re", "-f", "concat", "-safe", "0", "-i", plist]
                input_desc = f"[shuffle×{PLAYLIST_REPEAT}] {category}/playlist"
                loop_forever = True
            else:
                rel_video = form.get("video", "").strip()
                vids = list_videos_in_category(category)
                if not rel_video:
                    if not vids:
                        return jsonify({"running": False, "error": f"No videos in category: {category}"}), 400
                    rel_video = vids[0]
                try:
                    input_path = _safe_resolve_under(root, rel_video)
                except ValueError:
                    return jsonify({"running": False, "error": "Invalid video path"}), 400
                if not _is_valid_video_file(input_path):
                    return jsonify({"running": False, "error": "Selected file is not a valid video"}), 400

                # Single file can safely use -stream_loop -1
                input_args = ["-re", "-stream_loop", "-1", "-i", input_path]
                input_desc = f"{category}/{rel_video}"
                loop_forever = True

        elif form["input_type"] == "srt":
            if not form["srt_url"] or not validate_srt_url(form["srt_url"]):
                return jsonify({"running": False, "error": "Invalid or missing SRT / RTMP input URL"}), 400
            input_args = ["-re", "-rw_timeout", "15000000", "-i", form["srt_url"]]
            input_desc = form["srt_url"]
            loop_forever = True
        else:
            return jsonify({"running": False, "error": "Invalid input type"}), 400

        # Build command
        try:
            cmd = build_cmd(
                input_args=input_args,
                encoder=form["encoder"],
                preset=form["preset"],
                bitrate=form["bitrate"],
                out_url=form["stream_key"]
            )
        except Exception as e:
            logger.error(f"Failed to build FFmpeg command: {e}")
            return jsonify({"running": False, "error": f"Failed to build command: {str(e)}"}), 400

        # Start supervised
        stop_requested.clear()
        log_buffer.append(f"[system] Starting: {input_desc} @ {form['bitrate']} ({form['encoder']}, preset {form['preset']})")
        logger.info(f"Starting supervised stream: {input_desc}")
        thread = threading.Thread(target=start_supervised, args=(cmd, loop_forever), daemon=True)
        thread.start()
        globals()["ffmpeg_thread"] = thread

        return jsonify({"running": True, "message": f"Started: {input_desc}"})
    except Exception as e:
        logger.error(f"Error in /start: {e}")
        return jsonify({"running": False, "error": f"Internal error: {str(e)}"}), 500

@app.route("/stop", methods=["POST"])
def stop_stream():
    global ffmpeg_process, ffmpeg_thread
    try:
        stop_requested.set()
        # Kill entire process group
        if ffmpeg_process and ffmpeg_process.poll() is None:
            try:
                os.killpg(ffmpeg_process.pid, signal.SIGTERM)
                try:
                    ffmpeg_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(ffmpeg_process.pid, signal.SIGKILL)
            except Exception as e:
                logger.warning(f"Failed to gracefully stop FFmpeg: {e}")
        if ffmpeg_thread and ffmpeg_thread.is_alive():
            ffmpeg_thread.join(timeout=5)
        stream_state.reset()
        log_buffer.append("[system] Stream stopped")
        return jsonify({"running": False, "message": "Stopped"})
    except Exception as e:
        logger.error(f"Error in /stop: {e}")
        return jsonify({"running": False, "error": f"Internal error: {str(e)}"}), 500

# Compatibility: single toggle endpoint used by many templates
@app.route("/toggle", methods=["POST"])
def toggle():
    try:
        if stream_state.get_status()["running"]:
            return stop_stream()
        else:
            return start_stream()
    except Exception as e:
        logger.error(f"Error in /toggle: {e}")
        return jsonify({"running": False, "error": f"Internal error: {str(e)}"}), 500

@app.route("/rescan", methods=["POST"])
def rescan():
    cats = list_categories()
    result = {c: len(list_videos_in_category(c)) for c in cats}
    return jsonify({"categories": cats, "counts": result})

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {error}")
    return jsonify({"error": "Internal server error"}), 500

if __name__ == "__main__":
    logger.info(f"Starting Flask app in {'DEBUG' if DEBUG_MODE else 'PRODUCTION'} mode")
    logger.info(f"Video folder: {VIDEO_FOLDER}")
    logger.info(f"Max log lines: {MAX_LOG_LINES}  |  Max restarts: {MAX_RESTARTS}  |  Stall timeout: {STALL_TIMEOUT}s  |  Repeat: {PLAYLIST_REPEAT}x")
    app.run(host="0.0.0.0", port=5000, debug=DEBUG_MODE)
