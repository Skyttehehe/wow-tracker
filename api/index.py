"""Vercel entry point: every /api/* request lands here (see vercel.json) and is
handled by the same code as the self-hosted backend, with Redis for storage."""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import server  # noqa: E402

try:
    server.STORE = server.make_store()
    handler = server.Handler
except RuntimeError as exc:
    SETUP_ERROR = str(exc)

    class handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"error": SETUP_ERROR}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET
