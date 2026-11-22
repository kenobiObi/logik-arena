"""Test de bout en bout : démarre un vrai serveur et simule une partie à 40 joueurs.

Lancer :  python tests/e2e.py        (nécessite requirements-dev.txt)
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parent.parent
TIMER = 5
STAKE = 1000


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
WS_BASE = f"ws://127.0.0.1:{PORT}"


async def wait_for(pred, timeout=6.0, label="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"Délai dépassé : {label}")


class Client:
    def __init__(self, name: str):
        self.name = name
        self.http = httpx.AsyncClient(base_url=BASE, timeout=20)
        self.state = None
        self.errors: list[str] = []
        self.ws = None
        self.task = None
        self.close_code = None
        self.seen = 0

    async def register(self):
        r = await self.http.post("/api/register", json={"username": self.name, "password": "motdepasse123"})
        assert r.status_code == 200, (self.name, r.text)
        return r.json()["id"]

    async def open(self, code: str, origin: str = BASE):
        cookie = "; ".join(f"{k}={v}" for k, v in self.http.cookies.items())
        headers = {"Cookie": cookie} if cookie else {}
        self.state, self.close_code, self.errors, self.seen = None, None, [], 0
        self.ws = await connect(f"{WS_BASE}/ws/{code}", additional_headers=headers, origin=origin)
        self.task = asyncio.create_task(self._reader())

    async def _reader(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg["type"] == "state":
                    self.state = msg
                    self.seen += 1
                elif msg["type"] == "error":
                    self.errors.append(msg["message"])
        except Exception:
            pass
        self.close_code = self.ws.close_code

    async def send(self, **kw):
        await self.ws.send(json.dumps(kw))

    async def close(self):
        if self.ws:
            await self.ws.close()
        await self.http.aclose()


def ok(msg: str):
    print(f"  ✔ {msg}")


async def scenario():
    # ---------------------------------------------------------- comptes
    host = Client("animateur")
    players = [Client(f"joueur{i:02d}") for i in range(41)]  # le 41e doit être refusé
    spectator = Client("retardataire")
    await host.register()
    await asyncio.gather(*(c.register() for c in players + [spectator]))
    r = await host.http.post("/api/register", json={"username": "ANIMATEUR", "password": "motdepasse123"})
    assert r.status_code == 409, "le pseudo doit être unique sans tenir compte de la casse"
    r = await host.http.post("/api/register", json={"username": "x", "password": "motdepasse123"})
    assert r.status_code == 400
    r = await host.http.post("/api/login", json={"username": "animateur", "password": "faux-mot-de-passe"})
    assert r.status_code == 401
    anon = httpx.AsyncClient(base_url=BASE)
    assert (await anon.get("/api/me")).status_code == 401
    ok("comptes : inscription, pseudo unique, mauvais mot de passe refusé, accès anonyme refusé")

    # ------------------------------------------------------- questions
    r = await host.http.get("/api/questions")
    cat = r.json()
    assert len(cat["bank"]) == 37 and all("correct" not in q for q in cat["bank"]), "la banque ne doit pas exposer les réponses"
    r = await host.http.post("/api/questions", json={
        "text": "Combien font 2 + 2 ?", "figure": "🍎🍎 + 🍎🍎", "choices": ["3", "4"], "correct": 1,
        "explanation": "Quatre.", "difficulty": 50})
    assert r.status_code == 200
    custom_id = r.json()["id"]
    bad = await host.http.post("/api/questions", json={"text": "Question ?", "choices": ["a", "a"], "correct": 0})
    assert bad.status_code == 400
    bad = await host.http.post("/api/questions", json={"text": "Question ?", "choices": ["a", "b"], "correct": 5})
    assert bad.status_code == 400
    mine = (await host.http.get("/api/questions")).json()["mine"]
    assert len(mine) == 1 and mine[0]["correct"] == 1
    ids = (await host.http.get("/api/playlist/auto", params={"count": 4})).json()["question_ids"]
    assert len(ids) == 4 and len(set(ids)) == 4
    diffs = {q["id"]: q["difficulty"] for q in cat["bank"]}
    seq = [diffs[i] for i in ids]
    assert seq == sorted(seq, reverse=True), seq
    ok(f"questions : banque sans réponses, question perso, validation, playlist auto triée {seq}")

    # ----------------------------------------------------------- salle
    playlist = ids + [custom_id]
    r = await players[0].http.post("/api/rooms", json={"question_ids": playlist, "stake": STAKE, "timer": TIMER})
    assert r.status_code == 400, "on ne peut pas utiliser la question perso d'un autre"
    for body in ({"question_ids": playlist, "stake": 0, "timer": TIMER},
                 {"question_ids": playlist, "stake": STAKE, "timer": 1},
                 {"question_ids": [], "stake": STAKE, "timer": TIMER},
                 {"question_ids": [ids[0], ids[0]], "stake": STAKE, "timer": TIMER}):
        assert (await host.http.post("/api/rooms", json=body)).status_code == 400, body
    r = await host.http.post("/api/rooms", json={"question_ids": playlist, "stake": STAKE, "timer": TIMER})
    assert r.status_code == 200
    code = r.json()["code"]
    assert len(code) == 5
    info = (await players[0].http.get(f"/api/rooms/{code.lower()}")).json()
    assert info["max_players"] == 40 and info["status"] == "lobby"
    assert (await players[0].http.get("/api/rooms/ZZZZZ")).status_code == 404
    ok(f"salle {code} créée ; mauvais paramètres et question d'autrui refusés")

    # ------------------------------------------------------ connexions
    await host.open(code)
    for c in players[:40]:
        await c.open(code)
    await wait_for(lambda: host.state and len(host.state["players"]) == 40, label="40 joueurs dans le lobby")
    await players[40].open(code)
    await wait_for(lambda: players[40].close_code is not None, label="refus du 41e joueur")
    assert players[40].close_code == 4409 and "pleine" in players[40].errors[0]
    assert len(host.state["players"]) == 40
    assert host.state["role"] == "host" and players[0].state["role"] == "player"
    ok("40 joueurs max : le 41e est refusé (code 4409)")

    bad_origin = Client("x")
    bad_origin.http.cookies = host.http.cookies
    await bad_origin.open(code, origin="http://evil.example")
    await wait_for(lambda: bad_origin.close_code is not None, label="origine refusée")
    assert bad_origin.close_code == 4403
    nocookie = Client("y")
    await nocookie.open(code)
    await wait_for(lambda: nocookie.close_code is not None, label="non connecté")
    assert nocookie.close_code == 4401
    nf = Client("z")
    nf.http.cookies = host.http.cookies
    await nf.open("QQQQQ")
    await wait_for(lambda: nf.close_code is not None, label="salle introuvable")
    assert nf.close_code == 4404
    ok("WebSocket : origine étrangère (4403), non connecté (4401), salle inconnue (4404) refusés")

    # --------------------------------------------------------- question 1
    await players[0].send(type="start")          # un candidat ne peut pas lancer la partie
    await asyncio.sleep(0.3)
    assert host.state["status"] == "lobby"
    await host.send(type="start")
    await wait_for(lambda: all(c.state["status"] == "question" for c in players[:40]), label="question 1")
    q = players[0].state["question"]
    assert "correct" not in q and "explanation" not in q, "fuite de la bonne réponse vers les candidats"
    assert "correct" in host.state["question"]
    assert host.state["qi"] == 0 and host.state["total"] == 5
    correct = host.state["question"]["correct"]
    wrong = (correct + 1) % len(q["choices"])
    for i, c in enumerate(players[:40]):
        if i < 30:
            await c.send(type="answer", choice=correct)
        elif i < 35:
            await c.send(type="answer", choice=wrong)
        # 35..39 : pas de réponse
    await players[1].send(type="answer", choice=99)        # indice invalide ignoré
    await players[2].send(type="answer", choice="1")       # type invalide ignoré
    await wait_for(lambda: host.state["live"]["answered"] == 35, label="35 réponses côté animateur")
    await players[0].send(type="reveal")                   # réservé à l'animateur
    await asyncio.sleep(0.2)
    assert host.state["status"] == "question"
    await host.send(type="reveal")
    await wait_for(lambda: players[0].state["status"] == "reveal", label="révélation 1")
    res = players[0].state["result"]
    assert res["eliminated"] and len(res["eliminated"]) == 10 and res["unanswered"] == 5
    assert res["right"] == 30 and sum(res["counts"]) == 35 and not res["mercy"]
    assert players[0].state["pot"] == 10 * STAKE and players[0].state["alive"] == 30
    assert players[0].state["question"]["correct"] == correct and players[0].state["me"]["alive"]
    assert not players[39].state["me"]["alive"] and players[39].state["me"]["out_at"] == 1
    ok("Q1 : 30 bonnes réponses, 10 éliminés (5 erreurs + 5 absents), cagnotte 10 000 €, pas de fuite de réponse")

    # --------------------------------------------------------- question 2 : clémence
    await host.send(type="next")
    await wait_for(lambda: players[0].state["status"] == "question" and players[0].state["qi"] == 1, label="question 2")
    # reconnexion d'un candidat en cours de partie
    await players[5].ws.close()
    await asyncio.sleep(0.2)
    await players[5].open(code)
    await wait_for(lambda: players[5].state and players[5].state["status"] == "question", label="reconnexion")
    assert players[5].state["me"]["alive"] and players[5].state["role"] == "player"
    # un candidat éliminé ne peut plus répondre
    correct = host.state["question"]["correct"]
    wrong = (correct + 1) % len(host.state["question"]["choices"])
    await players[39].send(type="answer", choice=correct)
    for c in players[:30]:
        await c.send(type="answer", choice=wrong)
    await wait_for(lambda: host.state["live"]["answered"] == 30, label="30 réponses fausses")
    assert players[39].state["me"]["answer"] is None
    # un spectateur arrive en cours de partie
    await spectator.open(code)
    await wait_for(lambda: spectator.state is not None, label="spectateur")
    assert spectator.state["role"] == "spectator" and "correct" not in spectator.state["question"]
    await host.send(type="reveal")
    await wait_for(lambda: players[0].state["status"] == "reveal", label="révélation 2")
    res = players[0].state["result"]
    assert res["mercy"] and res["eliminated"] == [] and players[0].state["alive"] == 30
    assert players[0].state["pot"] == 10 * STAKE
    ok("Q2 : tout le monde se trompe -> personne n'est éliminé ; reconnexion et spectateur OK")

    # --------------------------------------------------------- question 3 : temps écoulé
    await host.send(type="next")
    await wait_for(lambda: players[0].state["status"] == "question" and players[0].state["qi"] == 2, label="question 3")
    correct = host.state["question"]["correct"]
    wrong = (correct + 1) % len(host.state["question"]["choices"])
    for c in players[:15]:
        await c.send(type="answer", choice=correct)
    await wait_for(lambda: host.state["live"]["answered"] == 15, label="15 bonnes réponses")
    await players[7].send(type="answer", choice=wrong)      # changer de réponse avant la fin est permis
    await players[7].send(type="answer", choice=correct)
    await wait_for(lambda: players[7].state["me"]["answer"] == correct, label="changement de réponse")
    await asyncio.sleep(max(0.0, host.state["deadline"] / 1000 - time.time()) + 0.9)
    for c in players[15:30]:
        await c.send(type="answer", choice=correct)         # trop tard : ignoré
    await asyncio.sleep(0.4)
    assert host.state["live"]["answered"] == 15, "les réponses tardives doivent être refusées"
    await host.send(type="reveal")
    await wait_for(lambda: players[0].state["status"] == "reveal", label="révélation 3")
    assert players[0].state["alive"] == 15 and players[0].state["pot"] == 25 * STAKE
    ok("Q3 : réponses tardives refusées, 15 éliminés, cagnotte 25 000 €")

    # --------------------------------------------------------- question 4 : tous justes
    await host.send(type="next")
    await wait_for(lambda: players[0].state["status"] == "question" and players[0].state["qi"] == 3, label="question 4")
    correct = host.state["question"]["correct"]
    for c in players[:15]:
        await c.send(type="answer", choice=correct)
    await wait_for(lambda: host.state["live"]["answered"] == 15, label="15 réponses")
    await host.send(type="reveal")
    await wait_for(lambda: players[0].state["status"] == "reveal", label="révélation 4")
    assert players[0].state["alive"] == 15 and not players[0].state["question"]["final"]

    # --------------------------------------------------------- question 5 : ultime
    await host.send(type="next")
    await wait_for(lambda: players[0].state["status"] == "question" and players[0].state["qi"] == 4, label="question ultime")
    assert players[0].state["question"]["final"] and players[0].state["question"]["figure"] == "🍎🍎 + 🍎🍎"
    for i, c in enumerate(players[:15]):
        await c.send(type="answer", choice=1 if i < 10 else 0)
    await wait_for(lambda: host.state["live"]["answered"] == 15, label="15 réponses finales")
    await host.send(type="reveal")
    await wait_for(lambda: players[0].state["status"] == "reveal", label="révélation finale")
    assert players[0].state["alive"] == 10 and players[0].state["pot"] == 30 * STAKE
    await host.send(type="next")
    await wait_for(lambda: players[0].state["status"] == "finished", label="fin de partie")
    f = players[0].state["final"]
    assert len(f["winners"]) == 10 and f["share"] == 3000 and f["pot"] == 30000 and not f["lost"]
    ok("Q5 (ultime) : 10 gagnants se partagent 30 000 € -> 3 000 € chacun")

    # ------------------------------------------------------------ rejouer
    await host.send(type="reset")
    await wait_for(lambda: players[0].state["status"] == "lobby", label="retour au lobby")
    assert players[0].state["pot"] == 0 and players[0].state["alive"] == 40 and len(players[0].state["players"]) == 40
    assert all(p["alive"] for p in players[0].state["players"])
    assert spectator.state["role"] == "spectator", "salle pleine : le spectateur reste spectateur"
    ok("Rejouer : retour au lobby, tout le monde de nouveau en lice, cagnotte remise à zéro")

    # ------------------------------------------------- exclusion + fermeture
    await players[3].send(type="answer", choice=0)      # hors question : ignoré
    await host.send(type="kick", user_id=players[3].state["players"][3]["id"])
    await wait_for(lambda: players[3].close_code == 4001, label="exclusion")
    await wait_for(lambda: len(host.state["players"]) == 39, label="39 joueurs")
    await players[3].open(code)
    await wait_for(lambda: players[3].close_code == 4001, label="ré-exclusion")
    await spectator.send(type="close")                  # ignoré (pas l'animateur)
    await asyncio.sleep(0.2)
    assert (await host.http.get(f"/api/rooms/{code}")).status_code == 200
    mine = (await host.http.get("/api/rooms/mine")).json()["rooms"]
    assert [r["code"] for r in mine] == [code]
    await host.send(type="close")
    await wait_for(lambda: players[0].close_code == 4002 and host.close_code is not None, label="fermeture")
    assert (await host.http.get(f"/api/rooms/{code}")).status_code == 404
    ok("Exclusion (4001, non réversible), fermeture de salle (4002) réservée à l'animateur")

    # ------------------------------------------------- limite de salles par hôte
    codes = []
    for _ in range(3):
        r = await host.http.post("/api/rooms", json={"question_ids": ids[:1], "stake": 10, "timer": 10})
        assert r.status_code == 200
        codes.append(r.json()["code"])
    r = await host.http.post("/api/rooms", json={"question_ids": ids[:1], "stake": 10, "timer": 10})
    assert r.status_code == 400 and "3 salles" in r.json()["detail"]
    ok("Maximum 3 salles ouvertes par animateur")

    # ------------------------------------------------------ déconnexion
    await host.http.post("/api/logout")
    assert (await host.http.get("/api/me")).status_code == 401
    ok("Déconnexion : la session est invalidée côté serveur")

    for c in players + [spectator, host, bad_origin, nocookie, nf]:
        try:
            await c.close()
        except Exception:
            pass
    await anon.aclose()


def main():
    tmp = tempfile.mkdtemp()
    env = dict(os.environ, DATABASE_PATH=os.path.join(tmp, "test.db"), AUTH_RATE_MAX="10000")
    py = sys.executable
    server = subprocess.Popen(
        [py, "-m", "uvicorn", "app.main:app", "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT, env=env,
    )
    try:
        for _ in range(100):
            try:
                if httpx.get(BASE + "/", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise SystemExit("Le serveur ne démarre pas.")
        print("Serveur de test démarré, simulation de la partie…")
        asyncio.run(scenario())
        print("\nTous les tests de bout en bout ont réussi.")
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    main()
