"""Serveur FastAPI : API REST + WebSocket de jeu + fichiers statiques."""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .game import MAX_PLAYERS, GameManager, JoinError
from .security import DUMMY_HASH, RateLimiter, hash_password, verify_password

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
COOKIE = "session"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,20}$")
MAX_CUSTOM_QUESTIONS = 200

manager = GameManager()
auth_limiter = RateLimiter(limit=int(os.environ.get("AUTH_RATE_MAX", "20")), window=600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    db.seed_questions()
    task = asyncio.create_task(manager.cleanup_loop())
    yield
    task.cancel()


app = FastAPI(title="Logik Arena", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

CSP = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Content-Security-Policy", CSP)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ------------------------------------------------------------------ modèles
class Credentials(BaseModel):
    username: str
    password: str


class QuestionIn(BaseModel):
    text: str
    figure: str = ""
    choices: list[str]
    correct: int
    explanation: str = ""
    difficulty: int = 50


class RoomIn(BaseModel):
    question_ids: list[int]
    stake: int = 1000
    timer: int = 30


# ------------------------------------------------------------------ outils
def client_ip(request: Request) -> str:
    if os.environ.get("TRUST_PROXY") == "1":
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "?"


def current_user(request: Request) -> dict:
    user = db.get_user_by_token(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(401, "Connexion requise.")
    return user


def set_session_cookie(request: Request, response: Response, token: str) -> None:
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(
        COOKIE, token, max_age=db.SESSION_TTL, httponly=True, samesite="lax", secure=secure, path="/"
    )


def check_auth_rate(request: Request) -> None:
    if not auth_limiter.hit(client_ip(request)):
        raise HTTPException(429, "Trop de tentatives. Réessaie dans quelques minutes.")


def clean_question(q: QuestionIn) -> dict:
    text = q.text.strip()
    if not 3 <= len(text) <= 300:
        raise HTTPException(400, "L'énoncé doit faire entre 3 et 300 caractères.")
    figure = q.figure.rstrip()
    if len(figure) > 400:
        raise HTTPException(400, "La figure est limitée à 400 caractères.")
    choices = [c.strip() for c in q.choices]
    if not 2 <= len(choices) <= 4:
        raise HTTPException(400, "Il faut entre 2 et 4 réponses possibles.")
    if any(not c or len(c) > 80 for c in choices):
        raise HTTPException(400, "Chaque réponse doit faire entre 1 et 80 caractères.")
    if len({c.lower() for c in choices}) != len(choices):
        raise HTTPException(400, "Les réponses doivent être toutes différentes.")
    if not 0 <= q.correct < len(choices):
        raise HTTPException(400, "Indique laquelle des réponses est la bonne.")
    explanation = q.explanation.strip()
    if len(explanation) > 300:
        raise HTTPException(400, "L'explication est limitée à 300 caractères.")
    if not 1 <= q.difficulty <= 99:
        raise HTTPException(400, "La difficulté doit être comprise entre 1 et 99.")
    return {
        "text": text, "figure": figure, "choices": choices, "correct": q.correct,
        "explanation": explanation, "difficulty": q.difficulty,
    }


def auto_playlist(count: int) -> list[int]:
    """Choisit `count` questions de la banque, de la plus facile à la plus difficile."""
    pool = db.bank_difficulties()
    count = min(count, len(pool))
    chosen: list[dict] = []
    for i in range(count):
        target = 95 - 94 * i / (count - 1) if count > 1 else 50
        remaining = [q for q in pool if q not in chosen]
        best = min(abs(q["difficulty"] - target) for q in remaining)
        close = [q for q in remaining if abs(q["difficulty"] - target) <= best + 4]
        chosen.append(random.choice(close))
    chosen.sort(key=lambda q: -q["difficulty"])
    return [q["id"] for q in chosen]


# ---------------------------------------------------------------- comptes
def _start_session(request: Request, response: Response, user_id: int, username: str) -> dict:
    set_session_cookie(request, response, db.create_session(user_id))
    return {"id": user_id, "username": username}


@app.post("/api/register")
def register(data: Credentials, request: Request, response: Response):
    check_auth_rate(request)
    username = data.username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "Pseudo : 3 à 20 caractères (lettres, chiffres, _ . -).")
    if not 8 <= len(data.password) <= 128:
        raise HTTPException(400, "Le mot de passe doit faire entre 8 et 128 caractères.")
    try:
        user_id = db.create_user(username, hash_password(data.password))
    except ValueError:
        raise HTTPException(409, "Ce pseudo est déjà pris.")
    return _start_session(request, response, user_id, username)


@app.post("/api/login")
def login(data: Credentials, request: Request, response: Response):
    check_auth_rate(request)
    row = db.get_user_by_name(data.username.strip())
    ok = verify_password(data.password, row["pw_hash"] if row else DUMMY_HASH)
    if not row or not ok:
        raise HTTPException(401, "Pseudo ou mot de passe incorrect.")
    return _start_session(request, response, row["id"], row["username"])


@app.post("/api/logout")
def logout(request: Request, response: Response):
    db.delete_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return {"id": user["id"], "username": user["username"]}


# -------------------------------------------------------------- questions
@app.get("/api/questions")
def questions(user: dict = Depends(current_user)):
    return db.list_questions(user["id"])


@app.post("/api/questions")
def create_question(data: QuestionIn, user: dict = Depends(current_user)):
    if db.count_user_questions(user["id"]) >= MAX_CUSTOM_QUESTIONS:
        raise HTTPException(400, f"Limite de {MAX_CUSTOM_QUESTIONS} questions personnelles atteinte.")
    return {"id": db.insert_question(user["id"], clean_question(data))}


@app.delete("/api/questions/{question_id}")
def remove_question(question_id: int, user: dict = Depends(current_user)):
    if not db.delete_question(user["id"], question_id):
        raise HTTPException(404, "Question introuvable.")
    return {"ok": True}


@app.get("/api/playlist/auto")
def playlist_auto(count: int = 10, user: dict = Depends(current_user)):
    if not 3 <= count <= 30:
        raise HTTPException(400, "Choisis entre 3 et 30 questions.")
    return {"question_ids": auto_playlist(count)}


# ------------------------------------------------------------------ salles
@app.post("/api/rooms")
def create_room(data: RoomIn, user: dict = Depends(current_user)):
    if not 1 <= len(data.question_ids) <= 40:
        raise HTTPException(400, "Une partie contient entre 1 et 40 questions.")
    if len(set(data.question_ids)) != len(data.question_ids):
        raise HTTPException(400, "Une question ne peut apparaître qu'une fois.")
    if not 1 <= data.stake <= 100_000:
        raise HTTPException(400, "La mise doit être comprise entre 1 et 100 000 €.")
    if not 5 <= data.timer <= 120:
        raise HTTPException(400, "Le temps par question doit être compris entre 5 et 120 secondes.")
    try:
        questions_ = db.get_questions_for_game(data.question_ids, user["id"])
        room = manager.create(user["id"], user["username"], questions_, data.stake, data.timer)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"code": room.code}


@app.get("/api/rooms/mine")
def my_rooms(user: dict = Depends(current_user)):
    return {
        "rooms": [
            {"code": r.code, "status": r.status, "players": len(r.players)}
            for r in manager.rooms_of(user["id"])
        ]
    }


@app.get("/api/rooms/{code}")
def room_info(code: str, user: dict = Depends(current_user)):
    room = manager.get(code)
    if room is None:
        raise HTTPException(404, "Salle introuvable. Vérifie le code.")
    return {
        "code": room.code, "status": room.status, "host": room.host_name,
        "players": len(room.players), "max_players": MAX_PLAYERS,
    }


# --------------------------------------------------------------- WebSocket
def origin_allowed(origin: str, host: str | None) -> bool:
    extra = {o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()}
    if origin in extra:
        return True
    try:
        return urlparse(origin).netloc == host
    except ValueError:
        return False


async def refuse(ws: WebSocket, message: str, code: int) -> None:
    try:
        await ws.send_text(json.dumps({"type": "error", "message": message}, ensure_ascii=False))
        await ws.close(code)
    except Exception:
        pass


@app.websocket("/ws/{code}")
async def room_socket(ws: WebSocket, code: str):
    await ws.accept()
    origin = ws.headers.get("origin")
    if origin and not origin_allowed(origin, ws.headers.get("host")):
        return await refuse(ws, "Origine refusée.", 4403)
    user = await asyncio.to_thread(db.get_user_by_token, ws.cookies.get(COOKIE))
    if not user:
        return await refuse(ws, "Connexion requise.", 4401)
    room = manager.get(code)
    if room is None:
        return await refuse(ws, "Salle introuvable.", 4404)
    try:
        await room.connect(ws, user["id"], user["username"])
    except JoinError as exc:
        return await refuse(ws, exc.message, exc.code)

    try:
        while True:
            text = await ws.receive_text()
            if len(text) > 4096:
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if await room.handle(user["id"], msg):
                manager.remove(room.code)
                break
    except WebSocketDisconnect:
        pass
    finally:
        await room.disconnect(ws, user["id"])


# ----------------------------------------------------------------- statique
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
