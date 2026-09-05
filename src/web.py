"""Application shell.

A neutral container for the firm's analysis tools. It owns sign-in, the
navigation manifest and the static assets, and it mounts whichever tool routers
the registry declares available.

IT KNOWS NOTHING ABOUT ANY TOOL'S SUBJECT MATTER. There is no tax vocabulary in
this file and no import of the tax engine - the shell reaches a tool only
through the router path its registry entry names. That is what lets a second
tool arrive without this file changing, and what stops one tool's concerns
leaking into another's.

Run it:

    APP_PASSWORD=... python3 -m src.web            # or --host/--port
"""
from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from . import modules
from .session import SESSION_COOKIE, SESSION_HOURS, check_password, new_token, valid

STATIC = Path(__file__).resolve().parent / "static"
APP_NAME = os.environ.get("APP_NAME", "Workbench")

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)


# ---------------------------------------------------------------- sign-in
@app.post("/api/login")
def login(response: Response, password: str = Form(...)):
    if not check_password(password):
        raise HTTPException(status_code=401, detail="Incorrect password")
    token, expiry = new_token()
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict",
                        max_age=SESSION_HOURS * 3600)
    return {"ok": True, "expires": expiry}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/session")
def session(request: Request):
    return {"authenticated": valid(request.cookies.get(SESSION_COOKIE)),
            "app_name": APP_NAME}


# ---------------------------------------------------------------- navigation
@app.get("/api/tools")
def tools(request: Request):
    """The navigation manifest.

    Reserved tools are listed so they have a place in the sidebar. Listing one
    imports nothing and enables nothing.
    """
    if not valid(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status_code=401, detail="Sign in required")
    return {"app_name": APP_NAME, "tools": modules.manifest()}


# ---------------------------------------------------------------- static
@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


@app.get("/app.js")
def appjs():
    return Response((STATIC / "app.js").read_text(),
                    media_type="application/javascript")


@app.get("/app.css")
def appcss():
    return Response((STATIC / "app.css").read_text(), media_type="text/css")


# ---------------------------------------------------------------- tool mounting
def mount_tools(application: FastAPI = app) -> list[str]:
    """Import and mount each AVAILABLE tool's router at its declared prefix.

    A RESERVED tool is skipped entirely: no import is attempted, so a tool that
    does not exist yet cannot break the application that reserves space for it.
    """
    mounted = []
    for tool in modules.available():
        if not tool.router:
            continue
        module_path, _, attr = tool.router.partition(":")
        router = getattr(importlib.import_module(module_path), attr)
        application.include_router(router, prefix=tool.api_prefix)
        mounted.append(tool.id)
    return mounted


MOUNTED = mount_tools()


def main() -> None:
    import uvicorn
    from .session import password
    p = argparse.ArgumentParser(description=f"{APP_NAME} - analysis tools")
    p.add_argument("--host", default="127.0.0.1",
                   help="default 127.0.0.1 - bind wider only behind your own network")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    password()
    print(f"  {APP_NAME}: mounted tools -> {', '.join(MOUNTED) or 'none'}")
    for t in modules.REGISTRY:
        if t.status == modules.RESERVED:
            print(f"  reserved (not mounted) -> {t.id}: {t.label}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
