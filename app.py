from flask import Flask, render_template, request, redirect
import os
import subprocess

app = Flask(__name__)

VIDEO_DIR = "/videos"
LOG_FILE = "/app/stream.log"
default_encoder = "libx264"

# Ensure videos folder exists
os.makedirs(VIDEO_DIR, exist_ok=True)

def get_videos():
    return [f for f in os.listdir(VIDEO_DIR) if f.lower().endswith((".mp4", ".mkv", ".mov"))]

@app.route("/", methods=["GET", "POST"])
def index():
    videos = get_videos()
    logs = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            logs = f.readlines()[-50:]  # last 50 lines

    if request.method == "POST":
        video_name = request.form.get("video_name")
        srt_url = request.form.get("srt_url")
        stream_key = request.form.get("stream_key")
        encoder = request.form.get("encoder") or default_encoder

        if video_name:
            video_path = os.path.join(VIDEO_DIR, video_name)
            cmd = f"ffmpeg -re -i '{video_path}' -c:v {encoder} -f flv '{stream_key}'"
            subprocess.Popen(cmd, shell=True)
        elif srt_url:
            cmd = f"ffmpeg -re -i '{srt_url}' -c:v {encoder} -f flv '{stream_key}'"
            subprocess.Popen(cmd, shell=True)

        return redirect("/")

    return render_template("index.html", videos=videos, logs=logs, default_encoder=default_encoder)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
