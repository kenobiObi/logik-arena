"""Accès SQLite : comptes, sessions et questions."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("DATABASE_PATH", str(ROOT_DIR / "data" / "game.db"))
QUESTIONS_FILE = Path(__file__).resolve().parent / "data" / "questions.json"
SESSION_TTL = 30 * 24 * 3600  # 30 jours

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash    TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    key         TEXT UNIQUE,
    text        TEXT NOT NULL,
    figure      TEXT NOT NULL DEFAULT '',
    choices     TEXT NOT NULL,
    correct     INTEGER NOT NULL,
    explanation TEXT NOT NULL DEFAULT '',
    difficulty  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_questions_owner ON questions(owner_id);
"""


@contextmanager
def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    with db() as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)


def seed_questions() -> int:
    """Charge (ou met à jour) la banque de questions depuis questions.json."""
    data = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    now = int(time.time())
    keys = [q["key"] for q in data]
    with db() as c:
        for q in data:
            c.execute(
                """INSERT INTO questions
                   (owner_id, key, text, figure, choices, correct, explanation, difficulty, created_at)
                   VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     text = excluded.text, figure = excluded.figure, choices = excluded.choices,
                     correct = excluded.correct, explanation = excluded.explanation,
                     difficulty = excluded.difficulty""",
                (
                    q["key"], q["text"], q.get("figure", ""), json.dumps(q["choices"], ensure_ascii=False),
                    q["correct"], q.get("explanation", ""), q["difficulty"], now,
                ),
            )
        marks = ",".join("?" for _ in keys)
        c.execute(f"DELETE FROM questions WHERE owner_id IS NULL AND key NOT IN ({marks})", keys)
    return len(data)


# ------------------------------------------------------------------ comptes
def create_user(username: str, pw_hash: str) -> int:
    try:
        with db() as c:
            cur = c.execute(
                "INSERT INTO users (username, pw_hash, created_at) VALUES (?, ?, ?)",
                (username, pw_hash, int(time.time())),
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError as exc:
        raise ValueError("username_taken") from exc


def get_user_by_name(username: str):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with db() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        c.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (_hash_token(token), user_id, now + SESSION_TTL),
        )
    return token


def get_user_by_token(token: str | None):
    if not token:
        return None
    with db() as c:
        row = c.execute(
            """SELECT u.id AS id, u.username AS username
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ? AND s.expires_at > ?""",
            (_hash_token(token), int(time.time())),
        ).fetchone()
    return dict(row) if row else None


def delete_session(token: str | None) -> None:
    if not token:
        return
    with db() as c:
        c.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


# ---------------------------------------------------------------- questions
def _question_dict(row, reveal: bool) -> dict:
    d = {
        "id": row["id"],
        "text": row["text"],
        "figure": row["figure"],
        "choices": json.loads(row["choices"]),
        "difficulty": row["difficulty"],
        "mine": row["owner_id"] is not None,
    }
    if reveal:
        d["correct"] = row["correct"]
        d["explanation"] = row["explanation"]
    return d


def list_questions(user_id: int) -> dict:
    """Banque (sans les réponses, pour éviter la triche) + questions de l'utilisateur."""
    with db() as c:
        bank = c.execute(
            "SELECT * FROM questions WHERE owner_id IS NULL ORDER BY difficulty DESC, id"
        ).fetchall()
        mine = c.execute(
            "SELECT * FROM questions WHERE owner_id = ? ORDER BY id DESC", (user_id,)
        ).fetchall()
    return {
        "bank": [_question_dict(r, reveal=False) for r in bank],
        "mine": [_question_dict(r, reveal=True) for r in mine],
    }


def count_user_questions(user_id: int) -> int:
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM questions WHERE owner_id = ?", (user_id,)).fetchone()[0]


def insert_question(owner_id: int, q: dict) -> int:
    with db() as c:
        cur = c.execute(
            """INSERT INTO questions
               (owner_id, key, text, figure, choices, correct, explanation, difficulty, created_at)
               VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?)""",
            (
                owner_id, q["text"], q["figure"], json.dumps(q["choices"], ensure_ascii=False),
                q["correct"], q["explanation"], q["difficulty"], int(time.time()),
            ),
        )
        return int(cur.lastrowid)


def delete_question(owner_id: int, question_id: int) -> bool:
    with db() as c:
        cur = c.execute("DELETE FROM questions WHERE id = ? AND owner_id = ?", (question_id, owner_id))
        return cur.rowcount > 0


def get_questions_for_game(ids: list[int], user_id: int) -> list[dict]:
    """Questions complètes (avec réponses) dans l'ordre demandé.
    Seules la banque et les questions de l'utilisateur sont accessibles."""
    out = []
    with db() as c:
        for qid in ids:
            row = c.execute(
                "SELECT * FROM questions WHERE id = ? AND (owner_id IS NULL OR owner_id = ?)",
                (qid, user_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"Question {qid} introuvable.")
            out.append(_question_dict(row, reveal=True))
    return out


def bank_difficulties() -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT id, difficulty FROM questions WHERE owner_id IS NULL").fetchall()
    return [{"id": r["id"], "difficulty": r["difficulty"]} for r in rows]
