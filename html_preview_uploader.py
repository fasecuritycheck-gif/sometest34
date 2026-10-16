"""Upload an HTML file to the existing Render preview API.

Drag an HTML file onto this script, run it without arguments to choose a file,
or use: python html_uploader.py "page.html" "SESSION_ID_OR_WAITING_PAGE_URL"
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


RENDER_BASE_URL = "https://studentpresidentvotingridgemont2027.onrender.com/"
MAX_FILE_SIZE = 10 * 1024 * 1024
DIRECT_UPLOAD_THRESHOLD = 500 * 1024
REQUEST_TIMEOUT = 60

# Preserve the existing server protocol: these are characters per chunk.
# JSON encoding adds bytes, so this is NOT a 500 KB HTTP request limit.
CHUNK_SIZE = 500 * 1024


class UploadHTTPError(RuntimeError):
    def __init__(self, status: int, details: str) -> None:
        self.status = status
        super().__init__(f"HTTP {status}: {details or 'No error details returned'}")


def encode_json(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def post_json(url: str, payload: dict) -> str:
    """Send one POST. Return text so empty success responses also work."""
    request = urllib.request.Request(
        url,
        data=encode_json(payload),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "HtmlUploader/2.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            details = exc.read(4096).decode("utf-8", errors="replace").strip()
        finally:
            exc.close()
        raise UploadHTTPError(exc.code, details) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not connect to Render: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("The request to Render timed out.") from exc


def choose_file() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1].strip('"')).expanduser().resolve()

    root = None
    try:
        from tkinter import Tk, filedialog

        root = Tk()
        root.withdraw()
        selected = filedialog.askopenfilename(
            title="Select preview HTML",
            filetypes=[("HTML files", "*.html *.htm")],
        )
    except Exception as exc:
        raise RuntimeError(
            'Could not open the file picker. Run: python html_uploader.py "page.html"'
        ) from exc
    finally:
        if root is not None:
            root.destroy()

    if not selected:
        raise ValueError("No HTML file selected")
    return Path(selected).expanduser().resolve()


def load_and_validate(path: Path) -> str:
    if path.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("Only .html and .htm files are accepted")
    if not path.is_file():
        raise ValueError(f"File does not exist: {path}")
    if path.stat().st_size > MAX_FILE_SIZE:
        raise ValueError("HTML file is larger than 10 MB")

    with path.open("rb") as file:
        raw = file.read(MAX_FILE_SIZE + 1)
    if len(raw) > MAX_FILE_SIZE:
        raise ValueError("HTML file is larger than 10 MB")

    # Remove an optional UTF-8 BOM, while preserving the file's line endings.
    html = raw.decode("utf-8-sig")
    if not html.strip():
        raise ValueError("The HTML file is empty")

    # Preserve the HTML and its JavaScript without modifying their contents.
    return html


def parse_session_id(value: str) -> str:
    value = value.strip().strip('"').strip()
    if not value:
        raise ValueError("Session ID cannot be empty")

    if "://" in value:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError("The waiting-page URL must use HTTP or HTTPS")
        values = urllib.parse.parse_qs(parsed.query).get("session", [])
        value = values[0].strip() if values else ""

    if not value:
        raise ValueError("The URL does not contain a session ID")
    return value


def session_api_base(session_id: str) -> str:
    safe_id = urllib.parse.quote(session_id, safe="")
    return f"{RENDER_BASE_URL.rstrip('/')}/api/session/{safe_id}"


def upload_direct(session_id: str, filename: str, html: str) -> None:
    print("Uploading HTML...")
    post_json(
        f"{session_api_base(session_id)}/html",
        {"filename": filename, "html": html},
    )
    print("Upload successful.")


def upload_chunked(session_id: str, filename: str, html: str) -> None:
    base_url = session_api_base(session_id)
    print("Initializing chunked upload...")

    try:
        response_text = post_json(
            f"{base_url}/upload/init",
            {
                "filename": filename,
                "total_size": len(html),
                "chunk_size": CHUNK_SIZE,
            },
        )
    except UploadHTTPError as exc:
        if exc.status == 404:
            print("Upload initialization returned 404; trying direct upload.")
            upload_direct(session_id, filename, html)
            return
        raise RuntimeError(f"Failed to initialize upload: {exc}") from exc

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Upload initialization returned invalid JSON.") from exc

    if not isinstance(result, dict) or not result.get("upload_id"):
        raise RuntimeError("The server did not return an upload_id.")
    upload_id = result["upload_id"]
    total_chunks = (len(html) + CHUNK_SIZE - 1) // CHUNK_SIZE

    for index, start in enumerate(range(0, len(html), CHUNK_SIZE), start=1):
        chunk = html[start:start + CHUNK_SIZE]
        try:
            post_json(
                f"{base_url}/upload/chunk",
                {
                    "upload_id": upload_id,
                    "chunk_num": index,
                    "total_chunks": total_chunks,
                    "chunk": chunk,
                },
            )
        except RuntimeError as exc:
            print()
            raise RuntimeError(f"Failed to upload chunk {index}: {exc}") from exc

        progress = index / total_chunks * 100
        print(
            f"\rUploading: {index}/{total_chunks} chunks ({progress:.0f}%)",
            end="",
            flush=True,
        )

    print()
    try:
        post_json(f"{base_url}/upload/complete", {"upload_id": upload_id})
    except RuntimeError as exc:
        raise RuntimeError(f"Failed to complete upload: {exc}") from exc
    print("Upload complete.")


def main() -> int:
    print("=" * 50)
    print("HTML Preview Uploader")
    print("=" * 50)

    try:
        path = choose_file()
        html = load_and_validate(path)
        file_size = len(html.encode("utf-8"))
        print(f"File loaded: {path.name} ({file_size:,} bytes)")

        if len(sys.argv) > 2:
            entered_session = sys.argv[2]
        else:
            print("\nCopy the session ID or the full waiting-page URL.")
            entered_session = input("Session ID or URL: ")
        session_id = parse_session_id(entered_session)

        print(f"\nUploading to session: {session_id}")
        if file_size > DIRECT_UPLOAD_THRESHOLD:
            upload_chunked(session_id, path.name, html)
        else:
            upload_direct(session_id, path.name, html)

        query = urllib.parse.urlencode({"session": session_id})
        preview_url = f"{RENDER_BASE_URL.rstrip('/')}/preview?{query}"
        print(f"\nPreview URL: {preview_url}")
        print("A waiting tab will load the HTML when it is ready.")
        print("If the tab already shows an older upload, refresh it to load this one.")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 1
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        print(f"\nError: {exc}")
        return 1
    finally:
        if sys.stdin.isatty():
            try:
                input("\nPress Enter to close...")
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
