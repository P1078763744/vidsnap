#!/usr/bin/env python3
"""
VidSnap Backend Server
──────────────────────
Install requirements:  pip install flask flask-cors yt-dlp
Also needs ffmpeg:     https://ffmpeg.org/download.html  (add to PATH)
Run:                   python server.py
Frontend:              open index.html in your browser
"""

import os
import uuid
import subprocess
import shutil
import tempfile

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp

# ─── APP SETUP ───────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, expose_headers=["Content-Disposition", "X-Filename", "X-Title"])

TEMP_DIR = os.path.join(tempfile.gettempdir(), "vidsnap_downloads")
os.makedirs(TEMP_DIR, exist_ok=True)

CHUNK_SIZE = 65_536  # 64 KB read chunks


# ─── HELPERS ─────────────────────────────────────────────────────────────────
def has_ffmpeg():
    return shutil.which("ffmpeg") is not None


def safe_name(title: str, max_len: int = 60) -> str:
    """Sanitize a video title for use as a filename."""
    return "".join(c for c in title if c.isalnum() or c in " -_()").strip()[:max_len]


def find_output_file(prefix: str) -> str | None:
    """Find the first file in TEMP_DIR that starts with prefix."""
    for fname in os.listdir(TEMP_DIR):
        if fname.startswith(prefix):
            return os.path.join(TEMP_DIR, fname)
    return None


def stream_file(path: str, download_name: str, mime: str, title: str = ""):
    """Stream a file to the client then delete it."""
    size = os.path.getsize(path)

    def generate():
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                yield chunk
        try:
            os.remove(path)
        except OSError:
            pass

    headers = {
        "Content-Disposition": f'attachment; filename="{download_name}"',
        "Content-Type": mime,
        "Content-Length": str(size),
        "X-Filename": download_name,
    }
    if title:
        headers["X-Title"] = title[:100]

    return Response(stream_with_context(generate()), headers=headers)


def cleanup_prefix(prefix: str):
    """Delete all temp files that start with prefix."""
    for fname in os.listdir(TEMP_DIR):
        if fname.startswith(prefix):
            try:
                os.remove(os.path.join(TEMP_DIR, fname))
            except OSError:
                pass


# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.route("/api/ping")
def ping():
    """Health-check — frontend uses this to detect the backend."""
    try:
        import yt_dlp.version as v
        ytdlp_ver = v.__version__
    except Exception:
        ytdlp_ver = "unknown"

    return jsonify({
        "status": "ok",
        "ffmpeg": has_ffmpeg(),
        "yt_dlp": ytdlp_ver,
    })


@app.route("/api/info")
def get_info():
    """
    GET /api/info?url=<video_url>
    Returns title, duration, thumbnail, uploader, and available quality list.
    """
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "url parameter is required"}), 400

    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # Build unique quality list
        seen_heights = set()
        qualities = []
        for f in info.get("formats", []):
            h = f.get("height")
            if not h:
                continue
            if h not in seen_heights:
                seen_heights.add(h)
                qualities.append({
                    "label": f"{h}p",
                    "height": h,
                    "fps": f.get("fps"),
                    "filesize": f.get("filesize"),
                })
        qualities.sort(key=lambda x: x["height"], reverse=True)

        # Also expose audio-only option
        qualities.append({"label": "Audio only", "height": 0, "fps": None, "filesize": None})

        return jsonify({
            "title":      info.get("title", "Untitled"),
            "duration":   info.get("duration", 0),
            "thumbnail":  info.get("thumbnail"),
            "uploader":   info.get("uploader", ""),
            "view_count": info.get("view_count"),
            "qualities":  qualities,
        })

    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Extraction failed: {e}"}), 500


@app.route("/api/download")
def download_video():
    """
    GET /api/download?url=<url>&quality=720&format=mp4
    Streams the downloaded video file.
    """
    url     = request.args.get("url", "").strip()
    quality = request.args.get("quality", "720").replace("p", "")
    fmt     = request.args.get("format", "mp4").lower().strip(".")
    audio   = request.args.get("audio_only", "false").lower() == "true"

    if not url:
        return jsonify({"error": "url parameter is required"}), 400

    task = str(uuid.uuid4())[:10]
    out_tmpl = os.path.join(TEMP_DIR, f"{task}.%(ext)s")

    if audio or quality == "0":
        fmt_str = "bestaudio/best"
        post = [{"key": "FFmpegExtractAudio",
                 "preferredcodec": fmt if fmt in ("mp3", "m4a", "wav", "opus", "flac") else "mp3"}]
        merge_fmt = None
    else:
        fmt_str = (
            f"bestvideo[height<={quality}][ext={fmt}]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={quality}]+bestaudio/"
            f"best[height<={quality}]/best"
        )
        post = []
        merge_fmt = fmt

    ydl_opts = {
        "format":               fmt_str,
        "outtmpl":              out_tmpl,
        "merge_output_format":  merge_fmt,
        "postprocessors":       post,
        "quiet":                True,
        "no_warnings":          True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        title = info.get("title", "video")
    except yt_dlp.utils.DownloadError as e:
        cleanup_prefix(task)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        cleanup_prefix(task)
        return jsonify({"error": str(e)}), 500

    out_path = find_output_file(task)
    if not out_path:
        return jsonify({"error": "Download finished but output file not found"}), 500

    ext = os.path.splitext(out_path)[1].lstrip(".")
    is_audio = ext in ("mp3", "m4a", "wav", "opus", "flac")
    mime = f"{'audio' if is_audio else 'video'}/{ext}"
    dl_name = f"{safe_name(title)}.{ext}"

    return stream_file(out_path, dl_name, mime, title)


@app.route("/api/trim-url")
def trim_from_url():
    """
    GET /api/trim-url?url=<url>&start=<sec>&end=<sec>&quality=720&format=mp4
    Downloads only the specified segment using yt-dlp's download_ranges + ffmpeg.
    """
    url     = request.args.get("url", "").strip()
    quality = request.args.get("quality", "720").replace("p", "")
    fmt     = request.args.get("format", "mp4").lower().strip(".")
    start   = request.args.get("start", type=float, default=0.0)
    end     = request.args.get("end",   type=float)

    if not url:
        return jsonify({"error": "url parameter is required"}), 400
    if end is None or end <= start:
        return jsonify({"error": "end must be a number greater than start"}), 400

    task = str(uuid.uuid4())[:10]
    out_tmpl = os.path.join(TEMP_DIR, f"{task}.%(ext)s")

    ydl_opts = {
        "format": (
            f"bestvideo[height<={quality}]+bestaudio/"
            f"best[height<={quality}]/best"
        ),
        "outtmpl":              out_tmpl,
        "merge_output_format":  fmt,
        "download_ranges":      yt_dlp.utils.download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
        "quiet":                True,
        "no_warnings":          True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        title = info.get("title", "video")
    except yt_dlp.utils.DownloadError as e:
        cleanup_prefix(task)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        cleanup_prefix(task)
        return jsonify({"error": str(e)}), 500

    out_path = find_output_file(task)
    if not out_path:
        return jsonify({"error": "Trim finished but output file not found"}), 500

    ext = os.path.splitext(out_path)[1].lstrip(".")
    dl_name = f"{safe_name(title)}-{int(start)}s-{int(end)}s.{ext}"

    return stream_file(out_path, dl_name, f"video/{ext}", title)


@app.route("/api/trim-file", methods=["POST"])
def trim_file():
    """
    POST /api/trim-file  (multipart/form-data)
    Fields: file (video), start (float), end (float), format (mp4|webm|mkv)
    Uses ffmpeg to cut the video server-side — much higher quality than browser trim.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    if not has_ffmpeg():
        return jsonify({
            "error": "ffmpeg is not installed. Download from https://ffmpeg.org/download.html"
        }), 500

    start = float(request.form.get("start", 0))
    end   = float(request.form.get("end",   0))
    fmt   = request.form.get("format", "mp4").lower().strip(".")
    fast  = request.form.get("fast", "true").lower() == "true"

    if end <= start:
        return jsonify({"error": "end must be greater than start"}), 400

    task    = str(uuid.uuid4())[:10]
    f       = request.files["file"]
    ext_in  = os.path.splitext(f.filename)[1] or ".mp4"
    src     = os.path.join(TEMP_DIR, f"{task}_src{ext_in}")
    out     = os.path.join(TEMP_DIR, f"{task}_out.{fmt}")
    dur     = end - start

    try:
        f.save(src)

        if fast:
            # Stream copy — instant, no quality loss, ±1 keyframe accuracy
            cmd = [
                "ffmpeg", "-y",
                "-i", src,
                "-ss", str(start),
                "-t",  str(dur),
                "-c", "copy",
                out,
            ]
        else:
            # Re-encode — frame-accurate, full quality, slower
            codec_v = "libx264" if fmt in ("mp4", "mkv") else "libvpx-vp9"
            codec_a = "aac"     if fmt in ("mp4", "mkv") else "libopus"
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", src,
                "-t",  str(dur),
                "-c:v", codec_v,
                "-c:a", codec_a,
                "-preset", "fast",
                "-crf", "23",
                out,
            ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg error:\n{result.stderr[-800:]}")

    except subprocess.TimeoutExpired:
        cleanup_prefix(task)
        return jsonify({"error": "Processing timed out (file too large)"}), 500
    except Exception as e:
        cleanup_prefix(task)
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            os.remove(src)
        except OSError:
            pass

    if not os.path.exists(out):
        return jsonify({"error": "Output file missing after ffmpeg"}), 500

    dl_name = f"vidsnap-trim-{int(start)}s-{int(end)}s.{fmt}"
    return stream_file(out, dl_name, f"video/{fmt}")


# ─── RUN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  🎬  VidSnap Backend")
    print("=" * 50)
    print(f"  📁  Temp dir : {TEMP_DIR}")
    print(f"  ✅  ffmpeg   : {'found' if has_ffmpeg() else '❌ NOT found — install ffmpeg!'}")
    try:
        import yt_dlp.version as _v
        print(f"  ✅  yt-dlp   : {_v.__version__}")
    except Exception:
        print("  ❌  yt-dlp not installed")
    print()
    print("  🚀  http://localhost:5000")
    print("  🌐  Open index.html in your browser")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
