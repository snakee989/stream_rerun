from flask import Flask, render_template_string, request, send_file
import os

app = Flask(__name__)

# Folder where your videos are stored
VIDEO_FOLDER = "./videos"
videos = [f for f in os.listdir(VIDEO_FOLDER) if f.endswith(".mp4")]

# Default video
current_video = videos[0] if videos else None

# HTML template as a string (all in one file)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Video Switcher</title>
    <style>
        body { font-family: Arial, sans-serif; background: #121212; color: #fff; text-align: center; }
        select, button { padding: 10px; margin: 10px; font-size: 16px; }
        video { margin-top: 20px; max-width: 90%; border: 2px solid #fff; }
    </style>
</head>
<body>
    <h1>Dynamic Video Switcher</h1>
    <form method="POST">
        <select name="video">
            {% for v in videos %}
            <option value="{{ v }}" {% if v == current_video %}selected{% endif %}>{{ v }}</option>
            {% endfor %}
        </select>
        <button type="submit">Switch Video</button>
    </form>

    {% if current_video %}
    <video controls autoplay>
        <source src="/video" type="video/mp4">
        Your browser does not support the video tag.
    </video>
    {% else %}
    <p>No video found!</p>
    {% endif %}
</body>
</html>
"""

@app.route("/", methods=["GET", "POST"])
def index():
    global current_video
    if request.method == "POST":
        selected = request.form.get("video")
        if selected in videos:
            current_video = selected
    return render_template_string(HTML_TEMPLATE, videos=videos, current_video=current_video)

@app.route("/video")
def video():
    if current_video:
        return send_file(os.path.join(VIDEO_FOLDER, current_video))
    return "No video selected", 404

if __name__ == "__main__":
    os.makedirs(VIDEO_FOLDER, exist_ok=True)
    app.run(host="0.0.0.0", port=8080)
