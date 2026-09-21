"""Turning a pasted video link into something safe to embed.

Shared by class videos and subscription course lectures so both accept the same
links and both refuse the same ones. The player URL is always rebuilt from an id
extracted from a recognised host - never the pasted URL itself, which would let
a crafted link render arbitrary content inside the page.
"""
import re
from urllib.parse import urlparse, parse_qs

_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,80}$")
_VIMEO_ID = re.compile(r"^\d{6,12}$")


def is_acceptable_link(url):
    return bool(url) and url.lower().startswith(("http://", "https://"))


def embed_url_for(url):
    """A player URL to put in an iframe, or None to fall back to a plain link."""
    if not url:
        return None

    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    host = host[4:] if host.startswith("www.") else host
    video_id = ""

    if host == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/")[0]
    elif host in ("youtube.com", "m.youtube.com", "youtube-nocookie.com"):
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith(("/embed/", "/shorts/", "/live/")):
            parts = parsed.path.split("/")
            video_id = parts[2] if len(parts) > 2 else ""
    elif host == "drive.google.com":
        parts = parsed.path.split("/")
        if len(parts) > 4 and parts[1] == "file" and parts[2] == "d":
            if _DRIVE_ID.match(parts[3]):
                return f"https://drive.google.com/file/d/{parts[3]}/preview"
        return None
    elif host in ("vimeo.com", "player.vimeo.com"):
        # Vimeo's paid tiers can lock playback to one domain, which is the
        # practical answer for paid course content.
        candidate = parsed.path.strip("/").split("/")[-1]
        if _VIMEO_ID.match(candidate):
            return f"https://player.vimeo.com/video/{candidate}"
        return None

    if _YOUTUBE_ID.match(video_id):
        # nocookie host so watching does not leave ad-tracking cookies behind.
        return f"https://www.youtube-nocookie.com/embed/{video_id}"
    return None
