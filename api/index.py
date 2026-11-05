"""Vercel entry point: every /api/* request lands here (see vercel.json) and is
handled by the same code as the self-hosted backend, with Redis for storage."""

import os
import sys
from http import HTTPStatus

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import server  # noqa: E402

try:
    server.STORE = server.make_store()
    SETUP_ERROR = None
except RuntimeError as exc:
    SETUP_ERROR = str(exc)


# Vercel finds the function by this top-level class name.
class handler(server.Handler):
    def do_GET(self):
        if SETUP_ERROR:
            return self.error(HTTPStatus.INTERNAL_SERVER_ERROR, SETUP_ERROR)
        return super().do_GET()

    def do_POST(self):
        if SETUP_ERROR:
            return self.error(HTTPStatus.INTERNAL_SERVER_ERROR, SETUP_ERROR)
        return super().do_POST()
