"""The council dashboard's routes on the engine's HTTP server.

    GET /dashboard                              the page (one HTML file)
    GET /v1/dashboard/councils                  ?limit= &status= &q=
    GET /v1/dashboard/councils/{sid}            ?since= &full=1
    POST /v1/dashboard/index/backfill           {"scan": [paths], "max_depth": 5}

The page reads only these and the existing per-council SSE stream
(``/v1/consult/{sid}/events``) for a council that is running. Everything
is read-only except the backfill, which writes only the index. The
engine binds loopback, and so does the dashboard.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger("consultants.server.dashboard_routes")


def register_dashboard_routes(app) -> None:
    from fastapi import Body, HTTPException
    from fastapi.responses import HTMLResponse

    from consultants.server import council_index, dashboard
    from consultants.server.dashboard_page import PAGE

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


__all__ = ["register_dashboard_routes"]
