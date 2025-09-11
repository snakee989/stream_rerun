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
    if bitrate.endswith("k") and bitrate[:-1].isdigit():
        return int(bitrate[:-1])
    return None

def build_cmd(input_args, encoder, preset, bitrate, out_url):
    # Force timed keyframes every 2s for Twitch; keep a 2x buffer and a 2s GOP default [21][22]
    bk = parse_bitrate_k(bitrate)
    buf = f"{bk*2}k" if bk else bitrate
    common_rc = [
        "-b:v", bitrate,
        "-maxrate", bitrate,
        "-bufsize", buf,
        "-g", "120",
        "-force_key_frames", "expr:gte(t,n_forced*2)"
    ]
    common_ts = ["-fflags", "+genpts"]  # stable timestamps for long runs [23]

    # Always normalize audio for live ingest compatibility [21]
    audio = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]

    pre = []
    vf = []
    vcodec = []

    if encoder == "h264_nvenc":
        vcodec = ["-c:v", "h264_nvenc", "-preset", preset, "-rc", "cbr", "-pix_fmt", "yuv420p"]  # CBR path [24]
    elif encoder == "h264_qsv":
        pre = ["-init_hw_device", "qsv=hw:/dev/dri/renderD128", "-filter_hw_device", "hw"]
        vf = ["-vf", "format=nv12,hwupload=extra_hw_frames=64"]
        vcodec = ["-c:v", "h264_qsv", "-preset", preset, "-look_ahead", "0"]  # steady bitrate [25]
    elif encoder == "h264_vaapi":
        pre = ["-vaapi_device", "/dev/dri/renderD128"]
        vf = ["-vf", "format=nv12,hwupload"]
        vcodec = ["-c:v", "h264_vaapi", "-bf", "2"]  # moderate B-frames [24]
    else:
        vcodec = ["-c:v", "libx264", "-preset", preset, "-pix_fmt", "yuv420p"]  # CPU x264 [24]

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
            for line in ffmpeg_process.stdout:
                log_buffer.append(line.rstrip("\n"))
            rc = ffmpeg_process.wait()
            state["running"] = False
            if stop_requested:
                break
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

@app.route("/", methods=["GET"])
def index():
    form = {k: DEFAULTS[k] for k in DEFAULTS}
    return render_template("index.html", videos=list_videos(), logs="", form=form)

@app.route("/toggle", methods=["POST"])
def toggle():
    global ffmpeg_process, ffmpeg_thread, last_cmd, stop_requested
    # If running -> stop; else -> start using posted form fields [12]
    if state["running"]:
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
        return jsonify({"running": False, "message": "Stop requested."})
    # Not running -> start with current form data [12]
    form = {k: request.form.get(k, DEFAULTS[k]) for k in DEFAULTS}
    if not form["stream_key"]:
        return jsonify({"running": False, "error": "Stream key / output URL is required."}), 400
    if form["input_type"] == "file":
        if not form["video"]:
            vids = list_videos()
            form["video"] = vids if vids else ""
        input_path = os.path.join(VIDEO_FOLDER, form["video"])
        input_args = ["-re", "-stream_loop", "-1", "-i", input_path]
    elif form["input_type"] == "srt":
        if not form["srt_url"]:
            return jsonify({"running": False, "error": "SRT / RTMP input URL is required."}), 400
        # Use a conservative read timeout; reconnect is supervised outside [26]
        input_args = ["-re", "-rw_timeout", "15000000", "-i", form["srt_url"]]
    else:
        return jsonify({"running": False, "error": "Invalid input type."}), 400

    cmd = build_cmd(
        input_args=input_args,
        encoder=form["encoder"],
        preset=form["preset"],
        bitrate=form["bitrate"],
        out_url=form["stream_key"]
    )
    last_cmd = cmd
    stop_requested = False
    ffmpeg_thread = threading.Thread(target=start_supervised, args=(cmd,), daemon=True)
    ffmpeg_thread.start()
    return jsonify({
        "running": True,
        "message": f"Start requested: {form['video'] if form['input_type']=='file' else form['srt_url']} @ {form['bitrate']} ({form['encoder']}, preset {form['preset']})"
    })

@app.route("/status")
def status():
    return jsonify({"running": state["running"], "restarts": state["restarts"]})

@app.route("/logs")
def logs():
    tail = list(log_buffer)[-200:]
    return jsonify({"lines": tail})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
