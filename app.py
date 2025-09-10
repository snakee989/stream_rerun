# app.py
from flask import Flask, render_template, request
import os
import subprocess

app = Flask(__name__)

VIDEO_FOLDER = "./videos"
DEFAULT_ENCODER = "libx264"
ffmpeg_process = None  # Global process variable

# Ensure videos folder exists
os.makedirs(VIDEO_FOLDER, exist_ok=True)

def start_stream(video_path, stream_url, encoder=DEFAULT_ENCODER):
    global ffmpeg_process

    # Stop previous process if running
    if ffmpeg_process and ffmpeg_process.poll() is None:
        ffmpeg_process.terminate()

    ffmpeg_cmd = [
        "ffmpeg",
        "-re",
        "-i", video_path,
        "-c:v", encoder,
        "-f", "flv",
        stream_url
    ]
    ffmpeg_process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

def stop_stream():
    global ffmpeg_process
    if ffmpeg_process and ffmpeg_process.poll() is None:
        ffmpeg_process.terminate()
        ffmpeg_process = None

def get_logs():
    global ffmpeg_process
    logs = ""
    if ffmpeg_process and ffmpeg_process.stdout:
        for line in ffmpeg_process.stdout.readlines():
            logs += line.decode(errors="ignore")
    return logs

@app.route("/", methods=["GET", "POST"])
def index():
    videos = [f for f in os.listdir(VIDEO_FOLDER) if f.endswith(".mp4")]
    logs = ""
    selected_video = videos[0] if videos else ""

    if request.method == "POST":
        action = request.form.get("action")
        input_type = request.form.get("input_type")
        encoder = request.form.get("encoder", DEFAULT_ENCODER)

        if input_type == "file":
            selected_video = request.form.get("video")
            video_path = os.path.join(VIDEO_FOLDER, selected_video)
            stream_url = request.form.get("stream_key")
        else:
            video_path = request.form.get("srt_url")
            stream_url = video_path  # For SRT/RTMP streaming, input is the URL

        if action == "start":
            start_stream(video_path, stream_url, encoder)
        elif action == "stop":
            stop_stream()

        logs = get_logs()

    return render_template(
        "index.html",
        videos=videos,
        logs=logs,
        default_encoder=DEFAULT_ENCODER
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
