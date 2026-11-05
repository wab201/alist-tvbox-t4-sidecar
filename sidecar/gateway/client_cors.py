"""Cross-origin playback APIs, without exposing browser access to management writes."""
from starlette.middleware.cors import CORSMiddleware


class ClientCORSMiddleware:
    def __init__(self, app):
        self.app = app
        self.cors = CORSMiddleware(
            app, allow_origins=["*"], allow_methods=["GET", "POST", "HEAD", "OPTIONS"],
            allow_headers=["*"], expose_headers=[
                "X-T4-Trace-ID", "Accept-Ranges", "Content-Range", "Content-Length",
                "Link",
            ],
        )

    @staticmethod
    def is_client_path(path: str) -> bool:
        parts = path.strip("/").split("/")
        if parts[:1] != ["t4"]:
            return False
        parts = parts[1:]
        if parts[:1] == ["profiles"] and len(parts) >= 3:
            parts = parts[2:]
        if parts == ["config.json"]:
            return True
        if len(parts) == 2 and parts[0] == "api":
            return parts[1] in {"home", "category", "search", "detail", "play", "action"}
        if parts[:1] == ["fm"]:
            return len(parts) == 3 or len(parts) == 5 and parts[3] == "api"
        if len(parts) == 3 and parts[0] == "acceleration":
            return parts[2] in {"component.json", "client.jar", "ready", "disable"}
        if len(parts) == 4 and parts[0] == "acceleration":
            return parts[2] == "plans"
        return len(parts) == 2 and parts[0] in {"media", "images", "proxy"}

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        root = scope.get("root_path", "").rstrip("/")
        if root and path.startswith(root + "/"):
            path = path[len(root):]
        target = self.cors if scope["type"] == "http" and self.is_client_path(path) else self.app
        await target(scope, receive, send)
