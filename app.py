from flask import Flask, render_template, request
import subprocess
import os
import signal

app = Flask(__name__)

VIDEO_DIR = "/videos"
stream_proc = None

# Detect default GPU encoder
def detect_gpu():
    try:
        subprocess.run(["nvidia-smi"], stdout=subprocess.DEVNULL)
        return "h264_nvenc"
    except:
        pass
    # Add AMD/Intel detection if desired
    return "libx264"  # CPU fallback

# List videos
def get_videos():
    if not os.path.exists(VIDEO_DIR):
        os.makedirs(VIDEO_DIR)
    return [f for f in os.listdir(VIDEO_DIR) if f.lower().endswith((".mp4", ".mkv", ".mov"))]

@app.route("/", methods=["GET", "POST"])
def index():
    global stream_proc
    logs = ""
    videos = get_videos()
    default_encoder = detect_gpu()

    if request.method == "POST":
        action = request.form.get("action")
        encoder = request.form.get("encoder") or default_encoder
        input_type = request.form.get("input_type")

        if input_type == "file":
            video_name = request.form.get("video")
            input_file = os.path.join(VIDEO_DIR, video_name)
        else:
            input_file = request.form.get("srt_url")  # SRT/RTMP link

        stream_key = request.form.get("stream_key")

        if action == "start":
            loop_cmd = " -stream_loop -1 " if input_type == "file" else " "
            ffmpeg_cmd = f"ffmpeg -re {loop_cmd}-i {input_file} -c:v {encoder} -preset fast -c:a aac -b:a 128k -f flv {stream_key}"
            print("Running:", ffmpeg_cmd)
            stream_proc = subprocess.Popen(ffmpeg_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
            logs = f"Streaming started using encoder: {encoder}..."

        elif action == "stop" and stream_proc:
            os.killpg(os.getpgid(stream_proc.pid), signal.SIGTERM)
            stream_proc = None
            logs = "Streaming stopped."

    return render_template("index.html", videos=videos, logs=logs, default_encoder=default_encoder)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
