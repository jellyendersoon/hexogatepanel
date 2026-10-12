import atexit
import json
import os
import re
import subprocess
from html import escape
from pathlib import Path

from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.responses import FileResponse, HTMLResponse

from app import on_startup
from app.utils.brand import Brand, brand_for_headers
from config import dashboard_settings, runtime_settings, server_settings

base_dir = Path(__file__).parent
build_dir = base_dir / "build"
statics_dir = build_dir / "statics"
NO_CACHE_FILENAMES = {"index.html", "404.html", "sw.js", "manifest.webmanifest"}
HASHED_ASSET_RE = re.compile(r".+-[A-Za-z0-9_-]{6,}\.[A-Za-z0-9]+$")


BRANDED_FILENAMES = {"index.html", "404.html"}


def apply_brand(html: str, brand: Brand) -> str:
    """Give the dashboard shell a tenant's title and icon, and hand the brand to the app."""
    public = brand.public()
    html = re.sub(
        r"<title>.*?</title>", lambda _: f"<title>{escape(brand.name)}</title>", html, count=1, flags=re.DOTALL
    )
    # The web manifest and touch icon are Hexogate's; a branded host gets neither.
    html = re.sub(r"<link\b[^>]*\brel=\"(?:manifest|apple-touch-icon)\"[^>]*>\s*", "", html)
    icon_url = public["favicon"] or public["logo"]
    if icon_url:
        icon = f'<link rel="icon" href="{escape(icon_url)}" />'
        html = re.sub(r"<link\b[^>]*\brel=\"icon\"[^>]*>", lambda _: icon, html, count=1)
    payload = json.dumps(public).replace("<", "\\u003c")
    return html.replace("</head>", f"<script>window.__HEXO_BRAND__={payload}</script></head>", 1)


class DashboardStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        # Starlette serves 404.html itself, bypassing file_response; brand that shell too.
        is_404_shell = response.status_code == 404 and isinstance(response, FileResponse)
        if is_404_shell and brand_for_headers(Headers(scope=scope)):
            return self.file_response(response.path, None, scope, 404)
        return response

    def file_response(self, full_path, stat_result, scope, status_code=200):
        file_name = Path(full_path).name
        brand = brand_for_headers(Headers(scope=scope)) if file_name in BRANDED_FILENAMES else None
        if brand:
            response = HTMLResponse(apply_brand(Path(full_path).read_text(encoding="utf-8"), brand), status_code)
        else:
            response = super().file_response(full_path, stat_result, scope, status_code)

        if file_name in NO_CACHE_FILENAMES:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        elif HASHED_ASSET_RE.match(file_name):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers.setdefault("Cache-Control", "public, max-age=3600")

        return response


def build_api_interface():
    subprocess.Popen(
        ["bun", "run", "wait-port-gen-api"],
        env={**os.environ, "UVICORN_PORT": str(server_settings.port)},
        cwd=base_dir,
        stdout=subprocess.DEVNULL,
    )


def build():
    proc = subprocess.Popen(
        ["bun", "run", "build", "--outDir", build_dir, "--assetsDir", "statics"],
        env={**os.environ, "VITE_BASE_API": dashboard_settings.vite_base_api},
        cwd=base_dir,
    )
    proc.wait()
    with open(build_dir / "index.html", "r") as file:
        html = file.read()
    with open(build_dir / "404.html", "w") as file:
        file.write(html)


def run_dev():
    build_api_interface()
    proc = subprocess.Popen(
        ["bun", "run", "dev", "--base", os.path.join(dashboard_settings.path, "")],
        env={**os.environ, "VITE_BASE_API": dashboard_settings.vite_base_api, "DEBUG": "false"},
        cwd=base_dir,
    )

    atexit.register(proc.terminate)


def run_build(app):
    if runtime_settings.role.runs_panel and not build_dir.is_dir():
        build()

    app.mount(dashboard_settings.path, DashboardStaticFiles(directory=build_dir, html=True), name="dashboard")
    app.mount("/statics/", DashboardStaticFiles(directory=statics_dir, html=True), name="statics")


def setup_dashboard(app):
    @on_startup
    def run_dashboard():
        if runtime_settings.debug:
            run_dev()
        else:
            run_build(app)
