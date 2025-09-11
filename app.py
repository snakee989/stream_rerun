import os
import subprocess
from flask import Flask, request, render_template

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


def list_videos():
    vids = [f for f in os.listdir(VIDEO_FOLDER) if f.lower().endswith(".mp4")]
    return sorted(vids)


def build_cmd(input_args, encoder, preset, bitrate, out_url):
    # Common rate control
    buf = f"{int(bitrate[:-1])*2}k" if bitrate.endswith("k") and bitrate[:-1].isdigit() else bitrate
    common = ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", buf, "-g", "50"]
    # Audio: copy for now
    audio = ["-c:a", "copy"]
    vf = []
    pre = []
    vcodec = []

    if encoder == "h264_nvenc":
        vcodec = ["-c:v", "h264_nvenc", "-preset", preset, "-pix_fmt", "yuv420p"]
    elif encoder == "h264_qsv":
        pre = ["-init_hw_device", "qsv=hw:/dev/dri/renderD128", "-filter_hw_device", "hw"]
        vf = ["-vf", "format=nv12,hwupload=extra_hw_frames=64"]
        vcodec = ["-c:v", "h264_qsv", "-preset", preset]
    elif encoder == "h264_vaapi":
        pre = ["-vaapi_device", "/dev/dri/renderD128"]
        vf = ["-vf", "format=nv12,hwupload"]
        vcodec = ["-c:v", "h264_vaapi"]
    else:
        vcodec = ["-c:v", "libx264", "-preset", preset, "-pix_fmt", "yuv420p"]

    return ["ffmpeg", "-loglevel", "verbose"] + pre + input_args + vcodec + common + vf + audio + ["-f", "flv", out_url]


@app.route("/", methods=["GET", "POST"])
def index():
    global ffmpeg_process
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
                    return render_template("index.html", videos=list_videos(), logs=logs, form=form)

                if form["input_type"] == "file":
                    if not form["video"]:
                        vids = list_videos()
                        form["video"] = vids if vids else ""
                    input_path = os.path.join(VIDEO_FOLDER, form["video"])
                    input_args = ["-re", "-stream_loop", "-1", "-i", input_path]
                elif form["input_type"] == "srt":
                    if not form["srt_url"]:
                        logs = "SRT / RTMP input URL is required."
                        return render_template("index.html", videos=list_videos(), logs=logs, form=form)
                    input_args = ["-re", "-i", form["srt_url"]]
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
                try:
                    ffmpeg_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    logs = f"Streaming started: {form['video'] if form['input_type']=='file' else form['srt_url']} @ {form['bitrate']} ({form['encoder']}, preset {form['preset']})"
                except Exception as e:
                    logs = f"Failed to start FFmpeg: {e}"

    return render_template("index.html", videos=list_videos(), logs=logs, form=form)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
