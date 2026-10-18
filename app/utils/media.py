"""Saving an uploaded home-page video to disk.

Unlike images (which live in the database), a video is too large to keep there,
so it is written under the Flask instance folder - which is outside the code
tree, survives a git deploy, and is captured by a database-folder backup only
if you include it. Returns (filename, mimetype, error); the caller stores the
filename and type in Settings.
"""
import os
import time

from flask import current_app

ALLOWED = {"video/mp4": ".mp4", "video/webm": ".webm"}


def media_dir():
    path = current_app.config.get("MEDIA_DIR") or os.path.join(current_app.instance_path, "media")
    os.makedirs(path, exist_ok=True)
    return path


def save_home_video(upload):
    if not upload or not upload.filename:
        return None, None, None
    if upload.mimetype not in ALLOWED:
        return None, None, "The video must be an MP4 or WebM file."

    data = upload.read()
    if not data:
        return None, None, "That file appears to be empty."
    cap = current_app.config.get("MAX_VIDEO_BYTES", 50 * 1024 * 1024)
    if len(data) > cap:
        return None, None, f"The video must be smaller than {cap // (1024*1024)} MB."

    # Confirm the bytes really are the claimed container, not just the extension.
    is_mp4 = data[4:8] == b"ftyp"
    is_webm = data[:4] == b"\x1a\x45\xdf\xa3"
    if not (is_mp4 or is_webm):
        return None, None, "That file is not a valid MP4 or WebM video."

    ext = ".mp4" if is_mp4 else ".webm"
    filename = f"home_video_{int(time.time())}{ext}"
    with open(os.path.join(media_dir(), filename), "wb") as fh:
        fh.write(data)
    return filename, ("video/mp4" if is_mp4 else "video/webm"), None


def delete_media(filename):
    if not filename:
        return
    try:
        os.remove(os.path.join(media_dir(), filename))
    except OSError:
        pass
