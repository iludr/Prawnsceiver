"""JPEG thumbnails for decoded pass images, generated on first request.

Full LRPT decodes are ~10 MB PNGs — the pass history page loading dozens of
them crawls, especially over the internet. The /thumbs/ endpoint serves a
small preview (height 400 px, JPEG) and caches it next to the original as
<name>.thumb.jpg; the history page shows the preview and opens the full
PNG only when it is clicked.
"""
import os
import tempfile

THUMB_SUFFIX = '.thumb.jpg'
THUMB_HEIGHT = 400  # px; image previews stay readable at this height


def thumb_path(png_path):
    """Cache location of a PNG's thumbnail."""
    if png_path.lower().endswith('.png'):
        return png_path[:-4] + THUMB_SUFFIX
    return png_path + THUMB_SUFFIX


def ensure_thumb(png_path):
    """Return (thumb_path, error) — generates the thumbnail if it is
    missing or older than the PNG.

    Concurrent callers may both generate; the write is atomic (temp file +
    rename), so the loser simply overwrites the winner with the same
    content. A stale thumbnail regenerates when the PNG is re-decoded.
    """
    tpath = thumb_path(png_path)
    try:
        if os.path.exists(tpath) and os.path.getmtime(tpath) >= os.path.getmtime(png_path):
            return tpath, None
        from PIL import Image
        resample = getattr(Image, 'Resampling', Image).LANCZOS
        with Image.open(png_path) as im:
            im = im.convert('RGB')
            if im.height > THUMB_HEIGHT:
                im = im.resize((max(1, round(im.width * THUMB_HEIGHT / im.height)),
                                THUMB_HEIGHT), resample)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(png_path) or '.',
                                       suffix='.thumbtmp')
            try:
                os.close(fd)
                im.save(tmp, 'JPEG', quality=80, optimize=True)
                os.replace(tmp, tpath)
            except Exception:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
        return tpath, None
    except ImportError:
        return None, 'Pillow not installed (pip install pillow)'
    except Exception as e:
        return None, f'thumbnail generation failed: {e}'
