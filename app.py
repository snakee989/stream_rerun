import os
import re
import json
import logging
import subprocess
import threading
import time
import signal
import copy
from collections import deque
from urllib.parse import urlparse
from datetime import datetime
from flask import Flask, request, render_template, jsonify
import redis
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Configuration
DEBUG_MODE = os.getenv('DEBUG', 'False').lower() == 'true'
MAX_LOG_LINES = int(os.getenv('MAX_LOG_LINES', '500'))
VIDEO_FOLDER = os.getenv('VIDEO_FOLDER', '/app/videos')
MAX_RESTARTS = int(os.getenv('MAX_RESTARTS', '10'))
REDIS_URL = os.getenv('REDIS_URL', 'redis://Redis:6379')

# Create directories if they don't exist
os.makedirs(VIDEO_FOLDER, exist_ok=True)
os.makedirs('/app/logs', exist_ok=True)

# Setup logging
logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/app/logs/app.log')
    ]
)
logger = logging.getLogger('streaming-server')

# Redis client
try:
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
    logger.info("Connected to Redis successfully")
except redis.ConnectionError:
    logger.warning("Could not connect to Redis, using in-memory storage")
    redis_client = None

# Flask app
app = Flask(__name__)

# Runtime state and log buffer
log_buffer = deque(maxlen=MAX_LOG_LINES)

# Default destinations
DEFAULT_DESTINATIONS = [
    {
        "id": "1",
        "name": "Twitch",
        "url": "rtmp://live.twitch.tv/app/",
        "key": "",
        "enabled": False,
        "type": "rtmp",
        "status": "inactive"
    },
    {
        "id": "2", 
        "name": "YouTube",
        "url": "rtmp://a.rtmp.youtube.com/live2/",
        "key": "",
        "enabled": False,
        "type": "rtmp",
        "status": "inactive"
    },
    {
        "id": "3",
        "name": "Facebook",
        "url": "rtmps://live-api-s.facebook.com:443/rtmp/",
        "key": "",
        "enabled": False,
        "type": "rtmps",
        "status": "inactive"
    },
    {
        "id": "4",
        "name": "Kick",
        "url": "rtmp://rtmp.kick.com:1935/live/",
        "key": "",
        "enabled": False,
        "type": "rtmp",
        "status": "inactive"
    }
]

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
        self.current_video = None
        self.destinations = copy.deepcopy(DEFAULT_DESTINATIONS)
        self.playlist = []
        self.current_index = 0
        self.shuffle_mode = True
    
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
    
    def set_current_video(self, video):
        with self._lock:
            self.current_video = video
    
    def set_destinations(self, destinations):
        with self._lock:
            self.destinations = destinations
    
    def set_playlist(self, playlist):
        with self._lock:
            self.playlist = playlist
    
    def set_shuffle_mode(self, shuffle):
        with self._lock:
            self.shuffle_mode = shuffle
    
    def reset(self):
        with self._lock:
            self.running = False
            self.start_time = None
            self.last_error = None
            self.process_id = None
            self.current_video = None
    
    def get_status(self):
        with self._lock:
            current_uptime = self.uptime
            if self.running and self.start_time:
                current_uptime += time.time() - self.start_time
            
            # Count enabled destinations
            enabled_destinations = sum(1 for d in self.destinations if d.get('enabled', False) and d.get('key'))
            
            return {
                'running': self.running,
                'restarts': self.restarts,
                'uptime': current_uptime,
                'last_error': self.last_error,
                'process_id': self.process_id,
                'current_video': self.current_video,
                'destinations_count': len(self.destinations),
                'enabled_destinations': enabled_destinations,
                'playlist_length': len(self.playlist),
                'shuffle_mode': self.shuffle_mode
            }

# Global state
stream_state = StreamState()
ffmpeg_process = None
ffmpeg_thread = None
stop_requested = False
last_cmd = None
file_observer = None

# Default settings
DEFAULTS = {
    "bitrate": "2500k",
    "input_type": "file",
    "video": "",
    "srt_url": "",
    "encoder": "libx264",
    "preset": "medium",
}

# Initialize state from Redis if available
def init_state():
    try:
        if redis_client:
            # Try to load destinations from Redis
            destinations_json = redis_client.get('stream:destinations')
            if destinations_json:
                stream_state.set_destinations(json.loads(destinations_json))
            
            # Try to load playlist from Redis
            playlist_json = redis_client.get('stream:playlist')
            if playlist_json:
                stream_state.set_playlist(json.loads(playlist_json))
            
            # Try to load shuffle mode from Redis
            shuffle_mode = redis_client.get('stream:shuffle_mode')
            if shuffle_mode:
                stream_state.set_shuffle_mode(shuffle_mode.lower() == 'true')
                
            logger.info("State initialized from Redis")
    except Exception as e:
        logger.error(f"Error initializing state from Redis: {e}")

# Save state to Redis
def save_state():
    try:
        if redis_client:
            redis_client.set('stream:destinations', json.dumps(stream_state.destinations))
            redis_client.set('stream:playlist', json.dumps(stream_state.playlist))
            redis_client.set('stream:shuffle_mode', str(stream_state.shuffle_mode))
    except Exception as e:
        logger.error(f"Error saving state to Redis: {e}")

# File system event handler
class VideoFileHandler(FileSystemEventHandler):
    def __init__(self):
        super().__init__()
    
    def on_created(self, event):
        if not event.is_directory and is_video_file(event.src_path):
            logger.info(f"New video file detected: {event.src_path}")
            relative_path = os.path.relpath(event.src_path, VIDEO_FOLDER)
            log_buffer.append(f"[filewatch] New video added: {relative_path}")
            # Rescan videos
            threading.Thread(target=scan_videos, daemon=True).start()
    
    def on_deleted(self, event):
        if not event.is_directory and is_video_file(event.src_path):
            logger.info(f"Video file deleted: {event.src_path}")
            relative_path = os.path.relpath(event.src_path, VIDEO_FOLDER)
            log_buffer.append(f"[filewatch] Video deleted: {relative_path}")
            # Rescan videos
            threading.Thread(target=scan_videos, daemon=True).start()
    
    def on_moved(self, event):
        if not event.is_directory and is_video_file(event.src_path):
            logger.info(f"Video file moved: {event.src_path} -> {event.dest_path}")
            src_relative = os.path.relpath(event.src_path, VIDEO_FOLDER)
            dest_relative = os.path.relpath(event.dest_path, VIDEO_FOLDER)
            log_buffer.append(f"[filewatch] Video moved: {src_relative} -> {dest_relative}")
            # Rescan videos
            threading.Thread(target=scan_videos, daemon=True).start()

def is_video_file(file_path):
    """Check if a file is a video file"""
    video_extensions = ['.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm', '.m4v', '.wmv']
    filename = file_path.lower()
    return any(filename.endswith(ext) for ext in video_extensions)

def start_file_watcher():
    """Start watching for file changes"""
    global file_observer
    try:
        file_observer = Observer()
        event_handler = VideoFileHandler()
        file_observer.schedule(event_handler, VIDEO_FOLDER, recursive=True)
        file_observer.start()
        logger.info(f"Started file watcher for: {VIDEO_FOLDER}")
    except Exception as e:
        logger.error(f"Error starting file watcher: {e}")

def stop_file_watcher():
    """Stop watching for file changes"""
    global file_observer
    if file_observer:
        file_observer.stop()
        file_observer.join()
        logger.info("Stopped file watcher")

# Validation functions
def validate_video_filename(filename):
    """Validate video filename to prevent path traversal attacks"""
    if not filename or '..' in filename or filename.startswith('/') or '\\' in filename:
        return False
    return is_video_file(filename)

def validate_bitrate(bitrate):
    """Validate bitrate format (e.g., '2500k' or '2500')"""
    if not bitrate:
        return False
    return bool(re.match(r'^\d+k?$', bitrate.strip()))

def validate_srt_url(url):
    """Basic URL validation for SRT/RTMP inputs"""
    if not url:
        return False
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme.lower() in ['srt', 'rtmp', 'rtmps', 'udp']
    except Exception as e:
        logger.debug(f"URL validation failed for {url}: {e}")
        return False

def validate_encoder(encoder):
    """Validate encoder selection"""
    valid_encoders = ['libx264', 'h264_nvenc', 'h264_vaapi']
    return encoder in valid_encoders

# Helper functions
def scan_videos():
    """Scan for video files and update playlist"""
    try:
        videos = []
        for root, dirs, files in os.walk(VIDEO_FOLDER):
            for file in files:
                if is_video_file(file):
                    full_path = os.path.join(root, file)
                    relative_path = os.path.relpath(full_path, VIDEO_FOLDER)
                    videos.append(relative_path)
        
        videos.sort()
        stream_state.set_playlist(videos)
        
        if redis_client:
            redis_client.set('stream:playlist', json.dumps(videos))
        
        logger.info(f"Found {len(videos)} video files")
        log_buffer.append(f"[filewatch] Found {len(videos)} videos")
        
        return videos
    except Exception as e:
        logger.error(f"Error scanning videos: {e}")
        return []

def list_videos():
    """List available video files with error handling"""
    try:
        if not stream_state.playlist:
            return scan_videos()
        return stream_state.playlist
    except Exception as e:
        logger.error(f"Error listing videos: {e}")
        return []

def get_next_video():
    """Get the next video from the playlist"""
    if not stream_state.playlist:
        scan_videos()
    
    if not stream_state.playlist:
        return None
    
    if stream_state.shuffle_mode:
        import random
        return random.choice(stream_state.playlist)
    else:
        next_index = (stream_state.current_index + 1) % len(stream_state.playlist)
        stream_state.current_index = next_index
        return stream_state.playlist[next_index]

def parse_bitrate_k(bitrate):
    """Parse bitrate string like '2500k' to integer"""
    bitrate = bitrate.strip()
    if bitrate.endswith('k') and bitrate[:-1].isdigit():
        return int(bitrate[:-1])
    elif bitrate.isdigit():
        return int(bitrate)
    return None

def check_hardware_encoder_availability(encoder):
    """Check if hardware encoder is available"""
    if encoder == "h264_vaapi":
        if not os.path.exists("/dev/dri/renderD128"):
            return False, "Intel VAAPI device (/dev/dri/renderD128) not found"
    elif encoder == "h264_nvenc":
        # Check if NVIDIA GPU is available
        try:
            result = subprocess.run(['nvidia-smi'], capture_output=True, text=True)
            if result.returncode != 0:
                return False, "NVIDIA GPU not available"
        except:
            return False, "NVIDIA GPU not available"
    return True, None

def build_cmd(input_args, encoder, preset, bitrate, destinations):
    """Build FFmpeg command with comprehensive validation"""
    if not validate_bitrate(bitrate):
        raise ValueError(f"Invalid bitrate format: {bitrate}")
    
    if not validate_encoder(encoder):
        raise ValueError(f"Invalid encoder: {encoder}")
    
    # Check hardware encoder availability
    available, error_msg = check_hardware_encoder_availability(encoder)
    if not available:
        raise ValueError(error_msg)
    
    bk = parse_bitrate_k(bitrate)
    if bk and bk < 100:
        log_buffer.append('[warning] Very low bitrate detected (< 100k)')
    elif bk and bk > 50000:
        log_buffer.append('[warning] Very high bitrate detected (> 50M)')
    
    # Build command components
    buf = f"{bk*2}k" if bk else f"{int(bitrate)*2}" if bitrate.isdigit() else bitrate
    
    common_rc = [
        "-b:v", bitrate,
        "-maxrate", bitrate,
        "-bufsize", buf,
        "-g", "120",
        "-force_key_frames", "expr:gte(t,n_forced*2)"
    ]
    
    common_ts = ["-fflags", "+genpts"]
    audio = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
    
    pre = []
    vf = []
    vcodec = []
    
    if encoder == "h264_nvenc":
        # NVIDIA NVENC encoder
        vcodec = ["-c:v", "h264_nvenc", "-preset", preset, "-rc", "cbr", "-pix_fmt", "yuv420p"]
    elif encoder == "h264_vaapi":
        # Intel VAAPI encoder
        pre = ["-vaapi_device", "/dev/dri/renderD128"]
        vf = ["-vf", "format=nv12,hwupload"]
        vcodec = ["-c:v", "h264_vaapi", "-bf", "2"]
    else:
        # CPU x264 encoder (default)
        vcodec = ["-c:v", "libx264", "-preset", preset, "-pix_fmt", "yuv420p"]
    
    # Build base command
    cmd = (["ffmpeg", "-loglevel", "verbose"] + 
           pre + input_args + vcodec + common_rc + vf + audio + common_ts)
    
    # Add multiple outputs for each destination
    for dest in destinations:
        if dest.get('enabled', False) and dest.get('key'):
            full_url = f"{dest['url']}{dest['key']}"
            cmd.extend(["-f", "flv", full_url])
    
    logger.info(f"Built FFmpeg command with {len([d for d in destinations if d.get('enabled', False) and d.get('key')])} destinations")
    return cmd

def start_supervised(cmd, video_name):
    """Start FFmpeg with supervision and auto-restart"""
    global ffmpeg_process, stop_requested
    backoff = 2
    restarts = 0
    
    logger.info("Starting supervised FFmpeg process")
    stream_state.set_current_video(video_name)
    
    while restarts < MAX_RESTARTS and not stop_requested:
        stream_state.set_running(True)
        
        try:
            logger.info(f"Starting FFmpeg process (attempt {restarts + 1})")
            ffmpeg_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
                preexec_fn=os.setsid if os.name != 'nt' else None
            )
            
            stream_state.process_id = ffmpeg_process.pid
            log_buffer.append(f"[supervisor] Started FFmpeg process (PID: {ffmpeg_process.pid})")
            
            # Read output line by line
            for line in ffmpeg_process.stdout:
                if stop_requested:
                    break
                log_buffer.append(line.rstrip("\n"))
            
            rc = ffmpeg_process.wait()
            stream_state.set_running(False)
            
            if stop_requested:
                log_buffer.append("[supervisor] Stop requested, exiting supervisor")
                break
            
            restarts += 1
            stream_state.increment_restarts()
            
            if restarts >= MAX_RESTARTS:
                log_buffer.append(f"[supervisor] Max restarts ({MAX_RESTARTS}) reached, giving up")
                stream_state.set_error(f"Max restarts ({MAX_RESTARTS}) reached")
                break
            
            log_buffer.append(f"[supervisor] FFmpeg exited (code={rc}), restarting in {backoff}s (attempt {restarts + 1}/{MAX_RESTARTS})")
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 60)  # Exponential backoff with cap
            
        except Exception as e:
            stream_state.set_running(False)
            stream_state.set_error(str(e))
            
            if stop_requested:
                break
                
            restarts += 1
            error_msg = f"[supervisor] Exception: {e}, retry in {backoff}s"
            log_buffer.append(error_msg)
            logger.error(error_msg)
            
            if restarts < MAX_RESTARTS:
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 60)
    
    stream_state.set_running(False)
    stream_state.set_current_video(None)
    logger.info("Supervisor thread exiting")

# Initialize state
init_state()
scan_videos()
start_file_watcher()

# Flask routes
@app.route("/", methods=["GET"])
def index():
    """Main page with streaming controls"""
    try:
        form = DEFAULTS.copy()
        videos = list_videos()
        
        # Pre-select first video if available
        if videos and not form["video"]:
            form["video"] = videos[0]
        
        # Get current destinations
        destinations = stream_state.destinations
        
        return render_template("index.html", 
                             videos=videos, 
                             logs="", 
                             form=form, 
                             destinations=destinations,
                             shuffle_mode=stream_state.shuffle_mode)
    except Exception as e:
        logger.error(f"Error loading index page: {e}")
        return f"Error loading page: {str(e)}", 500

@app.route("/toggle", methods=["POST"])
def toggle():
    """Start/stop streaming with comprehensive validation"""
    global ffmpeg_process, ffmpeg_thread, last_cmd, stop_requested
    
    try:
        if stream_state.running:
            logger.info("Stopping stream")
            stop_requested = True
            
            # Terminate FFmpeg process
            if ffmpeg_process and ffmpeg_process.poll() is None:
                try:
                    logger.info(f"Terminating FFmpeg process (PID: {ffmpeg_process.pid})")
                    ffmpeg_process.terminate()
                    ffmpeg_process.wait(timeout=3)
                    logger.info("FFmpeg process terminated successfully")
                except subprocess.TimeoutExpired:
                    logger.warning("FFmpeg process didn't terminate gracefully, killing it")
                    try:
                        ffmpeg_process.kill()
                        ffmpeg_process.wait(timeout=1)
                        logger.info("FFmpeg process killed")
                    except Exception as e:
                        logger.error(f"Failed to kill FFmpeg process: {e}")
                        log_buffer.append(f"[error] Failed to kill process: {e}")
                except Exception as e:
                    logger.error(f"Failed to terminate FFmpeg process: {e}")
                    log_buffer.append(f"[error] Failed to stop process: {e}")
            
            # Wait for supervisor thread to finish
            if ffmpeg_thread and ffmpeg_thread.is_alive():
                logger.info("Waiting for supervisor thread to finish")
                ffmpeg_thread.join(timeout=5)
                if ffmpeg_thread.is_alive():
                    logger.warning("Supervisor thread didn't finish in time")
            
            stream_state.reset()
            log_buffer.append("[system] Stream stopped by user")
            return jsonify({"running": False, "message": "Stream stopped successfully"})
        
        # Starting stream - validate form data
        form = {}
        for key in DEFAULTS.keys():
            value = request.form.get(key, DEFAULTS[key])
            form[key] = value.strip() if isinstance(value, str) else value
        
        # Get enabled destinations
        destinations = []
        for dest in stream_state.destinations:
            enabled = request.form.get(f"dest_{dest['id']}_enabled") == "on"
            key = request.form.get(f"dest_{dest['id']}_key", "").strip()
            if enabled and key:
                dest_copy = dest.copy()
                dest_copy['enabled'] = True
                dest_copy['key'] = key
                dest_copy['status'] = 'active'
                destinations.append(dest_copy)
        
        # Validate at least one destination
        if not destinations:
            return jsonify({"running": False, "error": "At least one destination with stream key is required"}), 400
        
        # Update destinations state
        for dest in stream_state.destinations:
            for active_dest in destinations:
                if dest['id'] == active_dest['id']:
                    dest['enabled'] = True
                    dest['key'] = active_dest['key']
                    dest['status'] = 'active'
                else:
                    dest['enabled'] = False
                    dest['status'] = 'inactive'
        
        save_state()
        
        # Comprehensive input validation
        if not validate_bitrate(form["bitrate"]):
            return jsonify({"running": False, "error": "Invalid bitrate format (use format like '2500k' or '2500')"}), 400
        
        if not validate_encoder(form["encoder"]):
            return jsonify({"running": False, "error": "Invalid encoder selection"}), 400
        
        # Input-specific validation
        if form["input_type"] == "file":
            if form["video"] and not validate_video_filename(form["video"]):
                return jsonify({"running": False, "error": "Invalid video filename"}), 400
            
            if not form["video"]:
                videos = list_videos()
                if not videos:
                    return jsonify({"running": False, "error": "No video files found. Upload videos to the video folder"}), 400
                form["video"] = videos[0]
            
            input_path = os.path.join(VIDEO_FOLDER, form["video"])
            if not os.path.exists(input_path):
                return jsonify({"running": False, "error": f"Video file not found: {form['video']}"}), 400
            
            input_args = ["-re", "-stream_loop", "-1", "-i", input_path]
            video_name = form["video"]
            
        elif form["input_type"] == "srt":
            if not form["srt_url"] or not validate_srt_url(form["srt_url"]):
                return jsonify({"running": False, "error": "Invalid or missing SRT / RTMP input URL"}), 400
            
            input_args = ["-re", "-rw_timeout", "15000000", "-i", form["srt_url"]]
            video_name = form["srt_url"]
            
        else:
            return jsonify({"running": False, "error": "Invalid input type"}), 400
        
        # Build FFmpeg command
        try:
            cmd = build_cmd(
                input_args=input_args,
                encoder=form["encoder"],
                preset=form["preset"],
                bitrate=form["bitrate"],
                destinations=destinations
            )
        except Exception as e:
            logger.error(f"Failed to build FFmpeg command: {e}")
            return jsonify({"running": False, "error": f"Failed to build command: {str(e)}"}), 400
        
        # Start the stream
        last_cmd = cmd
        stop_requested = False
        
        logger.info("Starting new stream")
        ffmpeg_thread = threading.Thread(
            target=start_supervised, 
            args=(cmd, video_name), 
            daemon=True
        )
        ffmpeg_thread.start()
        
        dest_count = len(destinations)
        success_msg = f"Started: {video_name} → {dest_count} destinations @ {form['bitrate']} ({form['encoder']})"
        
        log_buffer.append(f"[system] {success_msg}")
        logger.info(success_msg)
        
        return jsonify({
            "running": True,
            "message": success_msg
        })
        
    except Exception as e:
        logger.error(f"Error in toggle endpoint: {e}")
        return jsonify({"running": False, "error": f"Internal error: {str(e)}"}), 500

@app.route("/skip", methods=["POST"])
def skip_video():
    """Skip to the next video in the playlist"""
    global stop_requested
    
    if not stream_state.running:
        return jsonify({"error": "Stream is not running"}), 400
    
    logger.info("Skipping current video")
    stop_requested = True
    
    # Terminate FFmpeg process
    if ffmpeg_process and ffmpeg_process.poll() is None:
        try:
            ffmpeg_process.terminate()
            ffmpeg_process.wait(timeout=2)
        except:
            try:
                ffmpeg_process.kill()
                ffmpeg_process.wait(timeout=1)
            except:
                pass
    
    stop_requested = False
    log_buffer.append("[system] Skipped to next video")
    
    return jsonify({"message": "Skipping to next video"})

@app.route("/shuffle", methods=["POST"])
def toggle_shuffle():
    """Toggle shuffle mode"""
    data = request.get_json()
    if not data or 'shuffle' not in data:
        return jsonify({"error": "Missing shuffle parameter"}), 400
    
    shuffle = data['shuffle']
    stream_state.set_shuffle_mode(shuffle)
    save_state()
    
    mode = "enabled" if shuffle else "disabled"
    log_buffer.append(f"[system] Shuffle mode {mode}")
    
    return jsonify({"message": f"Shuffle mode {mode}", "shuffle": shuffle})

@app.route("/destinations", methods=["GET", "POST"])
def manage_destinations():
    """Get or update streaming destinations"""
    try:
        if request.method == "POST":
            data = request.get_json()
            if not data or not isinstance(data, list):
                return jsonify({"error": "Invalid destinations data"}), 400
            
            # Create a complete destinations list with all properties
            complete_destinations = []
            for new_dest in data:
                # Find the original destination to preserve all properties
                original_dest = next((d for d in DEFAULT_DESTINATIONS if d["id"] == new_dest["id"]), None)
                if original_dest:
                    # Merge the original properties with the new ones
                    merged_dest = copy.deepcopy(original_dest)
                    merged_dest.update(new_dest)
                    complete_destinations.append(merged_dest)
                else:
                    # If it's a custom destination, use the provided data
                    complete_destinations.append(new_dest)
            
            # Save to state and Redis
            stream_state.set_destinations(complete_destinations)
            save_state()
            
            logger.info(f"Updated destinations: {[d['name'] for d in complete_destinations]}")
            
            return jsonify({
                "message": "Destinations updated successfully", 
                "destinations": complete_destinations
            })
        
        else:
            # GET request - return current destinations
            return jsonify({"destinations": stream_state.destinations})
            
    except Exception as e:
        logger.error(f"Error managing destinations: {e}")
        return jsonify({"error": f"Internal error: {str(e)}"}), 500

@app.route("/status")
def status():
    """Get current streaming status"""
    try:
        status_data = stream_state.get_status()
        status_data["video_count"] = len(list_videos())
        return jsonify(status_data)
    except Exception as e:
        logger.error(f"Error getting status: {e}")
        return jsonify({"error": "Failed to get status"}), 500

@app.route("/logs")
def logs():
    """Get recent log entries"""
    try:
        tail = list(log_buffer)[-200:]
        return jsonify({"lines": tail})
    except Exception as e:
        logger.error(f"Error getting logs: {e}")
        return jsonify({"lines": [f"Error getting logs: {e}"]}), 500

@app.route("/videos")
def get_videos():
    """Get list of available videos"""
    try:
        videos = list_videos()
        return jsonify({"videos": videos, "count": len(videos)})
    except Exception as e:
        logger.error(f"Error getting videos: {e}")
        return jsonify({"error": "Failed to get videos"}), 500

@app.route("/health")
def health():
    """Health check endpoint with encoder availability"""
    try:
        encoders = {
            "cpu": {"name": "libx264", "available": True},
            "nvidia": {"name": "h264_nvenc", "available": check_hardware_encoder_availability("h264_nvenc")[0]},
            "intel": {"name": "h264_vaapi", "available": check_hardware_encoder_availability("h264_vaapi")[0]}
        }
        
        return jsonify({
            "status": "healthy",
            "encoders": encoders,
            "video_folder": VIDEO_FOLDER,
            "video_count": len(list_videos()),
            "max_restarts": MAX_RESTARTS,
            "destinations_count": len(stream_state.destinations),
            "redis_connected": redis_client is not None and redis_client.ping()
        })
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {error}")
    return jsonify({"error": "Internal server error"}), 500

# Signal handlers for graceful shutdown
def signal_handler(signum, frame):
    logger.info(f"Received signal {signum}, shutting down gracefully")
    stop_file_watcher()
    
    global stop_requested
    stop_requested = True
    
    if ffmpeg_process and ffmpeg_process.poll() is None:
        try:
            ffmpeg_process.terminate()
            ffmpeg_process.wait(timeout=5)
        except:
            try:
                ffmpeg_process.kill()
            except:
                pass
    
    save_state()
    exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == "__main__":
    logger.info(f"Starting streaming server in {'DEBUG' if DEBUG_MODE else 'PRODUCTION'} mode")
    logger.info(f"Video folder: {VIDEO_FOLDER}")
    logger.info(f"Max log lines: {MAX_LOG_LINES}")
    logger.info(f"Max restarts: {MAX_RESTARTS}")
    logger.info(f"Loaded {len(stream_state.destinations)} destinations")
    logger.info(f"Found {len(stream_state.playlist)} videos")
    
    app.run(host="0.0.0.0", port=5000, debug=DEBUG_MODE)
