import os
import signal
import subprocess
from flask import Flask, request, render_template_string

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
ffmpeg_process = None

os.makedirs(VIDEO_FOLDER, exist_ok=True)

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FFmpeg Stream Controller</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.1/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
  body { background-color: #0f1113; color: #e6e8ea; }
  .container { max-width: 840px; margin-top: 40px; padding: 28px; background-color: #171a1d; border-radius: 14px; box-shadow: 0 6px 24px rgba(0,0,0,0.4); }
  h1 { font-size: 1.6rem; }
  .form-label { font-weight: 600; }
  .hint { font-size: .85rem; color: #9aa0a6; }
  .btn-start { background-color: #1a7f37; color: #fff; }
  .btn-stop { background-color: #c0353a; color: #fff; }
  select, input { background-color: #1f2327; color: #e6e8ea; border: 1px solid #2c3136; }
  .logs { background-color: #111417; border: 1px solid #2c3136; padding: 10px; height: 160px; overflow-y: auto; border-radius: 8px; margin-top: 16px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; }
</style>
</head>
<body>
<div class="container">
  <div class="d-flex justify-content-between align-items-center mb-3">
    <h1 class="m-0">FFmpeg Stream Controller</h1>
    <span class="hint">GPU: NVIDIA (NVENC) • Intel (VAAPI/QSV)</span>
  </div>

  <form method="POST" class="mt-2">
    <div class="row g-3">
      <div class="col-12">
        <label for="stream_key" class="form-label">Stream Key / Output URL</label>
        <input type="text" class="form-control" id="stream_key" name="stream_key"
               placeholder="Twitch/YouTube RTMP or custom SRT link"
               value="{{ form.stream_key }}">
        <div class="hint mt-1">Examples: rtmp://live.twitch.tv/app/KEY or srt://host:port?streamid=…</div>
      </div>

      <div class="col-md-4">
        <label for="bitrate" class="form-label">Video Bitrate</label>
        <input type="text" class="form-control" id="bitrate" name="bitrate"
               placeholder="2500k" value="{{ form.bitrate }}">
      </div>

      <div class="col-md-4">
        <label for="input_type" class="form-label">Input Type</label>
        <select class="form-select" id="input_type" name="input_type">
          <option value="file" {% if form.input_type=='file' %}selected{% endif %}>Local Video</option>
          <option value="srt"  {% if form.input_type=='srt' %}selected{% endif %}>SRT / RTMP URL</option>
        </select>
      </div>

      <div class="col-md-4">
        <label for="encoder" class="form-label">Video Encoder</label>
        <select class="form-select" id="encoder" name="encoder">
          <option value="libx264" {% if form.encoder=='libx264' %}selected{% endif %}>CPU (libx264)</option>
          <option value="h264_nvenc" {% if form.encoder=='h264_nvenc' %}selected{% endif %}>NVIDIA GPU (NVENC)</option>
          <option value="h264_qsv" {% if form.encoder=='h264_qsv' %}selected{% endif %}>Intel iGPU (QSV)</option>
          <option value="h264_vaapi" {% if form.encoder=='h264_vaapi' %}selected{% endif %}>Intel/AMD (VAAPI)</option>
        </select>
        <div class="hint mt-1" id="preset-help"></div>
      </div>

      <div class="col-md-8" id="video-div">
        <label for="video" class="form-label">Select Video</label>
        <select class="form-select" id="video" name="video">
          {% for v in videos %}
            <option value="{{v}}" {% if form.video==v %}selected{% endif %}>{{v}}</option>
          {% endfor %}
        </select>
      </div>

      <div class="col-md-8" id="srt-div">
        <label for="srt_url" class="form-label">SRT / RTMP URL</label>
        <input type="text" class="form-control" id="srt_url" name="srt_url"
               placeholder="srt://host:port?streamid=..." value="{{ form.srt_url }}">
      </div>

      <div class="col-md-4">
        <label for="preset" class="form-label">Encoder Preset</label>
        <select class="form-select" id="preset" name="preset"></select>
      </div>
    </div>

    <div class="d-flex gap-2 mt-3">
      <button type="submit" name="action" value="start" class="btn btn-start">Start Stream</button>
      <button type="submit" name="action" value="stop"  class="btn btn-stop">Stop Stream</button>
    </div>
  </form>

  <div class="logs mt-3">{{ logs }}</div>
</div>

<script>
document.addEventListener("DOMContentLoaded", function() {
  const inputType   = document.getElementById("input_type");
  const videoDiv    = document.getElementById("video-div");
  const srtDiv      = document.getElementById("srt-div");
  const encoder     = document.getElementById("encoder");
  const preset      = document.getElementById("preset");
  const presetHelp  = document.getElementById("preset-help");
  const currentPreset = "{{ form.preset }}";

  function toggleInputs() {
    if (inputType.value === "file") {
      videoDiv.style.display = "block";
      srtDiv.style.display   = "none";
    } else {
      videoDiv.style.display = "none";
      srtDiv.style.display   = "block";
    }
  }

  const PRESET_OPTIONS = {
    // value -> label (human-friendly)
    "libx264": {
      "ultrafast":"Ultra fast (lowest quality)",
      "superfast":"Super fast",
      "veryfast":"Very fast",
      "faster":"Faster",
      "fast":"Fast",
      "medium":"Medium (default)",
      "slow":"Slow",
      "slower":"Slower",
      "veryslow":"Very slow (highest quality)",
      "placebo":"Placebo"
    },
    "h264_nvenc": {
      "p1":"P1 (fastest, lowest quality)",
      "p2":"P2 (faster)",
      "p3":"P3 (fast)",
      "p4":"P4 (medium, default)",
      "p5":"P5 (slow, good quality)",
      "p6":"P6 (slower, better quality)",
      "p7":"P7 (slowest, best quality)",
      "ll":"Low latency",
      "llhp":"Low latency (high perf)",
      "llhq":"Low latency (high quality)",
      "hp":"High performance",
      "hq":"High quality",
      "bd":"Blu-ray compatible",
      "lossless":"Lossless",
      "losslesshp":"Lossless (high perf)"
    },
    "h264_qsv": {
      "veryfast":"Very fast",
      "faster":"Faster",
      "fast":"Fast",
      "medium":"Medium",
      "slow":"Slow"
    },
    "h264_vaapi": {
      "medium":"Medium"
    }
  };

  const DEFAULT_FOR_ENCODER = {
    "libx264":"medium",
    "h264_nvenc":"p4",
    "h264_qsv":"medium",
    "h264_vaapi":"medium"
  };

  const PRESET_HELP = {
    "libx264":"CPU presets trade speed vs quality; slower = better compression.",
    "h264_nvenc":"p1..p7 are speed→quality; ll/llhp/llhq are low-latency modes; hq/hp/bd are legacy modes; lossless for no quality loss.",
    "h264_qsv":"Quick Sync presets trade speed vs quality.",
    "h264_vaapi":"VAAPI often ignores presets; quality depends on bitrate and driver."
  };

  function populatePresets() {
    const enc = encoder.value;
    const options = PRESET_OPTIONS[enc] || {};
    preset.innerHTML = "";
    Object.entries(options).forEach(([val,label]) => {
      const opt = document.createElement("option");
      opt.value = val; opt.text = label;
      preset.appendChild(opt);
    });
    const want = currentPreset && options[currentPreset] ? currentPreset : (DEFAULT_FOR_ENCODER[enc] || "medium");
    preset.value = want;
    presetHelp.textContent = PRESET_HELP[enc] || "";
  }

  encoder.addEventListener("change", populatePresets);
  inputType.addEventListener("change", toggleInputs);

  toggleInputs();
  populatePresets();
});
</script>
</body>
</html>
"""

def list_videos():
    vids = [f for f in os.listdir(VIDEO_FOLDER) if f.lower().endswith(".mp4")]
    return sorted(vids)

def build_cmd(input_args, encoder, preset, bitrate, out_url):
    # Common rate control
    buf = f"{int(bitrate[:-1])*2}k" if bitrate.endswith("k") and bitrate[:-1].isdigit() else bitrate
    common = ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", buf, "-g", "50"]
    # Audio: keep original behavior (copy); change to AAC if needed later
    audio = ["-c:a", "copy"]
    vf = []
    pre = []
    vcodec = []

    if encoder == "h264_nvenc":
        vcodec = ["-c:v", "h264_nvenc", "-preset", preset, "-pix_fmt", "yuv420p"]
    elif encoder == "h264_qsv":
        # QSV: prefer direct device; if it fails in some builds, consider VAAPI->QSV flow
        pre = ["-init_hw_device", "qsv=hw:/dev/dri/renderD128", "-filter_hw_device", "hw"]
        vf  = ["-vf", "format=nv12,hwupload=extra_hw_frames=64"]
        vcodec = ["-c:v", "h264_qsv", "-preset", preset]
    elif encoder == "h264_vaapi":
        pre = ["-vaapi_device", "/dev/dri/renderD128"]
        vf  = ["-vf", "format=nv12,hwupload"]
        vcodec = ["-c:v", "h264_vaapi"]
    else:
        vcodec = ["-c:v", "libx264", "-preset", preset, "-pix_fmt", "yuv420p"]

    return ["ffmpeg", "-loglevel", "verbose"] + pre + input_args + vcodec + common + vf + audio + ["-f", "flv", out_url]

@app.route("/", methods=["GET", "POST"])
def index():
    global ffmpeg_process
    # sticky form values
    form = {k: request.form.get(k, DEFAULTS[k]) for k in DEFAULTS}

    logs = ""
    if request.method == "POST":
        action = request.form.get("action")
        if action == "stop":
            if ffmpeg_process and ffmpeg_process.poll() is None:
                try:
                    ffmpeg_process.terminate()
                    ffmpeg_process.wait(timeout=3)
                except Exception:
                    try:
                        ffmpeg_process.kill()
                    except Exception:
                        pass
                ffmpeg_process = None
                logs = f"Stream stopped ({form.get('stream_key') or 'no key'})"
            else:
                logs = "No stream running."
        elif action == "start":
            if ffmpeg_process and ffmpeg_process.poll() is None:
                logs = "Stream already running."
            else:
                if not form["stream_key"]:
                    logs = "Stream key / output URL is required."
                    return render_template_string(HTML, videos=list_videos(), logs=logs, form=form)

                if form["input_type"] == "file":
                    if not form["video"]:
                        vids = list_videos()
                        form["video"] = vids if vids else ""
                    input_path = os.path.join(VIDEO_FOLDER, form["video"])
                    input_args = ["-re", "-stream_loop", "-1", "-i", input_path]
                elif form["input_type"] == "srt":
                    if not form["srt_url"]:
                        logs = "SRT / RTMP input URL is required."
                        return render_template_string(HTML, videos=list_videos(), logs=logs, form=form)
                    input_args = ["-re", "-i", form["srt_url"]]
                else:
                    logs = "Invalid input type."
                    return render_template_string(HTML, videos=list_videos(), logs=logs, form=form)

                cmd = build_cmd(
                    input_args=input_args,
                    encoder=form["encoder"],
                    preset=form["preset"],
                    bitrate=form["bitrate"],
                    out_url=form["stream_key"]
                )
                try:
                    ffmpeg_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    logs = f"Streaming started: {form['video'] if form['input_type']=='file' else form['srt_url']} @ {form['bitrate']} ({form['encoder']}, preset {form['preset']})"
                except Exception as e:
                    logs = f"Failed to start FFmpeg: {e}"

    return render_template_string(HTML, videos=list_videos(), logs=logs, form=form)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
