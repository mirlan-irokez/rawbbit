from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

SERVICE_DIR = Path(__file__).resolve().parents[1]
SOURCE_DIR = SERVICE_DIR / "src"
ALLOWED_ORIGIN = "https://allowed.example"

_ASGI_PROBE = r"""
import asyncio
import json

from fastapi import Response
from starlette.middleware.cors import CORSMiddleware

from collector_api.main import app


allowed_origin = "https://allowed.example"
registered = [middleware for middleware in app.user_middleware if middleware.cls is CORSMiddleware]
middleware_config = [
    {"class": middleware.cls.__name__, "options": middleware.kwargs}
    for middleware in registered
]


async def test_response(status_code: int) -> Response:
    return Response(
        content=f"test-response-{status_code}",
        status_code=status_code,
        headers={"Retry-After": "30", "X-Test-Header": "unchanged"},
    )


app.add_api_route(
    "/test/{status_code}",
    test_response,
    methods=["POST", "OPTIONS"],
    include_in_schema=False,
)


async def request(method: str, path: str, headers: list[tuple[str, str]] | None = None) -> dict:
    response_status = None
    response_headers: dict[str, str] = {}
    response_body = bytearray()

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        nonlocal response_status
        if message["type"] == "http.response.start":
            response_status = message["status"]
            response_headers.update(
                {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in message.get("headers", [])
                }
            )
        elif message["type"] == "http.response.body":
            response_body.extend(message.get("body", b""))

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (key.encode("latin-1"), value.encode("latin-1"))
                for key, value in headers or []
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "state": {},
            "extensions": {},
        },
        receive,
        send,
    )
    return {
        "status": response_status,
        "headers": response_headers,
        "body": response_body.decode("utf-8"),
    }


async def main() -> None:
    results = {
        "middleware": middleware_config,
        "allowed_429": await request("POST", "/test/429", [("origin", allowed_origin)]),
        "allowed_503": await request("POST", "/test/503", [("origin", allowed_origin)]),
        "disallowed_503": await request("POST", "/test/503", [("origin", "https://evil.example")]),
        "no_origin_429": await request("POST", "/test/429"),
        "preflight": await request(
            "OPTIONS",
            "/test/418",
            [
                ("origin", allowed_origin),
                ("access-control-request-method", "POST"),
                ("access-control-request-headers", "content-type,x-api-key"),
            ],
        ),
    }
    print(json.dumps(results))


asyncio.run(main())
"""


@functools.lru_cache(maxsize=2)
def _run_asgi_probe(cors_enabled: bool) -> dict[str, Any]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(SOURCE_DIR),
            "CORS_ALLOW_ORIGINS": ALLOWED_ORIGIN if cors_enabled else "",
            "CORS_ALLOW_METHODS": "POST,OPTIONS",
            "CORS_ALLOW_HEADERS": "Content-Type,X-API-Key",
            "CORS_ALLOW_CREDENTIALS": "false",
            "CORS_MAX_AGE_SECONDS": "600",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", _ASGI_PROBE],
        cwd=SERVICE_DIR,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "isolated ASGI probe failed\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return json.loads(completed.stdout)


def _tokens(value: str | None) -> set[str]:
    return {
        token.strip().lower() for token in (value or "").split(",") if token.strip()
    }


class CORSMiddlewareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.enabled = _run_asgi_probe(True)
        cls.disabled = _run_asgi_probe(False)

    def test_registered_middleware_keeps_existing_options_and_only_exposes_retry_after(
        self,
    ) -> None:
        self.assertEqual(
            self.enabled["middleware"],
            [
                {
                    "class": "CORSMiddleware",
                    "options": {
                        "allow_origins": [ALLOWED_ORIGIN],
                        "allow_credentials": False,
                        "allow_methods": ["POST", "OPTIONS"],
                        "allow_headers": ["Content-Type", "X-API-Key"],
                        "max_age": 600,
                        "expose_headers": ["Retry-After"],
                    },
                }
            ],
        )

    def test_allowed_429_and_503_preserve_response_and_expose_only_retry_after(
        self,
    ) -> None:
        for key, status_code in (("allowed_429", 429), ("allowed_503", 503)):
            with self.subTest(status=status_code):
                response = self.enabled[key]
                headers = response["headers"]
                self.assertEqual(response["status"], status_code)
                self.assertEqual(response["body"], f"test-response-{status_code}")
                self.assertEqual(headers["retry-after"], "30")
                self.assertEqual(headers["x-test-header"], "unchanged")
                self.assertEqual(headers["access-control-allow-origin"], ALLOWED_ORIGIN)
                self.assertEqual(
                    _tokens(headers.get("access-control-expose-headers")),
                    {"retry-after"},
                )

    def test_disallowed_origin_does_not_receive_allow_origin(self) -> None:
        response = self.enabled["disallowed_503"]
        headers = response["headers"]
        self.assertEqual(response["status"], 503)
        self.assertEqual(response["body"], "test-response-503")
        self.assertEqual(headers["retry-after"], "30")
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertEqual(
            _tokens(headers.get("access-control-expose-headers")), {"retry-after"}
        )

    def test_no_origin_response_has_no_cors_access_headers_and_keeps_retry_response(
        self,
    ) -> None:
        response = self.enabled["no_origin_429"]
        headers = response["headers"]
        without_cors = self.disabled["no_origin_429"]
        self.assertEqual(response["status"], 429)
        self.assertEqual(response["body"], "test-response-429")
        self.assertEqual(response["status"], without_cors["status"])
        self.assertEqual(response["body"], without_cors["body"])
        self.assertEqual(headers["retry-after"], "30")
        self.assertEqual(headers["x-test-header"], "unchanged")
        for name, value in without_cors["headers"].items():
            self.assertEqual(headers[name], value)
        self.assertEqual(set(headers) - set(without_cors["headers"]), {"vary"})
        self.assertEqual(_tokens(headers.get("vary")), {"origin"})
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertNotIn("access-control-expose-headers", headers)

    def test_preflight_response_remains_unchanged(self) -> None:
        response = self.enabled["preflight"]
        headers = response["headers"]
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["body"], "OK")
        self.assertEqual(headers["access-control-allow-origin"], ALLOWED_ORIGIN)
        self.assertEqual(
            _tokens(headers.get("access-control-allow-methods")), {"post", "options"}
        )
        self.assertEqual(
            _tokens(headers.get("access-control-allow-headers")),
            {
                "accept",
                "accept-language",
                "content-language",
                "content-type",
                "x-api-key",
            },
        )
        self.assertEqual(headers["access-control-max-age"], "600")
        self.assertNotIn("retry-after", headers)
        self.assertNotIn("access-control-expose-headers", headers)

    def test_cors_disabled_mode_does_not_intercept_preflight(self) -> None:
        self.assertEqual(self.disabled["middleware"], [])
        response = self.disabled["preflight"]
        headers = response["headers"]
        self.assertEqual(response["status"], 418)
        self.assertEqual(response["body"], "test-response-418")
        self.assertEqual(headers["retry-after"], "30")
        self.assertEqual(headers["x-test-header"], "unchanged")
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertNotIn("access-control-expose-headers", headers)


if __name__ == "__main__":
    unittest.main()
