"""Small web UI: pick an album (batch of scans), write known dates or estimate unknown ones."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from ..config import ENV_NAMES, Settings, env_overrides
from ..dates import parse_date_spec, parse_year_bound
from ..immich import ImmichClient, ImmichError
from ..jobs import JobManager
from ..service import AgeGuesser, build_guesser, report_dict

log = logging.getLogger(__name__)
_HERE = Path(__file__).parent
# Sent by our own pages on every state-changing request. A custom header cannot be sent cross-site
# without a CORS preflight (which this app never allows), so other websites cannot trigger actions.
CSRF_HEADER = "x-age-guesser"


class KnownDateRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1)
    date: str
    keep_order: bool = True
    album_id: str | None = None
    remove_from_album: bool = False


class EstimateRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1)
    joint: bool = False
    not_before: str | None = None
    not_after: str | None = None


class ApplyRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1)
    album_id: str | None = None
    remove_from_album: bool = False


class AssetIds(BaseModel):
    asset_ids: list[str] = Field(min_length=1)


class BirthDate(BaseModel):
    birth_date: date | None


class CalibrateRequest(BaseModel):
    per_person: int = Field(default=60, ge=5, le=1000)


class ConnectionTest(BaseModel):
    immich_url: str
    immich_api_key: str = ""


class Runtime:
    """Holds the current settings and AgeGuesser; the setup page can swap both at runtime."""

    def __init__(self, settings: Settings, build: Callable[[Settings], AgeGuesser],
                 guesser: AgeGuesser | None = None) -> None:
        self.settings = settings
        self._build = build
        self._lock = threading.Lock()
        self.guesser = guesser if guesser is not None else (build(settings) if settings.configured else None)

    def require(self) -> AgeGuesser:
        g = self.guesser
        if g is None:
            raise HTTPException(status_code=503, detail="Not set up yet: open the Settings page.")
        return g

    def reconfigure(self, settings: Settings) -> None:
        with self._lock:
            new = self._build(settings)
            old = self.guesser
            if old is not None and old.backend == new.backend:
                new.adopt_model(old)
            # The old instance is left to running jobs and the garbage collector.
            self.guesser, self.settings = new, settings


def _client(settings: Settings) -> ImmichClient:
    return ImmichClient(settings.immich_url, settings.immich_api_key, timeout=15)


def create_app(guesser: AgeGuesser | None = None, *, settings: Settings | None = None,
               build: Callable[[Settings], AgeGuesser] = build_guesser,
               make_client: Callable[[Settings], ImmichClient] = _client) -> FastAPI:
    if settings is None:
        settings = guesser.settings if guesser is not None else Settings.load()
    runtime = Runtime(settings, build, guesser)
    jobs = JobManager()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        jobs.shutdown()

    app = FastAPI(title="Immich Age Guesser", lifespan=lifespan)
    templates = Jinja2Templates(directory=_HERE / "templates")
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    app.state.runtime = runtime
    app.state.jobs = jobs

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"detail": "Missing request header."}, status_code=403)
        return await call_next(request)

    def bad_request(exc: Exception) -> HTTPException:
        return HTTPException(status_code=400, detail=str(exc))

    def page(request: Request, name: str, **context: Any) -> Response:
        return templates.TemplateResponse(request, name, {"configured": runtime.guesser is not None, **context})

    def setup_redirect() -> None:
        if runtime.guesser is None:
            raise _Redirect("/settings")

    g = runtime.require

    # --- pages ------------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(setup_redirect)])
    def index(request: Request) -> Response:
        guesser = g()
        error = ""
        albums: list[dict[str, Any]] = []
        version = ""
        try:
            version = guesser.immich.server_version()
            albums = sorted(guesser.immich.albums(), key=lambda a: a["albumName"].lower())
        except ImmichError as exc:
            error = str(exc)
        return page(request, "index.html", albums=albums, version=version, error=error,
                    calibration=guesser.calibration_summary())

    @app.get("/album/{album_id}", response_class=HTMLResponse, dependencies=[Depends(setup_redirect)])
    def album_page(request: Request, album_id: str) -> Response:
        guesser = g()
        try:
            album = guesser.immich.album(album_id)
        except ImmichError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return page(request, "album.html", album=album, immich_url=guesser.settings.public_url,
                    remove_default=guesser.settings.remove_from_album)

    @app.get("/people", response_class=HTMLResponse, dependencies=[Depends(setup_redirect)])
    def people_page(request: Request) -> Response:
        return page(request, "people.html")

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> Response:
        return page(request, "settings.html")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "configured": runtime.guesser is not None}

    @app.get("/thumb/{asset_id}")
    def thumbnail(asset_id: str, size: str = "thumbnail") -> Response:
        if size not in {"thumbnail", "preview"}:
            raise HTTPException(status_code=400, detail="bad size")
        try:
            data, content_type = g().immich.thumbnail(asset_id, size)
        except ImmichError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return Response(data, media_type=content_type, headers={"Cache-Control": "private, max-age=86400"})

    # --- settings ---------------------------------------------------------------------------

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        s = runtime.settings
        values = {name: getattr(s, name) for name in ENV_NAMES if name != "immich_api_key"}
        return {"values": values, "has_api_key": bool(s.immich_api_key), "locked": sorted(env_overrides()),
                "configured": runtime.guesser is not None, "data_dir": str(s.data_dir)}

    def merged_settings(body: dict[str, Any]) -> Settings:
        current = runtime.settings
        locked = env_overrides()
        values = {k: v for k, v in body.items() if k in ENV_NAMES and k not in locked}
        new_url = str(values.get("immich_url", current.immich_url)).strip().rstrip("/")
        if not values.get("immich_api_key"):
            # Never send the saved key to a server it was not entered for.
            if new_url != current.immich_url and "immich_api_key" not in locked:
                raise ValueError("Enter the API key again when changing the Immich URL.")
            values.pop("immich_api_key", None)
        merged = current.with_values(values)
        merged.validate()
        return merged

    @app.post("/api/settings/test")
    def test_connection(body: ConnectionTest) -> dict[str, Any]:
        try:
            s = merged_settings(body.model_dump())
        except ValueError as exc:
            raise bad_request(exc) from exc
        if not s.configured:
            raise bad_request(ValueError("Enter the Immich URL and an API key."))
        return _probe(make_client(s))

    @app.put("/api/settings")
    def save_settings(body: dict[str, Any]) -> dict[str, Any]:
        try:
            s = merged_settings(body)
        except (ValueError, TypeError) as exc:
            raise bad_request(exc) from exc
        if not s.configured:
            raise bad_request(ValueError("Enter the Immich URL and an API key."))
        s.save(exclude=env_overrides())
        runtime.reconfigure(s)
        return {"ok": True}

    # --- data -------------------------------------------------------------------------------

    @app.get("/api/albums/{album_id}/assets")
    def album_assets(album_id: str) -> dict[str, Any]:
        guesser = g()
        try:
            assets = guesser.immich.album_assets(album_id)
        except ImmichError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        ids = [a.id for a in assets]
        dated = guesser.store.dated(ids)
        estimates = guesser.store.estimates(ids)
        people: dict[str, dict[str, Any]] = {}
        items = []
        for a in assets:
            for p in a.people:
                if p.name:
                    entry = people.setdefault(p.id, {"id": p.id, "name": p.name, "photos": 0,
                                                     "birth_date": p.birth_date.isoformat() if p.birth_date else None})
                    entry["photos"] += 1
            items.append({
                "id": a.id,
                "file_name": a.file_name,
                "date": a.local_datetime.isoformat() if a.local_datetime else None,
                "people": [p.name for p in a.people if p.name],
                "dated": dated[a.id].to_dict() if a.id in dated else None,
                "estimate": estimates.get(a.id),
            })
        return {"assets": items, "people": sorted(people.values(), key=lambda p: p["name"].lower())}

    @app.get("/api/people")
    def list_people() -> list[dict[str, Any]]:
        try:
            people = [p for p in g().immich.people() if p.name]
        except ImmichError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return [{"id": p.id, "name": p.name, "hidden": p.is_hidden,
                 "birth_date": p.birth_date.isoformat() if p.birth_date else None}
                for p in sorted(people, key=lambda p: p.name.lower())]

    @app.get("/api/people/{person_id}/thumbnail")
    def person_thumbnail(person_id: str) -> Response:
        try:
            data, content_type = g().immich.person_thumbnail(person_id)
        except ImmichError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return Response(data, media_type=content_type, headers={"Cache-Control": "private, max-age=3600"})

    @app.put("/api/people/{person_id}/birthdate")
    def set_birthdate(person_id: str, body: BirthDate) -> dict[str, Any]:
        if body.birth_date and body.birth_date > date.today():
            raise HTTPException(status_code=400, detail="The birthday is in the future.")
        try:
            p = g().immich.set_birth_date(person_id, body.birth_date)
        except ImmichError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"id": p.id, "name": p.name, "birth_date": p.birth_date.isoformat() if p.birth_date else None}

    @app.get("/api/parse-date")
    def parse_date(text: str) -> dict[str, Any]:
        try:
            spec = parse_date_spec(text)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "label": spec.label, "precision": spec.precision,
                "written": spec.anchor(runtime.settings.date_anchor).isoformat()}

    # --- actions (background jobs) ----------------------------------------------------------

    @app.post("/api/known-date")
    def known_date(body: KnownDateRequest) -> dict[str, Any]:
        guesser = g()
        try:
            spec = parse_date_spec(body.date)
        except ValueError as exc:
            raise bad_request(exc) from exc
        album = body.album_id if body.remove_from_album else None
        job = jobs.submit("known-date", lambda progress: guesser.apply_known_date(
            body.asset_ids, spec, keep_order=body.keep_order, remove_from_album=album, progress=progress))
        return job.to_dict()

    @app.post("/api/estimate")
    def estimate(body: EstimateRequest) -> dict[str, Any]:
        guesser = g()
        try:
            not_before = parse_year_bound(body.not_before, end=False)
            not_after = parse_year_bound(body.not_after, end=True)
        except ValueError as exc:
            raise bad_request(exc) from exc
        job = jobs.submit("estimate", lambda progress: guesser.estimate(
            body.asset_ids, joint=body.joint, not_before=not_before, not_after=not_after, progress=progress))
        return job.to_dict()

    @app.post("/api/apply-estimates")
    def apply_estimates(body: ApplyRequest) -> dict[str, Any]:
        guesser = g()
        album = body.album_id if body.remove_from_album else None
        job = jobs.submit("apply", lambda progress: guesser.apply_estimates(
            body.asset_ids, remove_from_album=album, progress=progress))
        return job.to_dict()

    @app.post("/api/discard-estimates")
    def discard_estimates(body: AssetIds) -> dict[str, Any]:
        g().store.delete_estimates(body.asset_ids)
        return {"discarded": len(body.asset_ids)}

    @app.post("/api/calibrate")
    def calibrate(body: CalibrateRequest) -> dict[str, Any]:
        guesser = g()
        job = jobs.submit("calibrate", lambda progress: report_dict(
            guesser.calibrate(per_person=body.per_person, progress=progress)))
        return job.to_dict()

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        return job.to_dict()

    @app.exception_handler(_Redirect)
    async def redirect(_: Request, exc: _Redirect) -> Response:
        return RedirectResponse(exc.location, status_code=303)

    return app


class _Redirect(Exception):
    def __init__(self, location: str) -> None:
        self.location = location


def _probe(client: ImmichClient) -> dict[str, Any]:
    """Check that Immich is reachable and the key can read what the tool needs."""
    checks: list[dict[str, Any]] = []
    try:
        try:
            version = client.server_version()
        except ImmichError as exc:
            return {"ok": False, "checks": [{"label": "Reach Immich", "ok": False, "detail": str(exc)}]}
        checks.append({"label": f"Reach Immich {version}", "ok": True, "detail": ""})
        for label, fn in [
            ("Read albums (album.read)", client.albums),
            ("Read people (person.read)", lambda: client._json("GET", "/people", params={"size": 1})),
            ("Read photos (asset.read)", lambda: client._json("POST", "/search/metadata", json={"size": 1})),
        ]:
            try:
                fn()
                checks.append({"label": label, "ok": True, "detail": ""})
            except ImmichError as exc:
                detail = "the API key lacks this permission" if " 403:" in str(exc) else \
                    "wrong API key" if " 401:" in str(exc) else str(exc)
                checks.append({"label": label, "ok": False, "detail": detail})
    finally:
        client.close()
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
