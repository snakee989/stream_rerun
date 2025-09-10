import os
import subprocess
from flask import Flask, request, render_template

# Flask will look for templates in the same folder as app.py
app = Flask(__name__, template_folder='.')

DEFAULT_ENCODER = "libx264"
ffmpeg_process = None

# Ensure videos folder exists
VIDEO_FOLDER = "videos"
os.makedirs(VIDEO_FOLDER, exist_ok=True)

@app.route("/", methods=["GET", "POST"])
def index():
    global ffmpeg_process
    logs = ""

    # List all mp4 videos
    videos = [f for f in os.listdir(VIDEO_FOLDER) if f.endswith(".mp4")]

    if request.method == "POST":
        action = request.form.get("action")
        input_type = request.form.get("input_type")
        encoder = request.form.get("encoder")
        output_url = request.form.get("stream_key")

        # Stop streaming
        if action == "stop":
            if ffmpeg_process and ffmpeg_process.poll() is None:
                ffmpeg_process.terminate()
                ffmpeg_process = None
                logs = "Stream stopped."
            return render_template("index.html", videos=videos, logs=logs, default_encoder=encoder)

        # Start streaming
        if action == "start":
            if input_type == "file":
                video_file = request.form.get("video")
                input_path = os.path.join(VIDEO_FOLDER, video_file)
            else:
                input_path = request.form.get("srt_url")

            # Stop previous stream if running
            if ffmpeg_process and ffmpeg_process.poll() is None:
                ffmpeg_process.terminate()

            cmd = [
                "ffmpeg",
                "-re",
                "-i", input_path,
                "-c:v", encoder,
                "-preset", "fast",
                "-c:a", "aac",
                "-f", "flv",
                output_url
            ]
            try:
                ffmpeg_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                logs = f"Streaming started: {input_path} -> {output_url}"
            except Exception as e:
                logs = f"Error: {e}"

    return render_template("index.html", videos=videos, logs=logs, default_encoder=DEFAULT_ENCODER)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
