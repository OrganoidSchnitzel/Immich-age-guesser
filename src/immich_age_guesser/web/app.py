"""Small web UI: pick an album (batch of scans), write known dates or estimate unknown ones."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from ..dates import parse_date_spec, parse_year_bound
from ..immich import ImmichError
from ..jobs import JobManager
from ..service import AgeGuesser, report_dict

_HERE = Path(__file__).parent


class KnownDateRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1)
    date: str
    keep_order: bool = True


class EstimateRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1)
    joint: bool = False
    not_before: str | None = None
    not_after: str | None = None


class AssetIds(BaseModel):
    asset_ids: list[str] = Field(min_length=1)


class BirthDate(BaseModel):
    birth_date: date | None


class CalibrateRequest(BaseModel):
    per_person: int = Field(default=60, ge=5, le=1000)


def create_app(guesser: AgeGuesser) -> FastAPI:
    jobs = JobManager()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        jobs.shutdown()

    app = FastAPI(title="Immich Age Guesser", lifespan=lifespan)
    templates = Jinja2Templates(directory=_HERE / "templates")
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    app.state.guesser = guesser
    app.state.jobs = jobs
    immich = guesser.immich
    store = guesser.store

    def bad_request(exc: Exception) -> HTTPException:
        return HTTPException(status_code=400, detail=str(exc))

    # --- pages ------------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        error = ""
        albums: list[dict[str, Any]] = []
        version = ""
        try:
            version = immich.server_version()
            albums = sorted(immich.albums(), key=lambda a: a["albumName"].lower())
        except ImmichError as exc:
            error = str(exc)
        return templates.TemplateResponse(request, "index.html", {
            "albums": albums, "version": version, "error": error,
            "calibration": guesser.calibration_summary(),
        })

    @app.get("/album/{album_id}", response_class=HTMLResponse)
    def album_page(request: Request, album_id: str) -> Response:
        try:
            album = immich.album(album_id)
        except ImmichError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(request, "album.html", {
            "album": album, "immich_url": guesser.settings.public_url,
        })

    @app.get("/people", response_class=HTMLResponse)
    def people_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "people.html", {"immich_url": guesser.settings.public_url})

    @app.get("/thumb/{asset_id}")
    def thumbnail(asset_id: str, size: str = "thumbnail") -> Response:
        if size not in {"thumbnail", "preview"}:
            raise HTTPException(status_code=400, detail="bad size")
        try:
            data, content_type = immich.thumbnail(asset_id, size)
        except ImmichError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return Response(data, media_type=content_type, headers={"Cache-Control": "private, max-age=86400"})

    # --- data -------------------------------------------------------------------------------

    @app.get("/api/albums/{album_id}/assets")
    def album_assets(album_id: str) -> dict[str, Any]:
        try:
            assets = immich.album_assets(album_id)
        except ImmichError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        ids = [a.id for a in assets]
        dated = store.dated(ids)
        estimates = store.estimates(ids)
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
            people = [p for p in immich.people() if p.name]
        except ImmichError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return [{"id": p.id, "name": p.name, "hidden": p.is_hidden,
                 "birth_date": p.birth_date.isoformat() if p.birth_date else None}
                for p in sorted(people, key=lambda p: p.name.lower())]

    @app.get("/api/people/{person_id}/thumbnail")
    def person_thumbnail(person_id: str) -> Response:
        try:
            data, content_type = immich.person_thumbnail(person_id)
        except ImmichError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return Response(data, media_type=content_type, headers={"Cache-Control": "private, max-age=3600"})

    @app.put("/api/people/{person_id}/birthdate")
    def set_birthdate(person_id: str, body: BirthDate) -> dict[str, Any]:
        if body.birth_date and body.birth_date > date.today():
            raise HTTPException(status_code=400, detail="The birthday is in the future.")
        try:
            p = immich.set_birth_date(person_id, body.birth_date)
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
                "written": spec.anchor(guesser.settings.date_anchor).isoformat()}

    # --- actions (background jobs) ----------------------------------------------------------

    @app.post("/api/known-date")
    def known_date(body: KnownDateRequest) -> dict[str, Any]:
        try:
            spec = parse_date_spec(body.date)
        except ValueError as exc:
            raise bad_request(exc) from exc
        job = jobs.submit("known-date", lambda progress: guesser.apply_known_date(
            body.asset_ids, spec, keep_order=body.keep_order, progress=progress))
        return job.to_dict()

    @app.post("/api/estimate")
    def estimate(body: EstimateRequest) -> dict[str, Any]:
        try:
            not_before = parse_year_bound(body.not_before, end=False)
            not_after = parse_year_bound(body.not_after, end=True)
        except ValueError as exc:
            raise bad_request(exc) from exc
        job = jobs.submit("estimate", lambda progress: guesser.estimate(
            body.asset_ids, joint=body.joint, not_before=not_before, not_after=not_after, progress=progress))
        return job.to_dict()

    @app.post("/api/apply-estimates")
    def apply_estimates(body: AssetIds) -> dict[str, Any]:
        job = jobs.submit("apply", lambda progress: guesser.apply_estimates(body.asset_ids, progress=progress))
        return job.to_dict()

    @app.post("/api/discard-estimates")
    def discard_estimates(body: AssetIds) -> dict[str, Any]:
        store.delete_estimates(body.asset_ids)
        return {"discarded": len(body.asset_ids)}

    @app.post("/api/calibrate")
    def calibrate(body: CalibrateRequest) -> dict[str, Any]:
        job = jobs.submit("calibrate", lambda progress: report_dict(
            guesser.calibrate(per_person=body.per_person, progress=progress)))
        return job.to_dict()

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        return job.to_dict()

    return app
