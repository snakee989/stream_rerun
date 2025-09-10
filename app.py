import os
import subprocess
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)

VIDEO_FOLDER = "./videos"
DEFAULT_ENCODER = "libx264"
ffmpeg_process = None

# Ensure videos folder exists
os.makedirs(VIDEO_FOLDER, exist_ok=True)

def list_videos():
    return [f for f in os.listdir(VIDEO_FOLDER) if f.endswith(".mp4")]

@app.route("/", methods=["GET", "POST"])
def index():
    global ffmpeg_process
    logs = ""

    if request.method == "POST":
        action = request.form.get("action")
        stream_key = request.form.get("stream_key")
        input_type = request.form.get("input_type")
        encoder = request.form.get("encoder", DEFAULT_ENCODER)
        video = request.form.get("video")
        srt_url = request.form.get("srt_url")

        # Stop stream
        if action == "stop":
            if ffmpeg_process and ffmpeg_process.poll() is None:
                ffmpeg_process.terminate()
                ffmpeg_process = None
                logs = "Stream stopped."
            else:
                logs = "No stream running."

        # Start stream
        elif action == "start":
            if ffmpeg_process and ffmpeg_process.poll() is None:
                logs = "Stream already running."
            else:
                if input_type == "file":
                    input_path = os.path.join(VIDEO_FOLDER, video)
                    # Loop video indefinitely
                    cmd = [
                        "ffmpeg", "-re", "-stream_loop", "-1", "-i", input_path,
                        "-c:v", encoder,
                        "-preset", "veryfast",
                        "-b:v", "2500k",
                        "-maxrate", "2500k",
                        "-bufsize", "5000k",
                        "-pix_fmt", "yuv420p",
                        "-g", "50",
                        "-c:a", "copy",
                        "-f", "flv",
                        stream_key
                    ]
                elif input_type == "srt" and srt_url:
                    # Stream directly from URL (no loop)
                    cmd = [
                        "ffmpeg", "-re", "-i", srt_url,
                        "-c:v", encoder,
                        "-preset", "veryfast",
                        "-b:v", "2500k",
                        "-maxrate", "2500k",
                        "-bufsize", "5000k",
                        "-pix_fmt", "yuv420p",
                        "-g", "50",
                        "-c:a", "copy",
                        "-f", "flv",
                        stream_key
                    ]
                else:
                    logs = "Invalid input."
                    return render_template("index.html", videos=list_videos(), logs=logs, default_encoder=DEFAULT_ENCODER)

                ffmpeg_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                logs = f"Streaming started: {video if input_type=='file' else srt_url}"

    return render_template("index.html", videos=list_videos(), logs=logs, default_encoder=DEFAULT_ENCODER)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
