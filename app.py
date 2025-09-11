import os
import subprocess
import threading
import time
from collections import deque
from flask import Flask, request, render_template, jsonify

app = Flask(__name__)

VIDEO_FOLDER = "./videos"
DEFAULTS = {
    "stream_key": "",
    "bitrate": "2500k",
    "input_type": "file",
    "video": "",
    "srt_url": "",
    "encoder": "libx264",
    "preset": "medium",
}

os.makedirs(VIDEO_FOLDER, exist_ok=True)

# Runtime state
ffmpeg_process = None
ffmpeg_thread = None
stop_requested = False
last_cmd = None
log_buffer = deque(maxlen=500)
state = {"running": False, "restarts": 0}

def list_videos():
    vids = [f for f in os.listdir(VIDEO_FOLDER) if f.lower().endswith(".mp4")]
    return sorted(vids)

def parse_bitrate_k(bitrate):
    # "2500k" -> 2500, otherwise None
    if bitrate.endswith("k") and bitrate[:-1].isdigit():
        return int(bitrate[:-1])
    return None

def build_cmd(input_args, encoder, preset, bitrate, out_url):
    # Common rate control (CBR triad + timed keyframes every 2s)
    # Keep a 2x buffer and force timed keyframes for Twitch; use GOP=120 as a general 2s@60fps default.
    bk = parse_bitrate_k(bitrate)
    buf = f"{bk*2}k" if bk else bitrate
    common_rc = [
        "-b:v", bitrate,
        "-maxrate", bitrate,
        "-bufsize", buf,
        "-g", "120",
        "-force_key_frames", "expr:gte(t,n_forced*2)"
    ]
    # Safer timestamps for long runs
    common_ts = ["-fflags", "+genpts"]

    # Audio: always re-encode to AAC 48 kHz stereo for streaming compatibility
    audio = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]

    pre = []
    vf = []
    vcodec = []

    if encoder == "h264_nvenc":
        # NVENC CBR + preset map; keep yuv420p for player compatibility
        vcodec = ["-c:v", "h264_nvenc", "-preset", preset, "-rc", "cbr", "-pix_fmt", "yuv420p"]
    elif encoder == "h264_qsv":
        # QSV via oneVPL with NV12 upload
        pre = ["-init_hw_device", "qsv=hw:/dev/dri/renderD128", "-filter_hw_device", "hw"]
        vf = ["-vf", "format=nv12,hwupload=extra_hw_frames=64"]
        vcodec = ["-c:v", "h264_qsv", "-preset", preset, "-look_ahead", "0"]
    elif encoder == "h264_vaapi":
        # VAAPI with NV12 upload; modest B-frames for quality
        pre = ["-vaapi_device", "/dev/dri/renderD128"]
        vf = ["-vf", "format=nv12,hwupload"]
        vcodec = ["-c:v", "h264_vaapi", "-bf", "2"]
    else:
        # CPU x264
        vcodec = ["-c:v", "libx264", "-preset", preset, "-pix_fmt", "yuv420p"]

    return ["ffmpeg", "-loglevel", "verbose"] + pre + input_args + vcodec + common_rc + vf + audio + common_ts + ["-f", "flv", out_url]

def start_supervised(cmd):
    global ffmpeg_process, state, stop_requested
    backoff = 2
    while True:
        state["running"] = True
        try:
            ffmpeg_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True
            )
            # Read logs
            for line in ffmpeg_process.stdout:
                log_buffer.append(line.rstrip("\n"))
            rc = ffmpeg_process.wait()
            state["running"] = False
            if stop_requested:
                break
            # Auto-restart with backoff
            state["restarts"] += 1
            log_buffer.append(f"[supervisor] ffmpeg exited (code={rc}), restarting in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            state["running"] = False
            if stop_requested:
                break
            log_buffer.append(f"[supervisor] exception: {e}, retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

@app.route("/", methods=["GET", "POST"])
def index():
    global ffmpeg_process, ffmpeg_thread, last_cmd, stop_requested
    form = {k: request.form.get(k, DEFAULTS[k]) for k in DEFAULTS}
    logs = ""

    if request.method == "POST":
        action = request.form.get("action")
        if action == "stop":
            stop_requested = True
            if ffmpeg_process and ffmpeg_process.poll() is None:
                try:
                    ffmpeg_process.terminate()
                    ffmpeg_process.wait(timeout=3)
                except Exception:
                    try:
                        ffmpeg_process.kill()
                    except Exception:
                        pass
            logs = f"Stop requested ({form.get('stream_key') or 'no key'})"
        elif action == "start":
            if state["running"]:
                logs = "Stream already running."
            else:
                # Validate output URL
                if not form["stream_key"]:
                    logs = "Stream key / output URL is required."
                    return render_template("index.html", videos=list_videos(), logs=logs, form=form)
                # Build input
                if form["input_type"] == "file":
                    if not form["video"]:
                        vids = list_videos()
                        form["video"] = vids if vids else ""
                    input_path = os.path.join(VIDEO_FOLDER, form["video"])
                    # Use -re for file pacing and loop
                    input_args = ["-re", "-stream_loop", "-1", "-i", input_path]
                elif form["input_type"] == "srt":
                    if not form["srt_url"]:
                        logs = "SRT / RTMP input URL is required."
                        return render_template("index.html", videos=list_videos(), logs=logs, form=form)
                    # Add general reconnect-ish options only helpful for some protocols
                    input_args = ["-re", "-rw_timeout", "15000000", "-i", form["srt_url"]]
                else:
                    logs = "Invalid input type."
                    return render_template("index.html", videos=list_videos(), logs=logs, form=form)

                cmd = build_cmd(
                    input_args=input_args,
                    encoder=form["encoder"],
                    preset=form["preset"],
                    bitrate=form["bitrate"],
                    out_url=form["stream_key"]
                )
                last_cmd = cmd
                stop_requested = False
                # Start supervisor thread
                ffmpeg_thread = threading.Thread(target=start_supervised, args=(cmd,), daemon=True)
                ffmpeg_thread.start()
                logs = f"Start requested: {form['video'] if form['input_type']=='file' else form['srt_url']} @ {form['bitrate']} ({form['encoder']}, preset {form['preset']})"

    return render_template("index.html", videos=list_videos(), logs=logs, form=form)

@app.route("/status")
def status():
    return jsonify({"running": state["running"], "restarts": state["restarts"]})

@app.route("/logs")
def logs():
    # Return latest 200 lines
    tail = list(log_buffer)[-200:]
    return jsonify({"lines": tail})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
