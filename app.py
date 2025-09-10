import os
import subprocess
from flask import Flask, request, render_template_string

app = Flask(__name__)

VIDEO_FOLDER = "./videos"
DEFAULT_ENCODER = "libx264"
DEFAULT_BITRATE = "2500k"
ffmpeg_process = None
last_stream_key = None
last_bitrate = DEFAULT_BITRATE

os.makedirs(VIDEO_FOLDER, exist_ok=True)

HTML = """<!DOCTYPE html>
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
    .logs { background-color: #2c2c2c; padding: 10px; height: 150px; overflow-y: auto; border-radius: 8px; margin-top: 20px; font-family: monospace; }
</style>
</head>
<body>
<div class="container">
    <h1>FFmpeg Stream Controller</h1>
    <form method="POST">
        <div class="mb-3">
            <label for="stream_key" class="form-label">Stream Key / Output URL</label>
            <input type="text" class="form-control" id="stream_key" name="stream_key" placeholder="Twitch/YouTube RTMP or custom SRT link" value="{{ last_stream_key }}">
        </div>

        <div class="mb-3">
            <label for="bitrate" class="form-label">Video Bitrate (e.g., 2500k)</label>
            <input type="text" class="form-control" id="bitrate" name="bitrate" value="{{ last_bitrate }}">
        </div>

        <div class="mb-3">
            <label for="input_type" class="form-label">Input Type</label>
            <select class="form-select" id="input_type" name="input_type">
                <option value="file">Local Video</option>
                <option value="srt">SRT / RTMP URL</option>
            </select>
        </div>

        <div class="mb-3" id="video-div">
            <label for="video" class="form-label">Select Video</label>
            <select class="form-select" id="video" name="video">
                {% for v in videos %}
                <option value="{{v}}">{{v}}</option>
                {% endfor %}
            </select>
        </div>

        <div class="mb-3" id="srt-div">
            <label for="srt_url" class="form-label">SRT / RTMP URL</label>
            <input type="text" class="form-control" id="srt_url" name="srt_url" placeholder="Paste your SRT/RTMP link here">
        </div>

        <div class="mb-3">
            <label for="encoder" class="form-label">Video Encoder</label>
            <select class="form-select" id="encoder" name="encoder">
                <option value="libx264" {% if default_encoder=="libx264" %}selected{% endif %}>CPU (libx264)</option>
                <option value="h264_nvenc" {% if default_encoder=="h264_nvenc" %}selected{% endif %}>NVIDIA GPU</option>
                <option value="h264_amf">AMD GPU</option>
                <option value="h264_qsv">Intel iGPU / QuickSync</option>
            </select>
        </div>

        <div class="d-flex justify-content-between">
            <button type="submit" name="action" value="start" class="btn btn-start">Start Stream</button>
            <button type="submit" name="action" value="stop" class="btn btn-stop">Stop Stream</button>
        </div>
    </form>

    <div class="logs mt-4">
        {% if logs %}
            {{ logs }}
        {% endif %}
    </div>
</div>

<script>
document.addEventListener("DOMContentLoaded", function() {
    const inputType = document.getElementById("input_type");
    const videoDiv = document.getElementById("video-div");
    const srtDiv = document.getElementById("srt-div");

    function toggleInputs() {
        if (inputType.value === "file") {
            videoDiv.style.display = "block";
            srtDiv.style.display = "none";
        } else {
            videoDiv.style.display = "none";
            srtDiv.style.display = "block";
        }
    }

    inputType.addEventListener("change", toggleInputs);
    toggleInputs();
});
</script>
</body>
</html>"""

def list_videos():
    return [f for f in os.listdir(VIDEO_FOLDER) if f.endswith(".mp4")]

@app.route("/", methods=["GET", "POST"])
def index():
    global ffmpeg_process, last_stream_key, last_bitrate
    logs = ""

    if request.method == "POST":
        action = request.form.get("action")
        stream_key = request.form.get("stream_key") or last_stream_key
        input_type = request.form.get("input_type")
        encoder = request.form.get("encoder", DEFAULT_ENCODER)
        video = request.form.get("video")
        srt_url = request.form.get("srt_url")
        bitrate = request.form.get("bitrate") or DEFAULT_BITRATE
        last_bitrate = bitrate

        if action == "stop":
            if ffmpeg_process and ffmpeg_process.poll() is None:
                ffmpeg_process.terminate()
                ffmpeg_process = None
                logs = f"Stream stopped ({last_stream_key})"
            else:
                logs = "No stream running."

        elif action == "start":
            if ffmpeg_process and ffmpeg_process.poll() is None:
                logs = "Stream already running."
            else:
                if not stream_key:
                    logs = "Stream key required to start."
                    return render_template_string(HTML, videos=list_videos(), logs=logs, default_encoder=DEFAULT_ENCODER, last_stream_key=last_stream_key, last_bitrate=last_bitrate)

                last_stream_key = stream_key

                if input_type == "file":
                    input_path = os.path.join(VIDEO_FOLDER, video)
                    cmd = [
                        "ffmpeg", "-re", "-stream_loop", "-1", "-i", input_path,
                        "-c:v", encoder,
                        "-preset", "veryfast",
                        "-b:v", bitrate,
                        "-maxrate", bitrate,
                        "-bufsize", str(int(bitrate.replace('k',''))*2) + "k",
                        "-pix_fmt", "yuv420p",
                        "-g", "50",
                        "-c:a", "copy",
                        "-f", "flv",
                        stream_key
                    ]
                elif input_type == "srt" and srt_url:
                    cmd = [
                        "ffmpeg", "-re", "-i", srt_url,
                        "-c:v", encoder,
                        "-preset", "veryfast",
                        "-b:v", bitrate,
                        "-maxrate", bitrate,
                        "-bufsize", str(int(bitrate.replace('k',''))*2) + "k",
                        "-pix_fmt", "yuv420p",
                        "-g", "50",
                        "-c:a", "copy",
                        "-f", "flv",
                        stream_key
                    ]
                else:
                    logs = "Invalid input."
                    return render_template_string(HTML, videos=list_videos(), logs=logs, default_encoder=DEFAULT_ENCODER, last_stream_key=last_stream_key, last_bitrate=last_bitrate)

                ffmpeg_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                logs = f"Streaming started: {video if input_type=='file' else srt_url} at {bitrate}"

    return render_template_string(HTML, videos=list_videos(), logs=logs, default_encoder=DEFAULT_ENCODER, last_stream_key=last_stream_key, last_bitrate=last_bitrate)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
