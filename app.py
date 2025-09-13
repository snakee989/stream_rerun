import os
import re
import logging
import subprocess
import threading
import time
from collections import deque
from urllib.parse import urlparse
from flask import Flask, request, render_template, jsonify

# Configuration
DEBUG_MODE = os.getenv('DEBUG', 'False').lower() == 'true'
MAX_LOG_LINES = int(os.getenv('MAX_LOG_LINES', '500'))
VIDEO_FOLDER = os.getenv('VIDEO_FOLDER', '/app/videos')
MAX_RESTARTS = int(os.getenv('MAX_RESTARTS', '10'))

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

# Runtime state and log buffer
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

# Global state
stream_state = StreamState()
ffmpeg_process = None
ffmpeg_thread = None
stop_requested = False
last_cmd = None

# Default settings
DEFAULTS = {
    "stream_key": "",
    "bitrate": "2500k",
    "input_type": "file",
    "video": "",
    "srt_url": "",
    "encoder": "libx264",
    "preset": "medium",
}

# Validation functions
def validate_video_filename(filename):
    """Validate video filename to prevent path traversal attacks"""
    if not filename or '..' in filename or filename.startswith('/') or '\\' in filename:
        return False
    return filename.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm'))

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
def list_videos():
    """List available video files with error handling"""
    try:
        if not os.path.exists(VIDEO_FOLDER):
            logger.warning(f"Video folder does not exist: {VIDEO_FOLDER}")
            return []
        
        files = os.listdir(VIDEO_FOLDER)
        valid_videos = [f for f in files if validate_video_filename(f)]
        
        if not valid_videos:
            logger.info("No valid video files found in video folder")
        
        return sorted(valid_videos)
    except Exception as e:
        logger.error(f"Error listing videos: {e}")
        return []

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
        # You could add NVIDIA GPU detection here
        # For now, we'll assume it's available if selected
        pass
    return True, None

def build_cmd(input_args, encoder, preset, bitrate, out_url):
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
    
    cmd = (["ffmpeg", "-loglevel", "verbose"] + 
           pre + input_args + vcodec + common_rc + vf + audio + common_ts + 
           ["-f", "flv", out_url])
    
    logger.info(f"Built FFmpeg command: {' '.join(cmd[:10])}...")  # Log first 10 elements for security
    return cmd

def start_supervised(cmd):
    """Start FFmpeg with supervision and auto-restart"""
    global ffmpeg_process, stop_requested
    backoff = 2
    restarts = 0
    
    logger.info("Starting supervised FFmpeg process")
    
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
                preexec_fn=os.setsid if os.name != 'nt' else None  # Process group for clean termination
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
    logger.info("Supervisor thread exiting")

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
        
        return render_template("index.html", videos=videos, logs="", form=form)
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
        
        # Comprehensive input validation
        if not form["stream_key"]:
            return jsonify({"running": False, "error": "Stream key / output URL is required"}), 400
        
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
            
        elif form["input_type"] == "srt":
            if not form["srt_url"] or not validate_srt_url(form["srt_url"]):
                return jsonify({"running": False, "error": "Invalid or missing SRT / RTMP input URL"}), 400
            
            input_args = ["-re", "-rw_timeout", "15000000", "-i", form["srt_url"]]
            
        else:
            return jsonify({"running": False, "error": "Invalid input type"}), 400
        
        # Build FFmpeg command
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
        
        # Start the stream
        last_cmd = cmd
        stop_requested = False
        
        logger.info("Starting new stream")
        ffmpeg_thread = threading.Thread(target=start_supervised, args=(cmd,), daemon=True)
        ffmpeg_thread.start()
        
        input_desc = form["video"] if form["input_type"] == "file" else form["srt_url"]
        success_msg = f"Started: {input_desc} @ {form['bitrate']} ({form['encoder']}, preset {form['preset']})"
        
        log_buffer.append(f"[system] {success_msg}")
        logger.info(success_msg)
        
        return jsonify({
            "running": True,
            "message": success_msg
        })
        
    except Exception as e:
        logger.error(f"Error in toggle endpoint: {e}")
        return jsonify({"running": False, "error": f"Internal error: {str(e)}"}), 500

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

@app.route("/health")
def health():
    """Health check endpoint with encoder availability"""
    try:
        encoders = {
            "cpu": {"name": "libx264", "available": True},
            "nvidia": {"name": "h264_nvenc", "available": True},  # Could add GPU detection
            "intel": {"name": "h264_vaapi", "available": os.path.exists("/dev/dri/renderD128")}
        }
        
        return jsonify({
            "status": "healthy",
            "encoders": encoders,
            "video_folder": VIDEO_FOLDER,
            "video_count": len(list_videos()),
            "max_restarts": MAX_RESTARTS
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

if __name__ == "__main__":
    logger.info(f"Starting Flask app in {'DEBUG' if DEBUG_MODE else 'PRODUCTION'} mode")
    logger.info(f"Video folder: {VIDEO_FOLDER}")
    logger.info(f"Max log lines: {MAX_LOG_LINES}")
    logger.info(f"Max restarts: {MAX_RESTARTS}")
    
    app.run(host="0.0.0.0", port=5000, debug=DEBUG_MODE)
