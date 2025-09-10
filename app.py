import os
import subprocess
import threading
import time
from flask import Flask, render_template_string, request

app = Flask(__name__)

VIDEO_FOLDER = "./videos"
if not os.path.exists(VIDEO_FOLDER):
    os.makedirs(VIDEO_FOLDER)

ffmpeg_process = None
ffmpeg_logs = []

# Simple index.html template (use your existing HTML, but replace {% %} with string formatting)
INDEX_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FFmpeg Stream Controller</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.1/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
body { background-color: #121212; color: #f8f9fa; }
.container { max-width: 600px; margin-top: 50px; padding: 30px; background-color: #1e1e1e; border-radius: 12px; box-shadow: 0 0 20px rgba(0,0,0,0.5); }
h1 { text-align: center; margin-bottom: 30px; font-size: 2rem; }
.form-label { font-weight: 600; }
.btn-start { background-color: #28a745; color: white; }
.btn-stop { background-color: #dc3545; color: white; }
select, input { background-color: #2c2c2c; color: #f8f9fa; border: 1px solid #444; }
.logs { background-color: #2c2c2c; padding: 10px; height: 150px; overflow-y: auto; border-radius: 8px; margin-top: 20px; font-family: monospace; white-space: pre-line; }
</style>
</head>
<body>
<div class="container">
<h1>FFmpeg Stream Controller</h1>
<form method="POST">
<div class="mb-3">
<label for="stream_key" class="form-label">Stream Key / Output URL</label>
<input type="text" class="form-control" id="stream_key" name="stream_key" placeholder="Twitch/YouTube RTMP or custom SRT link" required>
</div>

<div class="mb-3">
<label for="video" class="form-label">Select Video</label>
<select class="form-select" id="video" name="video">
{% for v in videos %}
<option value="{{v}}">{{v}}</option>
{% endfor %}
</select>
</div>

<div class="d-flex justify-content-between">
<button type="submit" name="action" value="start" class="btn btn-start">Start Stream</button>
<button type="submit" name="action" value="stop" class="btn btn-stop">Stop Stream</button>
</div>
</form>

<div class="logs mt-4" id="logs">
{% if logs %}
{{ logs }}
{% endif %}
</div>

<script>
function fetchLogs() {
    fetch('/logs')
    .then(response => response.text())
    .then(data => {
        document.getElementById('logs').innerText = data;
    });
}
setInterval(fetchLogs, 1000);
</script>

</div>
</body>
</html>
"""

def stream_thread(video_path, stream_url, encoder):
    global ffmpeg_process, ffmpeg_logs
    ffmpeg_logs.append(f"Starting stream: {video_path} -> {stream_url}\n")
    command = [
        "ffmpeg", "-re", "-i", video_path,
        "-c:v", encoder,
        "-preset", "veryfast",
        "-b:v", "2500k",
        "-maxrate", "2500k",
        "-bufsize", "5000k",
        "-pix_fmt", "yuv420p",
        "-g", "50",
        "-c:a", "copy",
        "-f", "flv",
        stream_url
    ]
    ffmpeg_process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    for line in ffmpeg_process.stdout:
        ffmpeg_logs.append(line)
    ffmpeg_process = None
    ffmpeg_logs.append("Stream ended.\n")

@app.route("/", methods=["GET", "POST"])
def index():
    global ffmpeg_process, ffmpeg_logs
    videos = [f for f in os.listdir(VIDEO_FOLDER) if f.endswith(".mp4")]

    if request.method == "POST":
        action = request.form.get("action")
        if action == "start":
            if ffmpeg_process is None:
                video = request.form.get("video")
                stream_key = request.form.get("stream_key").strip()
                if not stream_key.startswith("rtmp://"):
                    ffmpeg_logs.append("Invalid RTMP URL!\n")
                else:
                    video_path = os.path.join(VIDEO_FOLDER, video)
                    encoder = "libx264"
                    threading.Thread(target=stream_thread, args=(video_path, stream_key, encoder), daemon=True).start()
            else:
                ffmpeg_logs.append("Stream is already running!\n")
        elif action == "stop":
            if ffmpeg_process is not None:
                ffmpeg_process.terminate()
                ffmpeg_process = None
                ffmpeg_logs.append("Stream stopped by user.\n")
            else:
                ffmpeg_logs.append("No stream running.\n")

    logs_text = "\n".join(ffmpeg_logs[-100:])
    return render_template_string(INDEX_HTML, videos=videos, logs=logs_text)

@app.route("/logs")
def get_logs():
    global ffmpeg_logs
    return "\n".join(ffmpeg_logs[-100:])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
