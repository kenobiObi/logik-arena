"""Moteur de jeu : salles (rooms), candidats, déroulement des questions.

Règles
------
* Un animateur (hôte) crée une salle ; jusqu'à MAX_PLAYERS candidats la rejoignent.
* Chaque candidat démarre avec une mise (1 000 € par défaut). Une mauvaise réponse
  (ou une absence de réponse) l'élimine et sa mise rejoint la cagnotte.
* Si TOUS les candidats encore en lice se trompent à une question intermédiaire,
  personne n'est éliminé. Ce n'est pas le cas de la question ultime.
* La dernière question de la liste est la question ultime : les candidats qui y
  répondent correctement se partagent la cagnotte.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import WebSocket

MAX_PLAYERS = 40
MAX_ROOMS = 200
MAX_ROOMS_PER_HOST = 3
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ANSWER_GRACE = 0.6      # tolérance (s) pour la latence réseau
IDLE_ROOM_TTL = 900     # une salle vide depuis 15 min est supprimée


class JoinError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class Player:
    user_id: int
    name: str
    alive: bool = True
    answer: Optional[int] = None
    out_at: Optional[int] = None   # numéro (1..n) de la question de l'élimination
    ws: Optional[WebSocket] = None


class Room:
    def __init__(self, code: str, host_id: int, host_name: str, questions: list[dict], stake: int, timer: int):
        self.code = code
        self.host_id = host_id
        self.host_name = host_name
        self.questions = questions
        self.stake = stake
        self.timer = timer

        self.status = "lobby"          # lobby | question | reveal | finished
        self.qi = -1
        self.pot = 0
        self.deadline = 0.0
        self.result: Optional[dict] = None
        self.final: Optional[dict] = None

        self.players: dict[int, Player] = {}
        self.spectators: dict[int, tuple[str, WebSocket]] = {}
        self.banned: set[int] = set()
        self.host_ws: Optional[WebSocket] = None

        self.last_activity = time.time()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ utilitaires
    def touch(self) -> None:
        self.last_activity = time.time()

    def has_connections(self) -> bool:
        return bool(self.host_ws or self.spectators or any(p.ws for p in self.players.values()))

    @property
    def alive_count(self) -> int:
        return sum(1 for p in self.players.values() if p.alive)

    @staticmethod
    async def _send(ws: WebSocket, text: str) -> None:
        try:
            await ws.send_text(text)
        except Exception:
            pass

    @staticmethod
    async def _close(ws: Optional[WebSocket], code: int) -> None:
        if ws is None:
            return
        try:
            await ws.close(code)
        except Exception:
            pass

    async def _tell(self, ws: Optional[WebSocket], message: str) -> None:
        if ws is not None:
            await self._send(ws, json.dumps({"type": "error", "message": message}, ensure_ascii=False))

    # ----------------------------------------------------------- connexions
    async def connect(self, ws: WebSocket, user_id: int, name: str) -> str:
        """Enregistre une connexion et renvoie le rôle ('host', 'player', 'spectator')."""
        self.touch()
        old: Optional[WebSocket] = None
        if user_id == self.host_id:
            old, self.host_ws = self.host_ws, ws
            role = "host"
        elif user_id in self.banned:
            raise JoinError("L'animateur t'a retiré de cette salle.", 4001)
        elif user_id in self.players:
            player = self.players[user_id]
            old, player.ws = player.ws, ws
            role = "player"
        elif self.status == "lobby":
            if len(self.players) >= MAX_PLAYERS:
                raise JoinError(f"La salle est pleine ({MAX_PLAYERS} candidats maximum).", 4409)
            self.players[user_id] = Player(user_id, name, ws=ws)
            role = "player"
        else:
            previous = self.spectators.get(user_id)
            old = previous[1] if previous else None
            self.spectators[user_id] = (name, ws)
            role = "spectator"

        if old is not None and old is not ws:
            await self._tell(old, "Tu as ouvert cette salle dans un autre onglet.")
            await self._close(old, 4000)
        await self.broadcast()
        return role

    async def disconnect(self, ws: WebSocket, user_id: int) -> None:
        self.touch()
        if user_id == self.host_id:
            if self.host_ws is ws:
                self.host_ws = None
        elif user_id in self.players:
            player = self.players[user_id]
            if player.ws is ws:
                player.ws = None
                if self.status == "lobby":
                    del self.players[user_id]   # libère la place
        elif user_id in self.spectators and self.spectators[user_id][1] is ws:
            del self.spectators[user_id]
        await self.broadcast()

    async def shutdown(self) -> None:
        sockets = [self.host_ws] + [p.ws for p in self.players.values()] + [s[1] for s in self.spectators.values()]
        for ws in sockets:
            if ws is not None:
                await self._tell(ws, "La salle a été fermée par l'animateur.")
                await self._close(ws, 4002)
        self.host_ws = None
        self.players.clear()
        self.spectators.clear()

    # --------------------------------------------------------------- actions
    async def handle(self, user_id: int, msg: dict) -> bool:
        """Traite un message du client. Renvoie True si la salle vient d'être fermée."""
        t = msg.get("type")
        if t == "answer":
            await self._answer(user_id, msg.get("choice"))
            return False
        if user_id != self.host_id:
            return False
        self.touch()

        if t == "start" and self.status == "lobby":
            if not self.players:
                await self._tell(self.host_ws, "Aucun candidat n'a encore rejoint la salle.")
                return False
            self._begin_question(0)
        elif t == "reveal" and self.status == "question":
            self._reveal()
        elif t == "next" and self.status == "reveal":
            if self.qi >= len(self.questions) - 1:
                self._finish()
            else:
                self._begin_question(self.qi + 1)
        elif t == "kick" and self.status == "lobby":
            uid = msg.get("user_id")
            player = self.players.get(uid) if isinstance(uid, int) else None
            if player is not None:
                del self.players[uid]
                self.banned.add(uid)
                await self._tell(player.ws, "L'animateur t'a retiré de la salle.")
                await self._close(player.ws, 4001)
        elif t == "reset" and self.status == "finished":
            self._reset()
        elif t == "close":
            await self.shutdown()
            return True
        else:
            return False
        await self.broadcast()
        return False

    async def _answer(self, user_id: int, choice) -> None:
        player = self.players.get(user_id)
        if player is None or not player.alive or self.status != "question":
            return
        q = self.questions[self.qi]
        if isinstance(choice, bool) or not isinstance(choice, int) or not 0 <= choice < len(q["choices"]):
            return
        if time.time() > self.deadline + ANSWER_GRACE:
            return
        player.answer = choice
        # Seuls le candidat et l'animateur ont besoin de la mise à jour.
        for uid in (user_id, self.host_id):
            ws = player.ws if uid == user_id else self.host_ws
            if ws is not None:
                await self._send(ws, json.dumps(self.snapshot(uid), ensure_ascii=False))

    def _begin_question(self, index: int) -> None:
        self.qi = index
        self.status = "question"
        self.result = None
        self.deadline = time.time() + self.timer
        for p in self.players.values():
            p.answer = None

    def _reveal(self) -> None:
        q = self.questions[self.qi]
        correct = q["correct"]
        participants = [p for p in self.players.values() if p.alive]
        counts = [0] * len(q["choices"])
        unanswered = 0
        for p in participants:
            if p.answer is None:
                unanswered += 1
            else:
                counts[p.answer] += 1
        right = [p for p in participants if p.answer == correct]
        wrong = [p for p in participants if p.answer != correct]
        is_final = self.qi == len(self.questions) - 1
        mercy = bool(participants) and not right and not is_final

        eliminated: list[str] = []
        if not mercy:
            for p in wrong:
                p.alive = False
                p.out_at = self.qi + 1
                self.pot += self.stake
                eliminated.append(p.name)

        self.result = {
            "counts": counts,
            "unanswered": unanswered,
            "participants": len(participants),
            "right": len(right),
            "eliminated": eliminated,
            "mercy": mercy,
        }
        self.status = "reveal"
        self.deadline = 0.0

    def _finish(self) -> None:
        survivors = [p for p in self.players.values() if p.alive]
        share = self.pot // len(survivors) if survivors else 0
        self.final = {
            "winners": [p.name for p in survivors],
            "share": share,
            "pot": self.pot,
            "lost": not survivors,
        }
        self.status = "finished"

    def _reset(self) -> None:
        self.status = "lobby"
        self.qi = -1
        self.pot = 0
        self.deadline = 0.0
        self.result = None
        self.final = None
        for uid in [uid for uid, p in self.players.items() if p.ws is None]:
            del self.players[uid]
        for p in self.players.values():
            p.alive = True
            p.answer = None
            p.out_at = None
        # Les spectateurs deviennent candidats s'il reste de la place.
        for uid, (name, ws) in list(self.spectators.items()):
            if len(self.players) < MAX_PLAYERS and uid not in self.banned:
                self.players[uid] = Player(uid, name, ws=ws)
                del self.spectators[uid]

    # ------------------------------------------------------------ diffusion
    def snapshot(self, viewer_id: int) -> dict:
        now = time.time()
        role = "host" if viewer_id == self.host_id else ("player" if viewer_id in self.players else "spectator")
        is_host = role == "host"
        data: dict = {
            "type": "state",
            "code": self.code,
            "role": role,
            "status": self.status,
            "host": self.host_name,
            "stake": self.stake,
            "timer": self.timer,
            "pot": self.pot,
            "max_players": MAX_PLAYERS,
            "qi": self.qi,
            "total": len(self.questions),
            "alive": self.alive_count,
            "now": int(now * 1000),
            "players": [],
        }
        for p in self.players.values():
            entry = {
                "id": p.user_id,
                "name": p.name,
                "alive": p.alive,
                "connected": p.ws is not None,
                "out_at": p.out_at,
            }
            if is_host and self.status == "question":
                entry["answered"] = p.answer is not None
            data["players"].append(entry)

        if self.status in ("question", "reveal"):
            q = self.questions[self.qi]
            qd = {
                "text": q["text"],
                "figure": q["figure"],
                "choices": q["choices"],
                "difficulty": q["difficulty"],
                "final": self.qi == len(self.questions) - 1,
            }
            # La bonne réponse n'est jamais envoyée aux candidats avant la révélation.
            if is_host or self.status == "reveal":
                qd["correct"] = q["correct"]
                qd["explanation"] = q["explanation"]
            data["question"] = qd

        if self.status == "question":
            data["deadline"] = int(self.deadline * 1000)
            if is_host:
                answered = sum(1 for p in self.players.values() if p.alive and p.answer is not None)
                data["live"] = {"answered": answered, "alive": self.alive_count}
        if self.status == "reveal":
            data["result"] = self.result
        if self.status == "finished":
            data["final"] = self.final

        me = self.players.get(viewer_id)
        if me is not None:
            data["me"] = {"alive": me.alive, "answer": me.answer, "out_at": me.out_at}
        return data

    async def broadcast(self) -> None:
        async with self._lock:
            targets: list[tuple[WebSocket, int]] = []
            if self.host_ws is not None:
                targets.append((self.host_ws, self.host_id))
            for p in self.players.values():
                if p.ws is not None:
                    targets.append((p.ws, p.user_id))
            for uid, (_, ws) in self.spectators.items():
                targets.append((ws, uid))
            payloads = [(ws, json.dumps(self.snapshot(uid), ensure_ascii=False)) for ws, uid in targets]
            await asyncio.gather(*(self._send(ws, text) for ws, text in payloads))


class GameManager:
    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}

    def count_for_host(self, host_id: int) -> int:
        return sum(1 for r in self.rooms.values() if r.host_id == host_id)

    def rooms_of(self, host_id: int) -> list[Room]:
        return [r for r in self.rooms.values() if r.host_id == host_id]

    def create(self, host_id: int, host_name: str, questions: list[dict], stake: int, timer: int) -> Room:
        if len(self.rooms) >= MAX_ROOMS:
            raise ValueError("Le serveur a atteint le nombre maximal de salles ouvertes.")
        if self.count_for_host(host_id) >= MAX_ROOMS_PER_HOST:
            raise ValueError(f"Tu as déjà {MAX_ROOMS_PER_HOST} salles ouvertes : reprends-en une ou ferme-en une.")
        code = ""
        for _ in range(100):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))
            if code not in self.rooms:
                break
        else:
            raise ValueError("Impossible de générer un code de salle, réessaie.")
        room = Room(code, host_id, host_name, questions, stake, timer)
        self.rooms[code] = room
        return room

    def get(self, code: str) -> Optional[Room]:
        return self.rooms.get(code.upper())

    def remove(self, code: str) -> None:
        self.rooms.pop(code.upper(), None)

    async def cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for code, room in list(self.rooms.items()):
                if not room.has_connections() and now - room.last_activity > IDLE_ROOM_TTL:
                    self.rooms.pop(code, None)
