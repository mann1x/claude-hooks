"""The council dashboard's routes on the engine's HTTP server.

    GET /dashboard                              the page (one HTML file)
    GET /v1/dashboard/councils                  ?limit= &status= &q=
    GET /v1/dashboard/councils/{sid}            ?since= &full=1
    POST /v1/dashboard/index/backfill           {"scan": [paths], "max_depth": 5}

The page reads only these and the existing per-council SSE stream
(``/v1/consult/{sid}/events``) for a council that is running. Everything
is read-only except the backfill, which writes only the index. The
engine binds loopback. With ``[service] dashboard_host`` set it also
listens there, and :func:`install_lan_guard` gives a client that is not
on loopback the dashboard's read-only routes and nothing else.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import threading
from typing import Optional

log = logging.getLogger("consultants.server.dashboard_routes")


#: What a client that is not on loopback may reach once the engine also
#: listens on ``[service] dashboard_host``: the page, its read-only API,
#: a council's event stream, health. Never the control API (consult,
#: follow-up, inject, cancel, apply, backfill): a council can run a
#: shell, and the LAN does not get to start one.
LAN_ROUTES = (re.compile(r"^/$"),
              re.compile(r"^/dashboard/?$"),
              re.compile(r"^/v1/dashboard/councils(?:/[^/]+(?:/messages|"
                         r"/calls/\d+)?)?$"),
              re.compile(r"^/v1/consult/[^/]+/events$"),
              re.compile(r"^/v1/health$"))


def lan_allowed(method: str, path: str) -> bool:
    return method in ("GET", "HEAD") and any(r.match(path) for r in LAN_ROUTES)


def is_local_client(host: Optional[str]) -> bool:
    """Loopback, or not an IP at all (a unix socket, the test client)."""
    if not host:
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return True


def install_lan_guard(app) -> None:
    """Refuse every route but :data:`LAN_ROUTES` to a client that is not
    on loopback, while ``app.state.lan_guard`` is set (the engine sets it
    when it listens on a ``dashboard_host``)."""
    from fastapi.responses import JSONResponse

    @app.middleware("http")
    async def _lan_guard(request, call_next):
        if getattr(app.state, "lan_guard", False):
            client = request.client.host if request.client else None
            if not is_local_client(client) and not lan_allowed(
                    request.method, request.url.path):
                return JSONResponse(status_code=403, content={
                    "detail": "the consultants engine's API is loopback-"
                              "only; the LAN gets the read-only dashboard "
                              "(/dashboard)"})
        return await call_next(request)


def register_dashboard_routes(app) -> None:
    from fastapi import Body, HTTPException
    from fastapi.responses import HTMLResponse, RedirectResponse

    from consultants.server import council_index, dashboard
    from consultants.server.dashboard_page import PAGE

    install_lan_guard(app)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/dashboard", status_code=307)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_page() -> str:
        return PAGE

    @app.get("/v1/dashboard/councils")
    def dashboard_councils(limit: int = 200, status: Optional[str] = None,
                           q: Optional[str] = None) -> dict:
        return dashboard.council_list(app, limit=max(1, min(limit, 2000)),
                                      status=status or None, query=q or None)

    @app.get("/v1/dashboard/councils/{sid}")
    def dashboard_council(sid: str, since: int = 0, full: int = 0) -> dict:
        d = dashboard.council_detail(app, sid, since=since, full=bool(full))
        if d is None:
            raise HTTPException(status_code=404, detail=f"no council {sid}")
        return d

    @app.get("/v1/dashboard/councils/{sid}/messages")
    def dashboard_messages(sid: str) -> dict:
        d = dashboard.council_messages(app, sid)
        if d is None:
            raise HTTPException(status_code=404, detail=f"no council {sid}")
        return d

    @app.get("/v1/dashboard/councils/{sid}/calls/{event_id}")
    def dashboard_call(sid: str, event_id: int) -> dict:
        d = dashboard.call_detail(app, sid, event_id)
        if d is None:
            raise HTTPException(status_code=404,
                                detail=f"no call {event_id} in {sid}")
        return d

    @app.post("/v1/dashboard/index/backfill")
    def dashboard_backfill(body: dict = Body(default={})) -> dict:
        # (Annotations are strings here — ``from __future__`` — so a
        # locally imported ``Request`` would not resolve: FastAPI would
        # read it as a required query parameter.)
        scan = [p for p in (body.get("scan") or []) if isinstance(p, str)]
        depth = int(body.get("max_depth") or 5)
        roots = council_index.find_stores(scan, max_depth=depth) if scan else []
        return {"added": council_index.backfill(roots), "scanned": roots}

    # Councils from before the index existed, in the projects already
    # known: cheap (seconds), in the background, once per engine start.
    def _startup_backfill() -> None:
        try:
            n = council_index.backfill()
            if n:
                log.info("council index: backfilled %d council(s)", n)
        except Exception:  # pragma: no cover — defensive
            log.exception("council index: startup backfill failed")

    @app.on_event("startup")
    def _start_backfill() -> None:
        threading.Thread(target=_startup_backfill, name="council-index",
                         daemon=True).start()


__all__ = ["register_dashboard_routes", "lan_allowed", "is_local_client",
           "LAN_ROUTES"]
