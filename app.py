import os
import re
import logging
import subprocess
import threading
import time
import glob
import json
import redis
import random
import tempfile
import atexit
import psutil
from collections import deque
from urllib.parse import urlparse, quote
from flask import Flask, request, render_template, jsonify

# Configuration
DEBUG_MODE = os.getenv('DEBUG', 'False').lower() == 'true'
MAX_LOG_LINES = int(os.getenv('MAX_LOG_LINES', '500'))
VIDEO_FOLDER = os.getenv('VIDEO_FOLDER', '/app/videos')
MAX_RESTARTS = int(os.getenv('MAX_RESTARTS', '10'))
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', '6379'))
REDIS_DB = int(os.getenv('REDIS_DB', '0'))
RESOURCE_MONITOR_INTERVAL = int(os.getenv('RESOURCE_MONITOR_INTERVAL', '60'))
HEALTH_CHECK_INTERVAL = int(os.getenv('HEALTH_CHECK_INTERVAL', '30'))
STATUS_LOG_INTERVAL = int(os.getenv('STATUS_LOG_INTERVAL', '300'))

# Create video folder if it doesn't exist
os.makedirs(VIDEO_FOLDER, exist_ok=True)

# Setup logging with debug level support
log_level = logging.DEBUG if DEBUG_MODE else logging.INFO
logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Log debug status immediately
logger.debug(f"DEBUG environment variable: {os.getenv('DEBUG', 'not set')}")
logger.debug(f"DEBUG_MODE value: {DEBUG_MODE}")
logger.debug(f"Log level set to: {logging.getLevelName(logger.getEffectiveLevel())}")

# Flask app
app = Flask(__name__)
app.debug = DEBUG_MODE  # Set Flask debug mode

# Redis client
redis_client = None
try:
    redis_client = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
    redis_client.ping()
    logger.info("Successfully connected to Redis.")
except redis.exceptions.ConnectionError as e:
    logger.error(f"Could not connect to Redis: {e}")
    redis_client = None

# Runtime state and log buffer
log_buffer = deque(maxlen=MAX_LOG_LINES)
temp_files = []  # Track temporary files for cleanup

# Global variables for monitoring threads
stop_requested = False
ffmpeg_process = None
ffmpeg_thread = None
resource_monitor_thread = None
health_check_thread = None
status_logger_thread = None
playlist_cleanup_thread = None

@atexit.register
def cleanup_temp_files():
    """Clean up temporary files on exit"""
    # Clean up all temporary files
    for file_path in temp_files:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.debug(f"Cleaned up temporary file: {file_path}")
        except Exception as e:
            logger.warning(f"Failed to clean up temp file {file_path}: {e}")
    
    # Clean up all playlist files from stream state
    try:
        stream_state.reset()  # This will clean up all playlist files
    except:
        pass

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
        self.playlist = []
        self.destinations = []
        self.current_category = "all"
        self.category_stats = {"all": 0, "justchatting": 0, "pool": 0}
        self.shuffle_mode = True  # Default to True
        self.current_playlist_file = None
        self.last_restart_time = time.time()
        # NEW: Track old playlist files for deferred cleanup
        self.old_playlist_files = []

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
            self.last_restart_time = time.time()

    def set_error(self, error):
        with self._lock:
            self.last_error = error
    
    def reset(self):
        with self._lock:
            self.running = False
            self.start_time = None
            self.last_error = None
            self.process_id = None
            # Clean up ALL playlist files on reset
            files_to_cleanup = []
            if self.current_playlist_file:
                files_to_cleanup.append(self.current_playlist_file)
                self.current_playlist_file = None
            files_to_cleanup.extend(self.old_playlist_files)
            self.old_playlist_files.clear()
        
        # Clean up files outside the lock
        for file_path in files_to_cleanup:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.debug(f"Cleaned up playlist file on reset: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to clean up playlist file {file_path} on reset: {e}")

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
                'process_id': self.process_id,
                'shuffle_mode': self.shuffle_mode,
                'last_restart_time': self.last_restart_time
            }
            
    def set_playlist(self, playlist):
        with self._lock:
            self.playlist = playlist
            
    def set_destinations(self, destinations):
        with self._lock:
            self.destinations = destinations

    def set_category(self, category):
        with self._lock:
            self.current_category = category
            
    def set_playlist_file(self, playlist_file):
        with self._lock:
            # Defer cleanup of previous playlist file
            if self.current_playlist_file and os.path.exists(self.current_playlist_file):
                self.old_playlist_files.append(self.current_playlist_file)
                logger.debug(f"Deferred cleanup of playlist file: {self.current_playlist_file}")
            self.current_playlist_file = playlist_file

    def cleanup_old_playlist_files(self):
        """Clean up old playlist files that are no longer needed"""
        with self._lock:
            files_to_remove = self.old_playlist_files.copy()
            self.old_playlist_files.clear()
        
        # Remove files outside the lock to avoid blocking
        removed_count = 0
        for file_path in files_to_remove:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.debug(f"Cleaned up old playlist file: {file_path}")
                    removed_count += 1
            except Exception as e:
                logger.warning(f"Failed to clean up old playlist file {file_path}: {e}")
                # Put it back in the list to try again later
                with self._lock:
                    self.old_playlist_files.append(file_path)
        
        if removed_count > 0:
            logger.debug(f"Cleaned up {removed_count} old playlist files")

# Global state
stream_state = StreamState()
last_cmd = None

# Default settings - will be updated from Redis if available
DEFAULTS = {
    "stream_key": "",
    "bitrate": "2500k",
    "input_type": "file",
    "video": "",
    "srt_url": "",
    "encoder": "libx264",
    "preset": "medium",
    "shuffle_mode": "true"
}

# Monitoring functions
def monitor_resources():
    """Monitor system resources and log usage"""
    logger.info("Starting resource monitoring thread")
    while not stop_requested:
        try:
            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024
            cpu_percent = process.cpu_percent(interval=1)
            
            logger.debug(f"Resource usage - Memory: {memory_mb:.2f} MB, CPU: {cpu_percent:.1f}%")
            
            # Log warning if memory usage is high
            if memory_mb > 500:  # Adjust threshold as needed
                logger.warning(f"High memory usage: {memory_mb:.2f} MB")
                
            time.sleep(RESOURCE_MONITOR_INTERVAL)
        except Exception as e:
            logger.error(f"Resource monitoring error: {e}")
            time.sleep(300)  # Wait 5 minutes on error
    logger.info("Resource monitoring thread stopped")

def check_ffmpeg_health():
    """Periodically check if FFmpeg is still running properly"""
    logger.info("Starting FFmpeg health check thread")
    while not stop_requested and stream_state.running:
        try:
            if ffmpeg_process and ffmpeg_process.poll() is None:
                # Process is still running
                logger.debug("FFmpeg process health check: OK")
            else:
                logger.warning("FFmpeg process health check: NOT RUNNING")
                
            time.sleep(HEALTH_CHECK_INTERVAL)
        except Exception as e:
            logger.error(f"Health check error: {e}")
            time.sleep(60)
    logger.info("FFmpeg health check thread stopped")

def log_periodic_status():
    """Log periodic status information"""
    logger.info("Starting periodic status logging thread")
    while not stop_requested:
        try:
            status = stream_state.get_status()
            logger.debug(f"Stream status: running={status['running']}, restarts={status['restarts']}, uptime={status['uptime']:.1f}s")
            
            if status['running'] and ffmpeg_process and ffmpeg_process.poll() is None:
                logger.debug(f"FFmpeg process {ffmpeg_process.pid} is still running")
            
            time.sleep(STATUS_LOG_INTERVAL)
        except Exception as e:
            logger.error(f"Status logging error: {e}")
            time.sleep(600)  # Wait 10 minutes on error
    logger.info("Periodic status logging thread stopped")

def cleanup_playlist_files():
    """Periodically clean up old playlist files"""
    logger.info("Starting playlist file cleanup thread")
    while not stop_requested:
        try:
            if stream_state.running:
                # Only clean up when stream is running (FFmpeg is using current file)
                stream_state.cleanup_old_playlist_files()
            time.sleep(300)  # Check every 5 minutes
        except Exception as e:
            logger.error(f"Playlist cleanup error: {e}")
            time.sleep(600)
    logger.info("Playlist file cleanup thread stopped")

# Start monitoring threads when module loads
resource_monitor_thread = threading.Thread(target=monitor_resources, daemon=True)
resource_monitor_thread.start()

status_logger_thread = threading.Thread(target=log_periodic_status, daemon=True)
status_logger_thread.start()

playlist_cleanup_thread = threading.Thread(target=cleanup_playlist_files, daemon=True)
playlist_cleanup_thread.start()

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
def is_video_file(filename):
    return filename.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.flv', '.webm'))

def scan_videos():
    """Scan for video files and update playlist based on current category"""
    try:
        videos = []
        category_counts = {"all": 0, "justchatting": 0, "pool": 0}

        # Use a set to track unique videos
        all_videos_set = set()
        
        # Scan the entire video folder once
        for root, dirs, files in os.walk(VIDEO_FOLDER):
            for file in files:
                if is_video_file(file):
                    full_path = os.path.join(root, file)
                    relative_path = os.path.relpath(full_path, VIDEO_FOLDER)
                    
                    # Add to all videos set (prevents duplicates)
                    if relative_path not in all_videos_set:
                        all_videos_set.add(relative_path)
                        videos.append(relative_path)
                        
                        # Count for statistics
                        category_counts["all"] += 1
                        
                        # Count for specific categories
                        if relative_path.startswith("justchatting" + os.sep):
                            category_counts["justchatting"] += 1
                        elif relative_path.startswith("pool" + os.sep):
                            category_counts["pool"] += 1

        # Filter videos based on current category
        current_videos = []
        if stream_state.current_category != "all":
            for video in videos:
                if video.startswith(stream_state.current_category + os.sep):
                    current_videos.append(video)
        else:
            current_videos = videos

        current_videos.sort()
        stream_state.set_playlist(current_videos)
        stream_state.category_stats = category_counts
        
        if redis_client:
            redis_client.set('stream:playlist', json.dumps(current_videos))
            redis_client.set('stream:category_stats', json.dumps(category_counts))
        
        logger.info(f"Found {len(current_videos)} videos in {stream_state.current_category} category")
        log_buffer.append(f"[filewatch] Found {len(current_videos)} videos in {stream_state.current_category} category")
        
        return current_videos
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

def validate_and_get_video_path(selected_video):
    """Validate the selected video and return its full path"""
    if not selected_video or not validate_video_filename(selected_video):
        raise ValueError(f"Invalid video filename: {selected_video}")
    
    video_path = os.path.join(VIDEO_FOLDER, selected_video)
    if not os.path.exists(video_path):
        raise ValueError(f"Video file not found: {selected_video}")
    
    return video_path

def escape_ffmpeg_path(file_path):
    """
    Properly escape file paths for FFmpeg concat demuxer.
    Handles emojis, Unicode characters, spaces, and special characters.
    """
    # Normalize the path to use forward slashes (works on Windows too)
    normalized_path = os.path.normpath(file_path).replace('\\', '/')
    
    # Escape single quotes by replacing ' with '\''
    escaped_path = normalized_path.replace("'", "'\\''")
    
    return escaped_path

def create_playlist_file():
    """Create a temporary playlist file with all videos in random order"""
    try:
        videos = list_videos()
        if not videos:
            raise ValueError("No videos available for playlist")
        
        # Create a temporary playlist file
        fd, playlist_path = tempfile.mkstemp(suffix='.txt', text=True)
        os.close(fd)
        
        # Add to global list for cleanup (for atexit cleanup)
        temp_files.append(playlist_path)
        
        # Prepare videos for playlist
        if stream_state.shuffle_mode:
            # Shuffle the playlist
            shuffled_videos = videos.copy()
            random.shuffle(shuffled_videos)
            playlist_videos = shuffled_videos
        else:
            # Use original order
            playlist_videos = videos
        
        # Write playlist file with UTF-8 encoding and proper escaping
        valid_video_count = 0
        with open(playlist_path, 'w', encoding='utf-8') as f:
            for video in playlist_videos:
                video_path = os.path.join(VIDEO_FOLDER, video)
                
                # CRITICAL: Validate that the file exists and is accessible
                if not os.path.exists(video_path):
                    logger.warning(f"Video file not found, skipping: {video_path}")
                    continue
                
                if not os.access(video_path, os.R_OK):
                    logger.warning(f"Video file not readable, skipping: {video_path}")
                    continue
                
                # PROPERLY escape the file path for FFmpeg concat demuxer
                # This handles emojis, Unicode characters, spaces, and special characters
                escaped_path = escape_ffmpeg_path(video_path)
                
                # Write with proper FFmpeg concat format
                f.write(f"file '{escaped_path}'\n")
                valid_video_count += 1
                
                # Log the first few entries for debugging
                if valid_video_count <= 3:
                    logger.debug(f"Playlist entry {valid_video_count}: {video_path} -> '{escaped_path}'")
        
        if valid_video_count == 0:
            os.remove(playlist_path)
            raise ValueError("Playlist created but contains no valid/accessible video files")
        
        # Set the new playlist file (this will defer cleanup of old one)
        stream_state.set_playlist_file(playlist_path)
        
        logger.info(f"Created UTF-8 playlist with {valid_video_count} valid videos at {playlist_path}")
        
        # Verify the playlist file was created with proper content
        try:
            with open(playlist_path, 'r', encoding='utf-8') as f:
                first_line = f.readline().strip()
                logger.debug(f"First playlist line: {first_line}")
        except Exception as e:
            logger.warning(f"Could not verify playlist file content: {e}")
        
        # Clean up old playlist files that are no longer needed
        stream_state.cleanup_old_playlist_files()
        
        return playlist_path
        
    except Exception as e:
        logger.error(f"Failed to create playlist file: {e}")
        # Clean up the invalid playlist file if it was created
        if 'playlist_path' in locals() and os.path.exists(playlist_path):
            try:
                os.remove(playlist_path)
            except:
                pass
        raise

def build_cmd(encoder, preset, bitrate, out_url, selected_video=None):
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
    
    # Handle different input types
    if stream_state.shuffle_mode:
        # Shuffle mode enabled: Create playlist with all videos (ignore specific video selection)
        playlist_file = create_playlist_file()
        # Robust input args for concat demuxer with UTF-8 support
        input_args = [
            "-re",
            "-f", "concat",
            "-safe", "0",
            "-i", playlist_file
        ]
        logger.info(f"Shuffle mode enabled - playing all {len(stream_state.playlist)} videos in random order")
    elif selected_video:
        # Specific video selected and shuffle disabled: Play only that video
        video_path = validate_and_get_video_path(selected_video)
        # Escape the path for the single video input
        escaped_video_path = escape_ffmpeg_path(video_path)
        input_args = ["-re", "-stream_loop", "-1", "-i", escaped_video_path]
        logger.info(f"Playing single video: {selected_video}")
    else:
        # Fallback: Use first video (when no selection and shuffle disabled)
        videos = list_videos()
        if videos:
            video_path = os.path.join(VIDEO_FOLDER, videos[0])
            escaped_video_path = escape_ffmpeg_path(video_path)
            input_args = ["-re", "-stream_loop", "-1", "-i", escaped_video_path]
            logger.info(f"No video selected, playing first video: {videos[0]}")
        else:
            raise ValueError("No videos available")
    
    # Build the complete command
    cmd = (["ffmpeg", "-loglevel", "verbose", "-re"] + 
           input_args +
           pre + vcodec + common_rc + vf + audio + common_ts + 
           ["-f", "flv", out_url])
    
    logger.info(f"Built FFmpeg command with shuffle={stream_state.shuffle_mode}, selected_video={selected_video}")
    logger.debug(f"FFmpeg command: {' '.join(cmd)}")
    return cmd

def start_supervised(cmd):
    """Start FFmpeg with supervision and auto-restart"""
    global ffmpeg_process, stop_requested, health_check_thread
    backoff = 2
    restarts = 0
    last_restart_time = time.time()
    
    # Error counting for transient errors
    transient_error_count = 0
    MAX_TRANSIENT_ERRORS = 20  # Allow more since these are normal during transitions
    
    logger.info("Starting supervised FFmpeg process")
    
    # Start health check thread
    health_check_thread = threading.Thread(target=check_ffmpeg_health, daemon=True)
    health_check_thread.start()
    
    while restarts < MAX_RESTARTS and not stop_requested:
        stream_state.set_running(True)
        start_time = time.time()
        
        try:
            logger.info(f"Starting FFmpeg process (attempt {restarts + 1})")
            
            # Set environment to ensure UTF-8 support
            env = os.environ.copy()
            env['LC_ALL'] = 'C.UTF-8'
            env['LANG'] = 'C.UTF-8'
            
            ffmpeg_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
                encoding='utf-8',
                errors='replace',  # Handle encoding errors gracefully
                env=env,
                preexec_fn=os.setsid if os.name != 'nt' else None
            )
            
            stream_state.process_id = ffmpeg_process.pid
            log_buffer.append(f"[supervisor] Started FFmpeg process (PID: {ffmpeg_process.pid})")
            logger.debug(f"FFmpeg command: {' '.join(cmd)}")
            
            # Read output line by line with timeout
            while not stop_requested:
                try:
                    line = ffmpeg_process.stdout.readline()
                    if not line:  # EOF reached
                        break
                    log_buffer.append(line.rstrip("\n"))
                    
                    line_lower = line.lower()
                    
                    # Enhanced error detection with specific error patterns
                    # Handle transient decoding errors gracefully (normal during video transitions)
                    if "invalid data found when processing input" in line_lower:
                        transient_error_count += 1
                        logger.warning(f"FFmpeg transient error #{transient_error_count} (normal during transitions): {line.strip()}")
                        
                        if transient_error_count >= MAX_TRANSIENT_ERRORS:
                            logger.error("Too many consecutive decoding errors - terminating stream")
                            stream_state.set_error("Multiple consecutive decoding failures")
                            ffmpeg_process.terminate()
                            break
                        # Don't break - let FFmpeg continue processing
                        continue
                    
                    # Reset transient error counter on normal frame output
                    if "frame=" in line_lower and transient_error_count > 0:
                        logger.info(f"Stream recovered after {transient_error_count} transient errors")
                        transient_error_count = 0
                    
                    # Check for other non-fatal warnings
                    if ("error" in line_lower or "fail" in line_lower or 
                        "invalid" in line_lower or "unable" in line_lower):
                        logger.warning(f"FFmpeg warning/error detected: {line.strip()}")
                        # Reset transient counter on other types of errors
                        transient_error_count = 0
                        
                    # Specifically check for critical errors that require restart
                    if "no such file or directory" in line_lower:
                        logger.error(f"FFmpeg file not found error: {line.strip()}")
                        stream_state.set_error(f"File not found: {line.strip()}")
                        # Force restart with backoff
                        ffmpeg_process.terminate()
                        break
                        
                    # Check for permission errors
                    if "permission denied" in line_lower:
                        logger.error(f"FFmpeg permission error: {line.strip()}")
                        stream_state.set_error(f"Permission denied: {line.strip()}")
                        # Force restart with backoff
                        ffmpeg_process.terminate()
                        break
                        
                    # Check for codec/format errors
                    if "unsupported codec" in line_lower or "invalid data" in line_lower:
                        # Note: "invalid data" without "processing input" might be more serious
                        if "processing input" not in line_lower:
                            logger.error(f"FFmpeg codec/format error: {line.strip()}")
                            stream_state.set_error(f"Codec/format error: {line.strip()}")
                            # Force restart with backoff
                            ffmpeg_process.terminate()
                            break
                    
                except Exception as e:
                    logger.error(f"Error reading FFmpeg output: {e}")
                    break
            
            rc = ffmpeg_process.wait()
            stream_state.set_running(False)
            
            # Calculate how long FFmpeg ran
            run_time = time.time() - start_time
            logger.info(f"FFmpeg exited with code {rc} after {run_time:.1f} seconds")
            
            if stop_requested:
                log_buffer.append("[supervisor] Stop requested, exiting supervisor")
                break
            
            restarts += 1
            stream_state.increment_restarts()
            
            # Check if we're restarting too frequently
            time_since_last_restart = time.time() - last_restart_time
            last_restart_time = time.time()
            
            if time_since_last_restart < 60:  # Restarted less than 60 seconds ago
                logger.warning(f"Frequent restarts detected: {time_since_last_restart:.1f}s between restarts")
            
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
        # Use the current DEFAULTS (which may have been updated from Redis)
        form = DEFAULTS.copy()
        videos = list_videos()
        
        # Pre-select first video if available and no video is selected
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
        
        # Update DEFAULTS with current form values for persistence
        for key, value in form.items():
            if key in DEFAULTS:
                DEFAULTS[key] = value
        
        # Save form settings to Redis
        save_state()
        
        # Get enabled destinations from stored destinations
        enabled_destinations = []
        for dest in stream_state.destinations:
            if dest.get('enabled') and dest.get('key') and dest.get('url'):
                stream_url = f"{dest['url']}{dest['key']}"
                enabled_destinations.append(stream_url)
        
        # Comprehensive input validation
        if not enabled_destinations:
            return jsonify({"running": False, "error": "At least one destination must be enabled with both URL and stream key"}), 400
        
        if not validate_bitrate(form["bitrate"]):
            return jsonify({"running": False, "error": "Invalid bitrate format (use format like '2500k' or '2500')"}), 400
        
        if not validate_encoder(form["encoder"]):
            return jsonify({"running": False, "error": "Invalid encoder selection"}), 400
        
        # Input-specific validation
        if form["input_type"] == "file":
            # Check if we have videos
            videos = list_videos()
            if not videos:
                return jsonify({"running": False, "error": "No video files found. Upload videos to the video folder"}), 400
            
            # Validate selected video if provided
            if form["video"] and not validate_video_filename(form["video"]):
                return jsonify({"running": False, "error": "Invalid video filename"}), 400
            
            # Check if selected video exists
            if form["video"]:
                video_path = os.path.join(VIDEO_FOLDER, form["video"])
                if not os.path.exists(video_path):
                    return jsonify({"running": False, "error": f"Video file not found: {form['video']}"}), 400
            
        elif form["input_type"] == "srt":
            if not form["srt_url"] or not validate_srt_url(form["srt_url"]):
                return jsonify({"running": False, "error": "Invalid or missing SRT / RTMP input URL"}), 400
            
            # For SRT input, we don't need playlist handling
            input_args = ["-re", "-rw_timeout", "15000000", "-i", form["srt_url"]]
            
            # Build FFmpeg command for the first enabled destination
            try:
                cmd = build_cmd(
                    encoder=form["encoder"],
                    preset=form["preset"],
                    bitrate=form["bitrate"],
                    out_url=enabled_destinations[0]  # Use the first enabled destination
                )
            except Exception as e:
                logger.error(f"Failed to build FFmpeg command: {e}")
                return jsonify({"running": False, "error": f"Failed to build command: {str(e)}"}), 400
        else:
            return jsonify({"running": False, "error": "Invalid input type"}), 400
        
        # Build FFmpeg command for the first enabled destination
        try:
            selected_video = form["video"] if form["input_type"] == "file" else None
            cmd = build_cmd(
                encoder=form["encoder"],
                preset=form["preset"],
                bitrate=form["bitrate"],
                out_url=enabled_destinations[0],  # Use the first enabled destination
                selected_video=selected_video
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
        
        if form["input_type"] == "file":
            if stream_state.shuffle_mode:
                input_desc = f"all {len(stream_state.playlist)} videos (shuffled)"
            else:
                input_desc = f"'{form['video']}'" if form["video"] else f"first video"
        else:
            input_desc = form["srt_url"]
            
        success_msg = f"Started: {input_desc} @ {form['bitrate']} ({form['encoder']}, preset {form['preset']}) to {len(enabled_destinations)} destinations"
        
        log_buffer.append(f"[system] {success_msg}")
        logger.info(success_msg)
        
        return jsonify({
            "running": True,
            "message": success_msg,
            "enabled_destinations": len(enabled_destinations),
            "video_count": len(stream_state.playlist),
            "shuffle_mode": stream_state.shuffle_mode
        })
        
    except Exception as e:
        logger.error(f"Error in toggle endpoint: {e}")
        return jsonify({"running": False, "error": f"Internal error: {str(e)}"}), 500

@app.route("/destinations", methods=["GET", "POST"])
def destinations():
    """Get or save streaming destinations"""
    try:
        if request.method == "GET":
            # Return current destinations
            return jsonify({"destinations": stream_state.destinations})
        
        elif request.method == "POST":
            # Save new destinations
            data = request.get_json()
            if not data or not isinstance(data, list):
                return jsonify({"error": "Invalid destinations data"}), 400
            
            # Validate and save destinations
            valid_destinations = []
            for dest in data:
                if not isinstance(dest, dict):
                    continue
                
                valid_dest = {
                    'id': dest.get('id', ''),
                    'name': dest.get('name', ''),
                    'url': dest.get('url', ''),
                    'key': dest.get('key', ''),
                    'enabled': dest.get('enabled', False),
                    'type': dest.get('type', 'rtmp'),
                    'status': dest.get('status', 'inactive')
                }
                valid_destinations.append(valid_dest)
            
            stream_state.set_destinations(valid_destinations)
            
            # Save to Redis if available
            if redis_client:
                redis_client.set('stream:destinations', json.dumps(valid_destinations))
            
            log_buffer.append(f"[destinations] Saved {len(valid_destinations)} destinations")
            return jsonify({"message": "Destinations saved successfully", "count": len(valid_destinations)})
            
    except Exception as e:
        logger.error(f"Error in destinations endpoint: {e}")
        return jsonify({"error": f"Internal error: {str(e)}"}), 500

@app.route("/category", methods=["POST"])
def set_category():
    """Set the current content category"""
    try:
        data = request.get_json()
        if not data or 'category' not in data:
            return jsonify({"error": "Missing category parameter"}), 400
        
        category = data['category']
        if category not in ['all', 'justchatting', 'pool']:
            return jsonify({"error": "Invalid category"}), 400
        
        # Stop current stream if running
        global stop_requested
        if stream_state.running:
            stop_requested = True
            if ffmpeg_process and ffmpeg_process.poll() is None:
                try:
                    ffmpeg_process.terminate()
                    ffmpeg_process.wait(timeout=3)
                except:
                    try:
                        ffmpeg_process.kill()
                    except:
                        pass
        
        stream_state.set_category(category)
        scan_videos()  # Rescan videos for the new category
        
        # Save to Redis if available
        if redis_client:
            redis_client.set('stream:category', category)
        
        log_buffer.append(f"[category] Switched to {category} category")
        return jsonify({"message": f"Category set to {category}", "category": category})
        
    except Exception as e:
        logger.error(f"Error setting category: {e}")
        return jsonify({"error": f"Internal error: {str(e)}"}), 500

@app.route("/category/stats")
def get_category_stats():
    """Get video counts for each category"""
    try:
        return jsonify(stream_state.category_stats)
    except Exception as e:
        logger.error(f"Error getting category stats: {e}")
        return jsonify({"error": "Failed to get category stats"}), 500

@app.route("/shuffle", methods=["POST"])
def toggle_shuffle():
    """Toggle shuffle mode"""
    try:
        data = request.get_json()
        if not data or 'shuffle' not in data:
            return jsonify({"error": "Missing shuffle parameter"}), 400
        
        shuffle_mode = bool(data['shuffle'])
        stream_state.shuffle_mode = shuffle_mode
        
        # Update DEFAULTS for persistence
        DEFAULTS['shuffle_mode'] = 'true' if shuffle_mode else 'false'
        save_state()
        
        # Save shuffle mode to Redis if available
        if redis_client:
            redis_client.set('stream:shuffle_mode', json.dumps(shuffle_mode))
        
        # If stream is running, restart with new shuffle setting
        global stop_requested
        if stream_state.running:
            stop_requested = True
            if ffmpeg_process and ffmpeg_process.poll() is None:
                try:
                    ffmpeg_process.terminate()
                    ffmpeg_process.wait(timeout=3)
                except:
                    try:
                        ffmpeg_process.kill()
                    except:
                        pass
            
            # Wait a moment before potentially restarting
            time.sleep(1)
            
            # The supervisor thread will automatically restart the stream
        
        log_buffer.append(f"[shuffle] Shuffle mode {'enabled' if shuffle_mode else 'disabled'}")
        return jsonify({"shuffle": shuffle_mode, "message": f"Shuffle mode {'enabled' if shuffle_mode else 'disabled'}"})
        
    except Exception as e:
        logger.error(f"Error toggling shuffle: {e}")
        return jsonify({"error": f"Internal error: {str(e)}"}), 500

@app.route("/skip", methods=["POST"])
def skip_video():
    """Skip to the next video in the playlist"""
    try:
        if not stream_state.running:
            return jsonify({"error": "Stream is not running"}), 400
        
        # Send a signal to FFmpeg to skip to next video
        if ffmpeg_process and ffmpeg_process.poll() is None:
            # Send 'q' to FFmpeg to quit gracefully and let supervisor restart
            try:
                ffmpeg_process.terminate()
                log_buffer.append("[system] Video skipped by user - restarting stream")
            except Exception as e:
                logger.error(f"Error skipping video: {e}")
                return jsonify({"error": f"Failed to skip video: {e}"}), 500
        
        return jsonify({"message": "Skipping to next video", "success": True})
        
    except Exception as e:
        logger.error(f"Error skipping video: {e}")
        return jsonify({"error": f"Internal error: {str(e)}"}), 500

@app.route("/status")
def status():
    """Get current streaming status"""
    try:
        status_data = stream_state.get_status()
        status_data["video_count"] = len(list_videos())
        status_data["shuffle_mode"] = stream_state.shuffle_mode
        status_data["current_category"] = stream_state.current_category
        
        # Count enabled destinations for status display
        enabled_count = sum(1 for dest in stream_state.destinations if dest.get('enabled') and dest.get('key') and dest.get('url'))
        status_data["enabled_destinations"] = enabled_count
        
        # Add resource usage information if available
        try:
            process = psutil.Process()
            status_data["memory_usage_mb"] = process.memory_info().rss / 1024 / 1024
            status_data["cpu_percent"] = process.cpu_percent(interval=0.1)
        except:
            status_data["memory_usage_mb"] = 0
            status_data["cpu_percent"] = 0
        
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

@app.route("/debug-status")
def debug_status():
    """Check if debug mode is working"""
    return jsonify({
        "debug_mode": DEBUG_MODE,
        "environment_debug": os.getenv('DEBUG'),
        "flask_debug": app.debug,
        "log_level": logging.getLevelName(logger.getEffectiveLevel()),
        "log_buffer_size": len(log_buffer),
        "resource_monitor_interval": RESOURCE_MONITOR_INTERVAL,
        "health_check_interval": HEALTH_CHECK_INTERVAL,
        "status_log_interval": STATUS_LOG_INTERVAL
    })

@app.route("/test-debug-log")
def test_debug_log():
    """Test debug logging"""
    logger.debug("This is a DEBUG level message")
    logger.info("This is an INFO level message")
    logger.warning("This is a WARNING level message")
    logger.error("This is an ERROR level message")
    return "Check your logs for debug messages"

@app.route("/resource-usage")
def resource_usage():
    """Get current resource usage"""
    try:
        process = psutil.Process()
        memory_mb = process.memory_info().rss / 1024 / 1024
        cpu_percent = process.cpu_percent(interval=1)
        
        return jsonify({
            "memory_usage_mb": round(memory_mb, 2),
            "cpu_percent": round(cpu_percent, 1),
            "thread_count": threading.active_count(),
            "ffmpeg_running": ffmpeg_process and ffmpeg_process.poll() is None if ffmpeg_process else False
        })
    except Exception as e:
        logger.error(f"Error getting resource usage: {e}")
        return jsonify({"error": "Failed to get resource usage"}), 500

@app.route("/health")
def health():
    """Health check endpoint with encoder availability"""
    try:
        encoders = {
            "cpu": {"name": "libx264", "available": True},
            "nvidia": {"name": "h264_nvenc", "available": True},  # Could add GPU detection
            "intel": {"name": "h264_vaapi", "available": os.path.exists("/dev/dri/renderD128")}
        }
        
        # Get resource usage
        process = psutil.Process()
        memory_mb = process.memory_info().rss / 1024 / 1024
        cpu_percent = process.cpu_percent(interval=0.1)
        
        return jsonify({
            "status": "healthy",
            "encoders": encoders,
            "video_folder": VIDEO_FOLDER,
            "video_count": len(list_videos()),
            "category_video_count": stream_state.category_stats,
            "current_category": stream_state.current_category,
            "max_restarts": MAX_RESTARTS,
            "destinations_count": len(stream_state.destinations),
            "redis_connected": redis_client is not None and redis_client.ping(),
            "debug_mode": DEBUG_MODE,
            "memory_usage_mb": round(memory_mb, 2),
            "cpu_percent": round(cpu_percent, 1),
            "thread_count": threading.active_count()
        })
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/debug-playlist")
def debug_playlist():
    """Debug endpoint to check playlist contents"""
    try:
        if not stream_state.current_playlist_file or not os.path.exists(stream_state.current_playlist_file):
            return jsonify({"error": "No active playlist file"})
        
        with open(stream_state.current_playlist_file, 'r', encoding='utf-8') as f:
            playlist_contents = f.readlines()
        
        # Check if files in playlist actually exist
        file_status = []
        for line in playlist_contents:
            if line.startswith("file '"):
                # Extract the file path
                file_path = line[6:-2].replace("'\\''", "'")  # Unescape
                exists = os.path.exists(file_path)
                file_status.append({
                    "path": file_path,
                    "exists": exists,
                    "line": line.strip()
                })
        
        return jsonify({
            "playlist_file": stream_state.current_playlist_file,
            "total_entries": len(playlist_contents),
            "existing_files": sum(1 for f in file_status if f['exists']),
            "missing_files": sum(1 for f in file_status if not f['exists']),
            "file_status": file_status
        })
    except Exception as e:
        return jsonify({"error": str(e)})

def init_state():
    try:
        if redis_client:
            # Load playlist
            playlist_json = redis_client.get('stream:playlist')
            if playlist_json:
                stream_state.set_playlist(json.loads(playlist_json))
            
            # Load destinations
            destinations_json = redis_client.get('stream:destinations')
            if destinations_json:
                stream_state.set_destinations(json.loads(destinations_json))
            
            # Load category from Redis
            category = redis_client.get('stream:category')
            if category:
                stream_state.set_category(category)
                
            # Load category stats from Redis
            category_stats_json = redis_client.get('stream:category_stats')
            if category_stats_json:
                stream_state.category_stats = json.loads(category_stats_json)
            
            # Load shuffle mode from Redis
            shuffle_json = redis_client.get('stream:shuffle_mode')
            if shuffle_json:
                stream_state.shuffle_mode = json.loads(shuffle_json)
                
            # Load form settings from Redis
            form_settings_json = redis_client.get('stream:form_settings')
            if form_settings_json:
                global DEFAULTS
                DEFAULTS.update(json.loads(form_settings_json))
                
            logger.info("State initialized from Redis")
    except Exception as e:
        logger.error(f"Error initializing state from Redis: {e}")

def save_state():
    try:
        if redis_client:
            redis_client.set('stream:playlist', json.dumps(stream_state.playlist))
            redis_client.set('stream:destinations', json.dumps(stream_state.destinations))
            redis_client.set('stream:category', stream_state.current_category)
            redis_client.set('stream:category_stats', json.dumps(stream_state.category_stats))
            redis_client.set('stream:shuffle_mode', json.dumps(stream_state.shuffle_mode))
            redis_client.set('stream:form_settings', json.dumps(DEFAULTS))
    except Exception as e:
        logger.error(f"Error saving state to Redis: {e}")

@app.route("/videos")
def get_videos():
    """Get available videos for the current category"""
    try:
        videos = list_videos()
        return jsonify({"videos": videos})
    except Exception as e:
        logger.error(f"Error getting videos: {e}")
        return jsonify({"videos": []})

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {error}")
    return jsonify({"error": "Internal server error"}), 500

if __name__ == "__main__":
    logger.info(f"DEBUG environment variable: {os.getenv('DEBUG', 'not set')}")
    logger.info(f"Debug mode enabled: {DEBUG_MODE}")
    logger.info(f"Starting Flask app in {'DEBUG' if DEBUG_MODE else 'PRODUCTION'} mode")
    logger.info(f"Video folder: {VIDEO_FOLDER}")
    logger.info(f"Max log lines: {MAX_LOG_LINES}")
    logger.info(f"Max restarts: {MAX_RESTARTS}")
    logger.info(f"Resource monitor interval: {RESOURCE_MONITOR_INTERVAL}s")
    logger.info(f"Health check interval: {HEALTH_CHECK_INTERVAL}s")
    logger.info(f"Status log interval: {STATUS_LOG_INTERVAL}s")
    
    init_state()
    scan_videos()
    app.run(host="0.0.0.0", port=5000, debug=DEBUG_MODE)
