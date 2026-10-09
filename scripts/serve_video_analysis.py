#!/usr/bin/env python3
"""Read-only loopback preview of one video directory, with single byte ranges.

GET supports closed, open-ended and suffix byte ranges. HEAD returns full-file
metadata with no body and ignores Range, as RFC 9110 specifies. Multiple ranges,
unknown range units and ranges on empty files are ignored (normal 200 response).
Invalid or unsatisfiable single byte ranges return 416 with bytes */file-size.
No authentication, credentials, media changes or public network binding.
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import sys
import threading
import time
from urllib.parse import quote, unquote, urlsplit, urlunsplit
import uuid

sys.dont_write_bytecode = True
from tool_paths import data_root, external_path

RFC_REFERENCE = "https://www.rfc-editor.org/rfc/rfc9110.html#section-14"


class RangeNotSatisfiable(ValueError):
    pass


def parse_byte_range(value, size):
    """Return inclusive (start,end), or None when Range should be ignored.

    Only one bytes range is served. No multipart body or fictitious partial
    response is produced when a client asks for multiple ranges.
    """
    if value is None or size == 0:
        return None
    unit, separator, spec = value.strip().partition("=")
    if not separator:
        raise RangeNotSatisfiable("Invalid Range header")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    matched = re.fullmatch(r"([0-9]*)-([0-9]*)", spec.strip())
    if matched is None or not any(matched.groups()):
        raise RangeNotSatisfiable("Invalid single byte range")
    first, last = matched.groups()
    try:
        if first:
            start = int(first)
            end = int(last) if last else size - 1
            if start >= size or end < start:
                raise RangeNotSatisfiable("Byte range outside file")
            return start, min(end, size - 1)
        length = int(last)
        if length <= 0:
            raise RangeNotSatisfiable("Suffix length must be positive")
        return max(0, size - length), size - 1
    except ValueError as error:
        if isinstance(error, RangeNotSatisfiable):
            raise
        raise RangeNotSatisfiable("Byte range integer cannot be parsed") from None


class VideoPreviewHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "YuncongLocalPreview/1"
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".mp4": "video/mp4", ".m4a": "audio/mp4", ".vtt": "text/vtt; charset=utf-8",
        ".srt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8",
        ".json": "application/json; charset=utf-8", ".html": "text/html; charset=utf-8",
    }

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *_):
        # Do not log request headers, URLs with query strings, or credentials.
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def confined_path(self):
        root = Path(self.directory).resolve()
        request_path = unquote(urlsplit(self.path).path)
        if "\0" in request_path or ".." in request_path.split("/"):
            raise PermissionError("Path outside the video directory")
        target = (root / request_path.lstrip("/")).resolve()
        if not target.is_relative_to(root):
            raise PermissionError("Symlink outside the video directory")
        return target

    def send_head(self):
        self._byte_range = None
        try:
            path = self.confined_path()
        except (OSError, ValueError):
            self.send_error(403, "Path outside the video directory")
            return None
        if path.is_dir():
            parts = urlsplit(self.path)
            if not parts.path.endswith("/"):
                self.send_response(301)
                self.send_header("Location", urlunsplit(parts._replace(path=parts.path + "/")))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            for name in ("index.html", "index.htm"):
                candidate = path / name
                if candidate.is_file():
                    candidate = candidate.resolve()
                    if not candidate.is_relative_to(Path(self.directory).resolve()):
                        self.send_error(403, "Index outside the video directory")
                        return None
                    path = candidate
                    break
            else:
                return self.list_directory(str(path))
        try:
            file = path.open("rb")
        except (OSError, ValueError):
            self.send_error(404, "File not found")
            return None
        try:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode):
                file.close()
                self.send_error(404, "Not a regular file")
                return None
            size = info.st_size
            modified = self.date_time_string(info.st_mtime)
            # GET is the only Range-enabled method; HEAD deliberately ignores it.
            value = self.headers.get("Range") if self.command == "GET" else None
            if len(self.headers.get_all("Range", [])) > 1:
                value = None  # Multiple field instances are not a single range.
            if_range = self.headers.get("If-Range")
            if value is not None and if_range is not None:
                # Only the exact Last-Modified HTTP-date is available as a validator.
                # ETags or stale dates therefore select the full representation.
                # A modification less than 60 seconds before Date is not a
                # strong HTTP-date validator (RFC 9110 section 8.8.2.2).
                if if_range != modified or time.time() - info.st_mtime < 60:
                    value = None
            try:
                bounds = parse_byte_range(value, size)
            except RangeNotSatisfiable:
                file.close()
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return None
            self._byte_range = bounds
            self.send_response(206 if bounds is not None else 200)
            self.send_header("Content-Type", self.guess_type(str(path)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", modified)
            self.send_header("Content-Length", str(bounds[1] - bounds[0] + 1 if bounds is not None else size))
            if bounds is not None:
                self.send_header("Content-Range", f"bytes {bounds[0]}-{bounds[1]}/{size}")
            self.end_headers()
            return file
        except BaseException:
            file.close()
            raise

    def copyfile(self, source, outputfile):
        bounds = getattr(self, "_byte_range", None)
        try:
            if bounds is None:
                super().copyfile(source, outputfile)
                return
            first, last = bounds
            source.seek(first)
            remaining = last - first + 1
            while remaining:
                block = source.read(min(256 * 1024, remaining))
                if not block:
                    self.close_connection = True
                    return
                outputfile.write(block)
                remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            # Browsers routinely cancel an old download when seeking elsewhere.
            self.close_connection = True


def create_server(video_dir, port=0):
    root = external_path(video_dir).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("video-dir must be an existing local directory")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    handler = functools.partial(VideoPreviewHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def write_status(path, record):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-dir", type=Path, required=True, help="Serve this local directory and its descendants")
    parser.add_argument("--port", type=int, default=0, help="0 chooses a free loopback port")
    parser.add_argument("--timeout", type=float, default=86400, help="Stop after this many seconds")
    parser.add_argument("--status-file", type=Path, help="New status file outside the code/video directories; default external runtime")
    args = parser.parse_args(argv)
    server = None
    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        root = external_path(args.video_dir).resolve(strict=True)
        status = external_path(args.status_file or data_root() / "runtime" / "video_analysis" /
                               f"preview_{os.getpid()}_{uuid.uuid4().hex[:8]}.json")
        if status.is_relative_to(root):
            raise ValueError("status-file must be outside the served video directory")
        status.parent.mkdir(parents=True, exist_ok=True)
        if status.exists():
            raise FileExistsError("Status file already exists; choose a new status path")
        server = create_server(root, args.port)
        began = time.monotonic()
        started = dt.datetime.now(dt.timezone.utc)
        record = {"schema_version": 1, "state": "running", "pid": os.getpid(),
                  "host": "127.0.0.1", "port": server.server_port,
                  "base_url": f"http://127.0.0.1:{server.server_port}/", "video_dir": str(root),
                  "status_file": str(status), "started_at": started.isoformat(),
                  "expires_at": (started + dt.timedelta(seconds=args.timeout)).isoformat(),
                  "timeout_seconds": args.timeout, "read_only": True,
                  "range_support": "single GET bytes range; HEAD ignores Range; multipart/unknown units ignored",
                  "http_reference": RFC_REFERENCE}
        write_status(status, record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        reason = ["server_stopped"]

        def stop(label):
            reason[0] = label
            server.shutdown()

        timer = threading.Timer(args.timeout, stop, args=("timeout",))
        timer.daemon = True
        previous = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda number, _frame: threading.Thread(
                target=stop, args=(signal.Signals(number).name,), daemon=True).start())
        timer.start()
        try:
            server.serve_forever(poll_interval=.25)
        finally:
            timer.cancel()
            server.server_close()
            for number, handler in previous.items():
                signal.signal(number, handler)
            record.update(state="stopped", stop_reason=reason[0],
                          stopped_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                          elapsed_seconds=time.monotonic() - began)
            write_status(status, record)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        if server is not None:
            server.server_close()
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
