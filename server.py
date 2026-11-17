import http.server
import socket
import socketserver
import os
import sys
import urllib.parse
import io
from PIL import Image

PORT = 8088
DASHBOARD_DIR = r"D:\OpenCode\Projects\Muramasa_HD\dashboard"
ORIG_DIR = r"D:\OpenCode\Projects\Muramasa_Art"
UPSCALED_DIR = r"D:\OpenCode\Projects\Muramasa_HD\upscaled_2k"
CACHE_DIR = os.path.join(DASHBOARD_DIR, "cache")

os.makedirs(os.path.join(CACHE_DIR, "orig"), exist_ok=True)
os.makedirs(os.path.join(CACHE_DIR, "2k"), exist_ok=True)

class CompressedHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DASHBOARD_DIR, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # Endpoint: compressed image comparison
        if parsed.path in ("/api/img/orig", "/api/img/2k"):
            params = urllib.parse.parse_qs(parsed.query)
            rel_path = params.get("path", [""])[0]
            if not rel_path:
                self.send_error(400, "Missing path query parameter")
                return

            kind = "orig" if parsed.path == "/api/img/orig" else "2k"
            self.serve_compressed(kind, rel_path)
            return

        # Prevent stale polling of progress.json
        if parsed.path == "/progress.json":
            fpath = os.path.join(DASHBOARD_DIR, "progress.json")
            if not os.path.isfile(fpath):
                self.send_error(404, "progress.json not found")
                return
            with open(fpath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return

        # HTTP Range support for MP4 / WebM video scrubbing
        if parsed.path.lower().endswith((".mp4", ".webm")):
            local_path = self.translate_path(self.path)
            if os.path.isfile(local_path):
                self.serve_video_range(local_path)
                return

        super().do_GET()

    def serve_video_range(self, file_path):
        file_size = os.path.getsize(file_path)
        ctype = "video/webm" if file_path.lower().endswith(".webm") else "video/mp4"
        range_hdr = self.headers.get("Range")
        start, end = 0, file_size - 1
        status = 200

        if range_hdr and range_hdr.startswith("bytes="):
            spec = range_hdr[6:].split(",")[0].strip()
            parts = spec.split("-")
            if parts[0]:
                start = int(parts[0])
                end = int(parts[1]) if (len(parts) > 1 and parts[1]) else (file_size - 1)
            elif len(parts) > 1 and parts[1]:
                suffix = int(parts[1])
                start = max(0, file_size - suffix)
                end = file_size - 1
            if start >= file_size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.end_headers()
                return
            end = min(end, file_size - 1)
            status = 206

        length = (end - start) + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        try:
            with open(file_path, "rb") as f:
                f.seek(start)
                rem = length
                while rem > 0:
                    chunk = f.read(min(65536, rem))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    rem -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def serve_compressed(self, kind, rel_path):
        # Resolve source file
        is_video_frame = rel_path.startswith("videos/")
        if is_video_frame:
            sub_png = os.path.splitext(rel_path[len("videos/"):])[0].replace("/", os.sep) + ".png"
            src = os.path.join(DASHBOARD_DIR, "video_frames", kind, sub_png)
            quality = 85 if kind == "orig" else 80
        elif kind == "orig":
            src = os.path.join(ORIG_DIR, rel_path)
            quality = 85
        else:
            src = os.path.join(UPSCALED_DIR, rel_path)
            quality = 80

        if not os.path.exists(src):
            # Check fallback to thumbnail if full resolution not yet available
            thumb_name = os.path.basename(rel_path)
            thumb_src = os.path.join(DASHBOARD_DIR, "thumbs", thumb_name)
            if os.path.exists(thumb_src):
                src = thumb_src
            else:
                self.send_error(404, f"File not found: {rel_path}")
                return

        # Flatten path for cache filename
        safe_rel = rel_path.replace("/", "_").replace("\\", "_")
        cache_file = os.path.join(CACHE_DIR, kind, safe_rel + ".webp")

        if (
            os.path.exists(cache_file)
            and os.path.getsize(cache_file) > 0
            and os.path.getmtime(cache_file) >= os.path.getmtime(src)
        ):
            try:
                with open(cache_file, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/webp")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception:
                pass

        try:
            with Image.open(src) as im:
                buf = io.BytesIO()
                # High-efficiency WebP compression
                im.save(buf, format="WEBP", quality=quality, method=4)
                data = buf.getvalue()

            # Cache to disk for instant subsequent loads
            try:
                with open(cache_file, "wb") as f:
                    f.write(data)
            except Exception:
                pass

            self.send_response(200)
            self.send_header("Content-Type", "image/webp")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(500, f"Error compressing image: {e}")

    def log_message(self, format, *args):
        pass # suppress request noise

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


if __name__ == "__main__":
    os.chdir(DASHBOARD_DIR)
    socketserver.TCPServer.allow_reuse_address = True
    server = ThreadedHTTPServer(("::", PORT), CompressedHandler)
    print(f"[Dashboard Server] Serving compressed WebP stream on http://localhost:{PORT}")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
