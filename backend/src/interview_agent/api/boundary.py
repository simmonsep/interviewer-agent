"""Small ASGI request boundary: exact Origin and bounded API bodies, no raw error echo."""

from starlette.responses import JSONResponse
import re


class ApiBoundary:
    def __init__(self, app, origin: str):
        self.app, self.origin = app, origin

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope["headers"]}
        async def no_store(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message["headers"], (b"cache-control", b"no-store")]}
            await send(message)
        if scope["method"] in ("POST", "PATCH", "PUT", "DELETE"):
            if headers.get(b"origin") != self.origin.encode():
                return await JSONResponse({"code": "origin_not_allowed"}, 403)(scope, receive, no_store)
            chunks = bytearray()
            # Main.java is bounded to 8KiB after JSON decoding. Escaped control characters
            # can consume six wire bytes per source byte; keep all older routes at 16KiB.
            large_source = re.fullmatch(r'/api/career-goals/[0-9a-fA-F-]{36}/(?:written/[0-9a-fA-F-]{36}/submissions|coding/[0-9a-fA-F-]{36}/edits)', scope['path'])
            file_preview = re.fullmatch(r'/api/career-goals/[0-9a-fA-F-]{36}/materials/file-previews',scope['path'])
            # F07Z-117: a 20,000-character material is ~60 KiB of UTF-8 before JSON escaping;
            # 128 KiB keeps the DoS ceiling without rejecting detailed resumes.
            materials_source = re.fullmatch(r'/api/career-goals/[0-9a-fA-F-]{36}/materials/sources',scope['path'])
            body_limit = (2097152 if file_preview else 1048576 if scope['path']=='/api/question-banks/imports'
                          else 131072 if materials_source else 65536 if large_source else 16384)
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunks.extend(message.get("body", b""))
                if len(chunks) > body_limit:
                    return await JSONResponse({"code": "request_too_large"}, 413)(scope, receive, no_store)
                if not message.get("more_body", False):
                    break
            consumed = False
            async def replay():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": bytes(chunks), "more_body": False}
                return await receive()
            await self.app(scope, replay, no_store)
        else:
            await self.app(scope, receive, no_store)
