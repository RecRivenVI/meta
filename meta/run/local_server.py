import argparse
import json
import mimetypes
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from .service_routes import FML_LIBRARIES, OFFICIAL_FML_LIBS_URL


class LocalServiceServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, document_root: Path, port: int):
        super().__init__(("127.0.0.1", port), LocalServiceHandler)
        self.document_root = document_root.resolve()
        payload_map = self.document_root / "java-runtime-payloads.json"
        if payload_map.is_file():
            self.java_payload_routes = json.loads(
                payload_map.read_text(encoding="utf-8")
            )
        else:
            self.java_payload_routes = {}


class LocalServiceHandler(BaseHTTPRequestHandler):
    server: LocalServiceServer

    def _send_file(self, root: Path, relative: str):
        root = root.resolve()
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            self.send_error(404)
            return
        if not target.is_file():
            self.send_error(404)
            return

        data = target.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command == "GET":
            self.wfile.write(data)

    def _proxy(self, urls):
        for url in urls:
            try:
                request = Request(url, method=self.command)
                response = urlopen(request, timeout=30)
            except (HTTPError, URLError, OSError):
                continue

            self.send_response(response.status)
            content_type = response.headers.get("Content-Type")
            if content_type:
                self.send_header("Content-Type", content_type)
            content_length = response.headers.get("Content-Length")
            if content_length:
                self.send_header("Content-Length", content_length)
            self.end_headers()
            if self.command == "GET":
                shutil.copyfileobj(response, self.wfile)
            response.close()
            return
        self.send_error(502)

    def _handle_java_payload(self, token: str):
        if (
            not token
            or "/" in token
            or token not in self.server.java_payload_routes
        ):
            self.send_error(404)
            return
        route = self.server.java_payload_routes[token]
        self._proxy([route["bmclapi"], route["official"]])

    def _handle_fml(self, relative: str):
        filename = unquote(relative)
        if not filename or "/" in filename or filename not in FML_LIBRARIES:
            self.send_error(404)
            return

        _sha1, bmcl_url = FML_LIBRARIES[filename]
        urls = [OFFICIAL_FML_LIBS_URL + filename]
        if bmcl_url:
            urls.insert(0, bmcl_url)
        self._proxy(urls)

    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def _dispatch(self):
        path = unquote(urlsplit(self.path).path)
        if path.startswith("/v1/"):
            self._send_file(self.server.document_root / "v1", path[4:])
        elif path.startswith("/java-runtime/"):
            self._send_file(
                self.server.document_root / "java-runtime", path[len("/java-runtime/") :]
            )
        elif path.startswith("/java-runtime-payload/"):
            self._handle_java_payload(path[len("/java-runtime-payload/") :])
        elif path.startswith("/fmllibs/"):
            self._handle_fml(path[len("/fmllibs/") :])
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")


def main():
    parser = argparse.ArgumentParser(
        description="Serve /v1, /java-runtime and /fmllibs on loopback."
    )
    parser.add_argument("--document-root", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if not (1 <= args.port <= 65535):
        parser.error("port must be between 1 and 65535")
    if not (args.document_root / "v1").is_dir():
        parser.error(f"missing document root/v1: {args.document_root / 'v1'}")

    server = LocalServiceServer(args.document_root, args.port)
    print(f"LISTENING http://127.0.0.1:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
