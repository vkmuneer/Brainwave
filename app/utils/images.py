"""Reading an uploaded picture safely.

Thumbnails are stored in the database rather than on disk, so one backup of
brainwave.db carries them and a redeploy onto a fresh filesystem does not blank
every picture.
"""
ALLOWED = {"image/png", "image/jpeg", "image/webp"}
MAX_BYTES = 2 * 1024 * 1024


def read_image_upload(upload):
    """Returns (data, mimetype, error).

    Only real raster types are accepted: an SVG is a document that can carry
    script, and the PDF engine cannot draw one either.
    """
    if not upload or not upload.filename:
        return None, None, None

    if upload.mimetype not in ALLOWED:
        return None, None, "The picture must be a PNG, JPG or WebP image."

    data = upload.read()
    if not data:
        return None, None, "That file appears to be empty."
    if len(data) > MAX_BYTES:
        return None, None, "The picture must be smaller than 2 MB."

    # Check the file really is what its type claims, rather than trusting the
    # browser-supplied content type.
    signatures = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF")
    if not data.startswith(signatures):
        return None, None, "That file is not a valid image."

    return data, upload.mimetype, None
