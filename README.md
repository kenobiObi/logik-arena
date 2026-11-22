# Logik Arena

Quiz de logique multijoueur en temps réel : **un animateur** crée une salle, **jusqu'à 40 candidats** la rejoignent avec un code. Chaque erreur élimine un candidat et sa mise rejoint la cagnotte, que se partagent les survivants de la question ultime.

Stack : Python (FastAPI + WebSockets), SQLite, front-end en JavaScript pur (aucun build).

## Règles du jeu

- Un animateur (compte requis) choisit ses questions (banque de 37 questions + les siennes), la mise par candidat (1 000 € par défaut) et le temps par question.
- Les candidats (compte requis) rejoignent avec le code à 5 caractères. Maximum **40 candidats** par salle ; les arrivants tardifs regardent en spectateurs.
- L'animateur lance la partie, puis pilote : *Révéler la réponse*, *Question suivante*. Il voit la bonne réponse pendant la question, les candidats non.
- Une mauvaise réponse ou une absence de réponse élimine ; la mise du candidat est ajoutée à la cagnotte.
- Si **tous** les candidats en lice se trompent à une question intermédiaire, personne n'est éliminé.
- La **dernière question** de la liste est la question ultime : les survivants qui la réussissent se partagent la cagnotte à parts égales. Si personne ne la réussit, la cagnotte est perdue.
- Une réponse peut être changée jusqu'à la fin du temps. Après la fin du temps, le serveur refuse les réponses.
- Fin de partie : l'animateur peut rejouer avec les mêmes questions ou fermer la salle.

## Lancer en local

```bash
cd logik-arena
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Ouvre http://localhost:8000. Pour tester à plusieurs sur le même réseau Wi-Fi : `uvicorn app.main:app --host 0.0.0.0 --port 8000`, puis ouvre le port 8000 dans le pare-feu (`sudo firewall-cmd --add-port=8000/tcp`) et utilise l'IP de ta machine.

## Structure

```
app/main.py            API REST, WebSocket, sécurité des requêtes
app/game.py            moteur de jeu (salles, éliminations, cagnotte)
app/db.py              SQLite : comptes, sessions, questions
app/security.py        scrypt (mots de passe) + limiteur de requêtes
app/data/questions.json  banque de questions (modifiable)
static/                index.html, style.css, app.js
tests/                 test_bank.py, e2e.py, ui_smoke.js
```

## Ajouter des questions à la banque

Édite `app/data/questions.json` (clé unique `key`, `text`, `choices` de 2 à 4 réponses, `correct` = index de la bonne réponse à partir de 0, `explanation`, `difficulty` = % estimé de bonnes réponses). Le fichier est relu à chaque démarrage. Ajoute ensuite la réponse attendue dans `tests/test_bank.py` : le test recalcule chaque réponse pour éviter les erreurs.

Les animateurs peuvent aussi écrire leurs propres questions depuis le site (page « Mes questions »).

## Tests

```bash
pip install -r requirements-dev.txt
python tests/test_bank.py      # vérifie les 37 questions (réponses recalculées)
python tests/e2e.py            # vrai serveur + 40 joueurs simulés en WebSocket
```

Test d'interface (facultatif, jsdom) : lance le serveur avec une base vide, puis
`npm install jsdom` et `BASE_URL=http://127.0.0.1:8000 NODE_PATH=./node_modules node tests/ui_smoke.js`.

## Variables d'environnement

| Variable | Rôle | Défaut |
|---|---|---|
| `DATABASE_PATH` | fichier SQLite | `data/game.db` |
| `TRUST_PROXY` | `1` derrière Nginx/Render : lit `X-Forwarded-For` pour limiter les tentatives de connexion par IP | désactivé |
| `ALLOWED_ORIGINS` | origines WebSocket supplémentaires autorisées, séparées par des virgules | aucune (même origine uniquement) |
| `AUTH_RATE_MAX` | tentatives de connexion/inscription par IP et par 10 min | `20` |

## Déploiement

**Important : lance toujours un seul worker** (`--workers 1`). Les salles vivent en mémoire du processus ; plusieurs workers les séparent. Un seul worker suffit largement pour des dizaines de salles. Un redémarrage du serveur ferme les salles en cours (les comptes et questions restent en base).

### VPS (Ubuntu/Debian, Nginx, HTTPS)

```bash
sudo apt install python3-venv nginx certbot python3-certbot-nginx
sudo useradd -r -m -d /opt/logik-arena logik
# copie le projet dans /opt/logik-arena puis :
cd /opt/logik-arena && sudo -u logik python3 -m venv .venv && sudo -u logik .venv/bin/pip install -r requirements.txt
```

`/etc/systemd/system/logik-arena.service` :

```ini
[Unit]
Description=Logik Arena
After=network.target

[Service]
User=logik
WorkingDirectory=/opt/logik-arena
StateDirectory=logik-arena
Environment=DATABASE_PATH=/var/lib/logik-arena/game.db
Environment=TRUST_PROXY=1
ExecStart=/opt/logik-arena/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always

[Install]
WantedBy=multi-user.target
```

`/etc/nginx/sites-available/logik-arena` (les en-têtes `Upgrade`/`Connection` sont indispensables aux WebSockets) :

```nginx
server {
    server_name quiz.exemple.com;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/logik-arena /etc/nginx/sites-enabled/
sudo systemctl enable --now logik-arena
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d quiz.exemple.com
```

### Docker

```bash
docker build -t logik-arena .
docker run -d -p 8000:8000 -v logik-data:/data -e DATABASE_PATH=/data/game.db logik-arena
```

### Render

Service web Python : build `pip install -r requirements.txt`, start `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`, variable `TRUST_PROXY=1`. Attention : sans disque persistant, la base SQLite est effacée à chaque déploiement (les comptes disparaissent) ; ajoute un disque et `DATABASE_PATH` dessus. Une instance gratuite qui se met en veille coupe aussi les parties en cours : un VPS est plus fiable pour jouer en direct.

## Sécurité (déjà en place)

Mots de passe hachés avec scrypt, sessions aléatoires stockées sous forme de hachage, cookie `HttpOnly` + `SameSite=Lax` (+ `Secure` en HTTPS), limitation des tentatives de connexion, vérification de l'origine des WebSockets, en-têtes CSP, aucune réponse envoyée aux candidats avant la révélation, aucun HTML injecté (tout le texte passe par `createTextNode`).

## Nom et marque

« Logik Arena » est un nom neutre. Si tu mets le site en ligne publiquement, évite le nom, le logo et les visuels de l'émission télévisée qui a inspiré le principe.
