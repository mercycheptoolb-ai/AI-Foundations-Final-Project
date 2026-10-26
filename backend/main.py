"""Kukua web server.

Run from the repo folder:  .venv/bin/uvicorn backend.main:app --port 8000
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Literal

from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pydantic_ai.exceptions import UsageLimitExceeded

from . import auth, store
from .agents import fallback_note, run_intake, run_triage
from .data_guide import ask
from .config import DAILY_BUDGET_USD, MODEL_NAME, ROOT
from .growth import MeasurementError, age_days_from_months, assess
from .models import DangerSign

app = FastAPI(title="Kukua - growth monitoring assistant")
FRONTEND = ROOT / "frontend"
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def budget_left() -> float:
    return DAILY_BUDGET_USD - store.spent_today()


def require_budget() -> None:
    if budget_left() <= 0:
        raise HTTPException(429, f"Today's AI budget (${DAILY_BUDGET_USD:.2f}) is used up. "
                                 "You can still enter measurements by hand: the WHO calculator works without AI.")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


# ---------------------------------------------------------------- accounts

def optional_user(kukua_session: str | None = Cookie(default=None)) -> dict | None:
    """The signed-in worker, or None for a guest. Guests can check growth but nothing is stored for them."""
    return auth.user_for(kukua_session)


def current_user(user: dict | None = Depends(optional_user)) -> dict:
    if not user:
        raise HTTPException(401, "Sign in to use saved child records.")
    return user


# Guests share the daily budget, so each device (IP) gets a small hourly allowance of AI calls.
GUEST_AI_CALLS_PER_HOUR = 20
_guest_calls: dict[str, deque] = defaultdict(deque)


def guest_ai_allowed(request: Request) -> bool:
    now, calls = time.time(), _guest_calls[request.client.host if request.client else "?"]
    while calls and now - calls[0] > 3600:
        calls.popleft()
    if len(calls) >= GUEST_AI_CALLS_PER_HOUR:
        return False
    calls.append(now)
    return True


def _set_session(response: Response, token: str) -> None:
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_HOURS * 3600, httponly=True, samesite="lax")


class LoginRequest(BaseModel):
    username: str = Field(max_length=30)
    password: str = Field(max_length=128)


class RegisterRequest(LoginRequest):
    full_name: str = Field(max_length=80)
    facility: str = Field(max_length=80)


@app.post("/api/login")
def login(req: LoginRequest, response: Response) -> dict:
    try:
        token, user = auth.login(req.username, req.password)
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    _set_session(response, token)
    return user


@app.post("/api/register")
def register(req: RegisterRequest, response: Response) -> dict:
    try:
        auth.register(req.username, req.full_name, req.facility, req.password)
        token, user = auth.login(req.username, req.password)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    _set_session(response, token)
    return user


@app.post("/api/logout")
def logout(response: Response, kukua_session: str | None = Cookie(default=None)) -> dict:
    auth.logout(kukua_session)
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user: dict | None = Depends(optional_user)) -> dict:
    return {"user": user}


@app.post("/api/intake")
async def intake(request: Request, note: str = Form(""), image: UploadFile | None = File(None),
                 user: dict | None = Depends(optional_user)) -> dict:
    require_budget()
    if user is None and not guest_ai_allowed(request):
        raise HTTPException(429, f"Guest limit reached ({GUEST_AI_CALLS_PER_HOUR} AI calls an hour). Sign in, or enter "
                                 "the measurements by hand: the WHO calculator needs no AI.")
    img = None
    if image is not None and image.filename:
        if image.content_type not in IMAGE_TYPES:
            raise HTTPException(415, "Photo must be JPEG, PNG or WebP.")
        data = await image.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "Photo is larger than 4 MB.")
        img = (data, image.content_type)
    try:
        return await run_intake(note, img)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:  # missing API key
        raise HTTPException(503, str(exc)) from exc
    except UsageLimitExceeded as exc:
        raise HTTPException(429, f"Intake stopped by the per-run token limit: {exc}") from exc


@app.get("/api/children")
def children(name: str = "", user: dict = Depends(current_user)) -> list[dict]:
    """Children whose name matches, one row per ID, so the worker can pick the right record."""
    return store.find_children(name[:80])


@app.get("/api/children/{child_id}")
def child_by_id(child_id: str, user: dict = Depends(current_user)) -> dict:
    found = store.child(child_id[:30])
    if not found:
        raise HTTPException(404, f"No child with ID {child_id}. Leave the ID empty to register a new child.")
    return {**found, "history": store.history(child_id)}


class AssessRequest(BaseModel):
    child_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{1,29}$")
    child_name: str | None = Field(default=None, max_length=80)
    sex: Literal["male", "female"]
    age_months: float = Field(ge=0, le=60)
    weight_kg: float
    length_cm: float | None = None
    measured_lying: bool | None = None
    muac_cm: float | None = None
    oedema: bool = False
    danger_signs: list[DangerSign] = []
    caregiver_language: Literal["English", "Swahili"] = "English"
    notes: str | None = Field(default=None, max_length=600)
    ai_note: bool = True


@app.post("/api/assess")
async def assess_visit(req: AssessRequest, request: Request, user: dict | None = Depends(optional_user)) -> dict:
    t0 = time.perf_counter()
    name = (req.child_name or "").strip() or "Unnamed child"
    age_days = age_days_from_months(req.age_months)
    id_notes: list[str] = []
    guest = user is None
    if guest:
        # Guests never read or write stored child records.
        child_id = None
        id_notes.append("Guest mode: this check was not saved and no earlier visits were used. Sign in to keep a child's history.")
    elif req.child_id:
        child_id = store.normalize_id(req.child_id)
        known = store.child(child_id)
        if known:
            # Refuse to attach this visit to a record that is clearly another child.
            if known["sex"] != req.sex:
                raise HTTPException(409, f"ID {child_id} belongs to a {'girl' if known['sex'] == 'female' else 'boy'} "
                                         f"({known['child_name']}). Check the ID before assessing.")
            if age_days < known["age_days"] - 14:
                raise HTTPException(409, f"ID {child_id} ({known['child_name']}) was {known['age_days'] / 30.4375:.1f} months "
                                         f"old on {known['visit_date']}, older than the {req.age_months:g} months entered. "
                                         "Check the ID or the age.")
            if store.child_key(known["child_name"]) != store.child_key(name):
                id_notes.append(f"Name differs from the record for {child_id} ('{known['child_name']}'). Check it is the same child.")
        else:
            id_notes.append(f"{child_id} is a new ID: no earlier visits on record.")
    else:
        child_id = store.new_child_id()
        id_notes.append(f"New child registered as {child_id}. Write this ID on the child's card for the next visit.")
    history = store.history(child_id) if child_id else []
    try:
        a = assess(sex=req.sex, age_days=age_days, weight_kg=req.weight_kg,
                   length_cm=req.length_cm, measured_lying=req.measured_lying, muac_cm=req.muac_cm,
                   oedema=req.oedema, danger_signs=list(req.danger_signs), history=history)
    except MeasurementError as exc:
        raise HTTPException(422, str(exc)) from exc
    calc_ms = round((time.perf_counter() - t0) * 1000, 1)

    record = req.model_dump()
    triage = None
    reason = "AI note turned off" if not req.ai_note else "daily AI budget used up"
    if req.ai_note and guest and not guest_ai_allowed(request):
        reason = f"guest limit of {GUEST_AI_CALLS_PER_HOUR} AI calls an hour reached"
    elif req.ai_note and budget_left() > 0:
        try:
            triage = await run_triage(record, a, history)
        except RuntimeError as exc:  # missing API key: the WHO result still stands
            reason = str(exc)
    if triage is None:
        triage = {"note": fallback_note(a, req.caregiver_language).model_dump(), "tool_calls": [],
                  "guard_events": [f"Rule-based template note used ({reason})."], "used_fallback": True, "usage": None}

    visit_id = None if guest else store.save_visit(
        user["username"], child_id, name, req.sex, a.age_days, req.weight_kg, a.length_cm_used, req.muac_cm,
        a.status, a.referral, a.model_dump(), triage["note"])
    return {"visit_id": visit_id, "guest": guest, "child_id": child_id, "id_notes": id_notes, "child_name": name, "assessment": a.model_dump(), "history": history,
            "calc_ms": calc_ms, **triage}


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=2000)


class ChatRequest(BaseModel):
    question: str = Field(max_length=500)
    history: list[ChatTurn] = Field(default=[], max_length=12)


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request, user: dict | None = Depends(optional_user)) -> dict:
    """Data guide: questions about Kenya nutrition data by county, province and over time. Open to guests."""
    require_budget()
    if user is None and not guest_ai_allowed(request):
        raise HTTPException(429, f"Guest limit reached ({GUEST_AI_CALLS_PER_HOUR} AI calls an hour). Sign in to keep asking.")
    try:
        return await ask(req.question, [t.model_dump() for t in req.history])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/visits")
def visits(user: dict = Depends(current_user)) -> list[dict]:
    return store.recent_visits(user["username"])


@app.get("/api/usage")
def usage() -> dict:
    spent = store.spent_today()
    return {"model": MODEL_NAME, "spent_today_usd": round(spent, 6), "daily_budget_usd": DAILY_BUDGET_USD,
            "budget_left_usd": round(DAILY_BUDGET_USD - spent, 6)}
