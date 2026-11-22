"use strict";

/* ===================================================================
   Logik Arena – interface (JavaScript pur, aucun framework)
   Tout le texte dynamique passe par createTextNode : pas d'injection HTML.
   =================================================================== */

const $app = document.getElementById("app");
const $toasts = document.getElementById("toasts");
const $banner = document.getElementById("banner");
const LETTERS = ["A", "B", "C", "D"];
const FATAL_CODES = new Set([4000, 4001, 4002, 4401, 4403, 4404, 4409]);
const eur = new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });

let me = null;
let ws = null;
let state = null;
let clockOffset = 0;
let tickTimer = null;
let retryTimer = null;
let lastPhase = "";

/* ------------------------------------------------------------ utilitaires */
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "value") el.value = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function mount(...nodes) {
  $app.replaceChildren(...nodes.filter(Boolean));
}

function toast(message, kind = "info") {
  const el = h("div", { class: `toast ${kind}`, role: "status" }, message);
  $toasts.append(el);
  setTimeout(() => el.remove(), 4200);
}

function setBanner(text) {
  $banner.hidden = !text;
  $banner.textContent = text || "";
}

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* réponse vide */ }
  if (!res.ok) {
    let msg = "Une erreur est survenue. Réessaie.";
    if (data && typeof data.detail === "string") msg = data.detail;
    else if (res.status === 422) msg = "Certaines données sont invalides.";
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

function plural(n, one, many) {
  return `${n} ${n > 1 ? many : one}`;
}

/* ----------------------------------------------------------------- routeur */
function route() {
  closeSocket();
  window.scrollTo(0, 0);
  if (!me) return renderAuth("login");
  const hash = location.hash || "#/";
  const m = hash.match(/^#\/room\/([A-Za-z0-9]{3,8})$/);
  if (m) return enterRoom(m[1].toUpperCase());
  if (hash === "#/setup") return renderSetup();
  if (hash === "#/questions") return renderQuestions();
  return renderHome();
}

function topbar(title) {
  return h("header", { class: "topbar" },
    h("a", { class: "brand", href: "#/" }, h("span", { class: "dot" }), "Logik Arena"),
    title ? h("span", { class: "topbar-title" }, title) : null,
    me ? h("div", { class: "userbox" },
      h("span", {}, me.username),
      h("button", { class: "btn small ghost", type: "button", onclick: logout }, "Se déconnecter")) : null);
}

async function logout() {
  try { await api("/api/logout", { method: "POST" }); } catch (_) { /* déjà déconnecté */ }
  me = null;
  go("#/");
}

/* --------------------------------------------------------------- connexion */
function renderAuth(mode) {
  const isLogin = mode === "login";
  const user = h("input", { id: "f-user", name: "username", autocomplete: "username", maxlength: "20", required: true });
  const pass = h("input", {
    id: "f-pass", name: "password", type: "password", required: true,
    autocomplete: isLogin ? "current-password" : "new-password",
  });
  const err = h("p", { class: "form-error", role: "alert" });

  const submit = async (ev) => {
    ev.preventDefault();
    err.textContent = "";
    try {
      me = await api(isLogin ? "/api/login" : "/api/register", {
        method: "POST", body: { username: user.value, password: pass.value },
      });
      go(location.hash || "#/");
    } catch (e) {
      err.textContent = e.message;
    }
  };

  mount(h("section", { class: "auth" },
    h("a", { class: "brand", href: "#/" }, h("span", { class: "dot" }), "Logik Arena"),
    h("h1", {}, "Le quiz de logique à 40 candidats"),
    h("p", { class: "lead" }, "Un animateur lance les questions, chaque erreur vous élimine, la cagnotte grossit."),
    h("form", { class: "card", onsubmit: submit },
      h("h2", {}, isLogin ? "Connexion" : "Créer un compte"),
      h("label", { class: "field", for: "f-user" }, h("span", {}, "Pseudo"), user,
        isLogin ? null : h("div", { class: "hint" }, "3 à 20 caractères : lettres, chiffres, _ . -")),
      h("label", { class: "field", for: "f-pass" }, h("span", {}, "Mot de passe"), pass,
        isLogin ? null : h("div", { class: "hint" }, "8 caractères minimum")),
      err,
      h("button", { class: "btn primary big", type: "submit" }, isLogin ? "Se connecter" : "Créer mon compte"),
      h("p", { class: "auth-switch" },
        isLogin ? "Pas encore de compte ? " : "Déjà un compte ? ",
        h("button", { class: "linklike", type: "button", onclick: () => renderAuth(isLogin ? "register" : "login") },
          isLogin ? "Créer un compte" : "Se connecter")))));
  user.focus();
}

/* ------------------------------------------------------------------ accueil */
function renderHome() {
  const codeInput = h("input", {
    class: "code-input", maxlength: "8", placeholder: "CODE", autocomplete: "off",
    autocapitalize: "characters", spellcheck: "false", "aria-label": "Code de la salle",
  });
  const joinForm = h("form", {
    class: "join-form",
    onsubmit: async (ev) => {
      ev.preventDefault();
      const code = codeInput.value.trim().toUpperCase();
      if (!code) return;
      try {
        await api("/api/rooms/" + encodeURIComponent(code));
        go("#/room/" + code);
      } catch (e) {
        toast(e.message, "bad");
      }
    },
  }, codeInput, h("button", { class: "btn primary", type: "submit" }, "Rejoindre"));

  const roomsBox = h("div", { class: "my-rooms" });

  mount(topbar(), h("div", { class: "wrap" },
    h("h1", {}, `Salut ${me.username}`),
    h("div", { class: "home-grid" },
      h("section", { class: "card" },
        h("h2", {}, "Rejoindre une partie"),
        h("p", { class: "muted" }, "Entre le code donné par l'animateur."),
        joinForm),
      h("section", { class: "card" },
        h("h2", {}, "Animer une partie"),
        h("p", { class: "muted" }, "Choisis les questions, crée la salle et pilote la partie en direct."),
        h("a", { class: "btn primary", href: "#/setup" }, "Préparer une partie"),
        roomsBox),
      h("section", { class: "card" },
        h("h2", {}, "Mes questions"),
        h("p", { class: "muted" }, "Écris tes propres questions de logique et réutilise-les dans tes parties."),
        h("a", { class: "btn", href: "#/questions" }, "Gérer mes questions")))));

  api("/api/rooms/mine").then((data) => {
    if (!roomsBox.isConnected || !data.rooms.length) return;
    roomsBox.append(h("h3", {}, "Tes salles ouvertes"));
    for (const r of data.rooms) {
      roomsBox.append(h("div", { class: "my-room" },
        h("span", {}, h("strong", {}, r.code), ` · ${plural(r.players, "candidat", "candidats")}`),
        h("a", { class: "btn small", href: "#/room/" + r.code }, "Reprendre")));
    }
  }).catch(() => {});
}

/* ------------------------------------------------------ préparation d'une partie */
async function renderSetup() {
  mount(topbar("Préparer une partie"), h("p", { class: "center-note" }, "Chargement des questions…"));
  let cat;
  try {
    cat = await api("/api/questions");
  } catch (e) {
    toast(e.message, "bad");
    return go("#/");
  }
  const all = [...cat.mine, ...cat.bank];
  const byId = new Map(all.map((q) => [q.id, q]));
  let playlist = [];
  let filter = "all";
  let stake = 1000;
  let timer = 30;
  let autoCount = 10;
  let leftScroll = 0;
  const root = h("div", { class: "wrap" });
  mount(topbar("Préparer une partie"), root);

  const numInput = (value, min, max, onChange, label) => h("input", {
    type: "number", value, min, max, step: "1", "aria-label": label,
    oninput: (ev) => onChange(parseInt(ev.target.value, 10) || 0),
  });

  async function autoGenerate() {
    try {
      const r = await api("/api/playlist/auto?count=" + autoCount);
      playlist = r.question_ids;
      draw();
    } catch (e) { toast(e.message, "bad"); }
  }

  async function createRoom() {
    try {
      const r = await api("/api/rooms", { method: "POST", body: { question_ids: playlist, stake, timer } });
      go("#/room/" + r.code);
    } catch (e) { toast(e.message, "bad"); }
  }

  function move(i, delta) {
    const j = i + delta;
    if (j < 0 || j >= playlist.length) return;
    [playlist[i], playlist[j]] = [playlist[j], playlist[i]];
    draw();
  }

  function draw() {
    const prev = root.querySelector(".avail-list");
    if (prev) leftScroll = prev.scrollTop;
    const inList = new Set(playlist);
    const available = all.filter((q) => filter === "all" || (filter === "bank" ? !q.mine : q.mine));

    const filters = h("div", { class: "filters" },
      [["all", "Toutes"], ["bank", "Banque"], ["mine", "Mes questions"]].map(([key, label]) =>
        h("button", {
          class: "btn small ghost" + (filter === key ? " active" : ""), type: "button",
          onclick: () => { filter = key; draw(); },
        }, label)));

    const availList = h("ul", { class: "list avail-list" },
      available.length ? available.map((q) => h("li", { class: "qrow" },
        h("span", { class: "diff", title: "Difficulté estimée : % de réussite" }, `${q.difficulty} %`),
        h("span", { class: "qtext" }, q.text),
        q.mine ? h("span", { class: "tag" }, "à moi") : null,
        h("button", {
          class: "btn small", type: "button", disabled: inList.has(q.id),
          onclick: () => { playlist.push(q.id); draw(); },
        }, inList.has(q.id) ? "Ajoutée" : "Ajouter")))
        : h("li", { class: "hint" }, "Aucune question dans cette liste. Écris-en dans « Mes questions »."));

    const playList = h("ol", { class: "list" },
      playlist.length ? playlist.map((id, i) => {
        const q = byId.get(id);
        return h("li", { class: "qrow" },
          h("span", { class: "pos" }, `Q${i + 1}`),
          h("span", { class: "qtext" }, q.text,
            h("small", {}, `Difficulté estimée : ${q.difficulty} %`)),
          i === playlist.length - 1 ? h("span", { class: "tag gold" }, "ultime") : null,
          h("span", { class: "row-actions" },
            h("button", { class: "btn small", type: "button", "aria-label": "Monter", disabled: i === 0, onclick: () => move(i, -1) }, "▲"),
            h("button", { class: "btn small", type: "button", "aria-label": "Descendre", disabled: i === playlist.length - 1, onclick: () => move(i, 1) }, "▼"),
            h("button", { class: "btn small", type: "button", "aria-label": "Retirer", onclick: () => { playlist.splice(i, 1); draw(); } }, "✕")));
      }) : h("li", { class: "hint" }, "Ajoute des questions ou génère une partie automatique."));

    root.replaceChildren(
      h("h1", {}, "Préparer une partie"),
      h("div", { class: "setup-grid" },
        h("section", { class: "card" },
          h("h2", {}, "Questions disponibles"),
          filters, availList),
        h("section", { class: "card" },
          h("h2", {}, `Ma partie (${plural(playlist.length, "question", "questions")})`),
          h("p", { class: "hint" }, "La dernière question est la question ultime : les survivants qui la réussissent se partagent la cagnotte."),
          h("div", { class: "inline-form" },
            h("label", { class: "field" }, h("span", {}, "Partie automatique"),
              numInput(autoCount, 3, 30, (v) => { autoCount = v; }, "Nombre de questions")),
            h("button", { class: "btn", type: "button", onclick: autoGenerate }, "Générer")),
          playList,
          h("div", { class: "settings" },
            h("label", { class: "field" }, h("span", {}, "Mise par candidat (€)"),
              numInput(stake, 1, 100000, (v) => { stake = v; }, "Mise par candidat")),
            h("label", { class: "field" }, h("span", {}, "Secondes par question"),
              numInput(timer, 5, 120, (v) => { timer = v; }, "Secondes par question"))),
          h("button", { class: "btn primary big", type: "button", disabled: playlist.length === 0, onclick: createRoom }, "Créer la salle"))));
    const list = root.querySelector(".avail-list");
    if (list) list.scrollTop = leftScroll;
  }
  draw();
}

/* ---------------------------------------------------------- mes questions */
async function renderQuestions() {
  mount(topbar("Mes questions"), h("p", { class: "center-note" }, "Chargement…"));
  let cat;
  try {
    cat = await api("/api/questions");
  } catch (e) {
    toast(e.message, "bad");
    return go("#/");
  }

  const text = h("textarea", { rows: "3", maxlength: "300", required: true, id: "q-text" });
  const figure = h("textarea", { class: "mono", rows: "3", maxlength: "400", id: "q-fig" });
  const radios = [];
  const inputs = [];
  const rows = [0, 1, 2, 3].map((i) => {
    const radio = h("input", { type: "radio", name: "correct", value: i, "aria-label": `La réponse ${LETTERS[i]} est la bonne` });
    const input = h("input", { maxlength: "80", placeholder: `Réponse ${LETTERS[i]}${i < 2 ? "" : " (facultative)"}`, "aria-label": `Réponse ${LETTERS[i]}` });
    radios.push(radio);
    inputs.push(input);
    return h("div", { class: "choice-input" }, radio, input);
  });
  radios[0].checked = true;
  const explanation = h("input", { maxlength: "300", id: "q-exp" });
  const difficulty = h("input", { type: "number", value: 50, min: "1", max: "99", id: "q-diff" });
  const err = h("p", { class: "form-error", role: "alert" });

  const submit = async (ev) => {
    ev.preventDefault();
    err.textContent = "";
    const checked = radios.findIndex((r) => r.checked);
    const filled = inputs.map((inp, i) => ({ i, v: inp.value.trim() })).filter((x) => x.v);
    const correct = filled.findIndex((x) => x.i === checked);
    if (filled.length < 2) { err.textContent = "Écris au moins deux réponses."; return; }
    if (correct < 0) { err.textContent = "La bonne réponse doit être l'une des réponses renseignées."; return; }
    try {
      await api("/api/questions", {
        method: "POST",
        body: {
          text: text.value, figure: figure.value, choices: filled.map((x) => x.v), correct,
          explanation: explanation.value, difficulty: parseInt(difficulty.value, 10) || 50,
        },
      });
      toast("Question ajoutée.", "ok");
      renderQuestions();
    } catch (e) {
      err.textContent = e.message;
    }
  };

  const remove = async (q) => {
    if (!window.confirm("Supprimer cette question ?")) return;
    try {
      await api("/api/questions/" + q.id, { method: "DELETE" });
      toast("Question supprimée.", "ok");
      renderQuestions();
    } catch (e) { toast(e.message, "bad"); }
  };

  mount(topbar("Mes questions"), h("div", { class: "wrap" },
    h("h1", {}, "Mes questions"),
    h("div", { class: "setup-grid" },
      h("form", { class: "card", onsubmit: submit },
        h("h2", {}, "Nouvelle question"),
        h("label", { class: "field", for: "q-text" }, h("span", {}, "Énoncé"), text),
        h("label", { class: "field", for: "q-fig" }, h("span", {}, "Figure ou suite (facultatif)"), figure,
          h("div", { class: "hint" }, "Texte brut ou émojis, affiché en police à chasse fixe.")),
        h("fieldset", { class: "field", style: "border:0;padding:0;margin:0 0 14px" },
          h("span", { style: "display:block;font-weight:700;margin-bottom:4px" }, "Réponses (coche la bonne)"),
          h("div", { class: "choice-inputs" }, rows)),
        h("label", { class: "field", for: "q-exp" }, h("span", {}, "Explication (facultatif)"), explanation),
        h("label", { class: "field", for: "q-diff" }, h("span", {}, "Difficulté estimée (% de bonnes réponses)"), difficulty),
        err,
        h("button", { class: "btn primary", type: "submit" }, "Ajouter la question")),
      h("section", { class: "card" },
        h("h2", {}, `Mes questions (${cat.mine.length})`),
        h("ul", { class: "list avail-list" },
          cat.mine.length ? cat.mine.map((q) => h("li", { class: "qrow" },
            h("span", { class: "diff" }, `${q.difficulty} %`),
            h("span", { class: "qtext" }, q.text,
              h("small", {}, `Bonne réponse : ${q.choices[q.correct]}`)),
            h("button", { class: "btn small ghost", type: "button", onclick: () => remove(q) }, "Supprimer")))
            : h("li", { class: "hint" }, "Tu n'as pas encore écrit de question."))))));
}

/* ---------------------------------------------------------------- la salle */
function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function closeSocket() {
  lastPhase = "";
  clearTimeout(retryTimer);
  clearInterval(tickTimer);
  setBanner("");
  if (ws) {
    const s = ws;
    ws = null;
    s.onclose = null;
    s.onmessage = null;
    try { s.close(); } catch (_) { /* déjà fermé */ }
  }
  state = null;
}

function enterRoom(code) {
  mount(topbar(`Salle ${code}`), h("p", { class: "center-note" }, "Connexion à la salle…"));
  openSocket(code);
}

function openSocket(code) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const sock = new WebSocket(`${proto}://${location.host}/ws/${code}`);
  ws = sock;
  let pingTimer = null;

  sock.onopen = () => {
    setBanner("");
    pingTimer = setInterval(() => send({ type: "ping" }), 25000);
  };
  sock.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === "state") {
      state = msg;
      clockOffset = msg.now - Date.now();
      renderRoom();
    } else if (msg.type === "error") {
      toast(msg.message, "bad");
    }
  };
  sock.onclose = (ev) => {
    clearInterval(pingTimer);
    if (ws !== sock) return;
    ws = null;
    if (FATAL_CODES.has(ev.code)) {
      if (ev.code === 4401) me = null;
      go("#/");
      return;
    }
    setBanner("Connexion perdue, reconnexion en cours…");
    retryTimer = setTimeout(() => openSocket(code), 1500);
  };
}

async function copyLink(code) {
  const link = `${location.origin}/#/room/${code}`;
  try {
    await navigator.clipboard.writeText(link);
    toast("Lien copié.", "ok");
  } catch (_) {
    toast(`Copie impossible. Partage ce lien : ${link}`, "bad");
  }
}

function closeRoom() {
  if (window.confirm("Fermer la salle pour tout le monde ?")) send({ type: "close" });
}

function stat(label, value, cls) {
  return h("div", { class: `stat ${cls || ""}` },
    h("span", { class: "stat-label" }, label),
    h("strong", { class: "stat-value" }, value));
}

function renderRoom() {
  if (!state) return;
  const s = state;
  const isHost = s.role === "host";
  let body;
  if (s.status === "lobby") body = lobbyView(s, isHost);
  else if (s.status === "question") body = questionView(s, isHost);
  else if (s.status === "reveal") body = revealView(s, isHost);
  else body = finishedView(s, isHost);

  mount(
    topbar(`Salle ${s.code}`),
    h("div", { class: "wrap" },
      h("section", { class: "room-head" },
        h("div", { class: "code-block" },
          h("span", { class: "code-label" }, "Code"),
          h("strong", { class: "code" }, s.code),
          h("button", { class: "btn small ghost", type: "button", onclick: () => copyLink(s.code) }, "Copier le lien")),
        h("div", { class: "stats" },
          stat("En jeu", `${s.alive} / ${s.players.length}`),
          stat("Cagnotte", eur.format(s.pot), "gold"),
          s.status === "lobby" ? stat("Mise", eur.format(s.stake)) : stat("Question", `${s.qi + 1} / ${s.total}`))),
      body,
      playersGrid(s, isHost)));
  const phase = `${s.status}:${s.qi}`;
  if (phase !== lastPhase) {
    lastPhase = phase;
    window.scrollTo(0, 0);
  }
  clearInterval(tickTimer);
  if (s.status === "question") {
    tickTimer = setInterval(tick, 200);
    tick();
  }
}

/* -- salle d'attente -- */
function lobbyView(s, isHost) {
  const n = s.players.length;
  return h("section", { class: "card stage-card" },
    h("h2", {}, isHost ? "Les candidats arrivent" : "En attente de l'animateur"),
    h("p", {}, isHost
      ? `${plural(n, "candidat", "candidats")} sur ${s.max_players}. Partage le code ou le lien, puis lance la partie quand tout le monde est là.`
      : `${s.host} lance la partie quand tous les candidats sont arrivés. Il y a ${plural(n, "candidat", "candidats")} dans la salle.`),
    h("p", { class: "muted" }, `${plural(s.total, "question", "questions")}, ${s.timer} secondes chacune, mise de ${eur.format(s.stake)} par candidat.`),
    isHost ? h("div", { class: "actions" },
      h("button", { class: "btn primary big", type: "button", disabled: n === 0, onclick: () => send({ type: "start" }) }, "Lancer la partie"),
      h("button", { class: "btn ghost", type: "button", onclick: closeRoom }, "Fermer la salle")) : null);
}

/* -- question en cours -- */
function questionView(s, isHost) {
  const q = s.question;
  const canAnswer = s.role === "player" && s.me && s.me.alive;
  const selected = s.me ? s.me.answer : null;

  const choices = h("div", { class: "choices" }, q.choices.map((label, i) => {
    const cls = ["choice"];
    if (selected === i) cls.push("selected");
    if (isHost && q.correct === i) cls.push("correct");
    return h("button", {
      class: cls.join(" "), type: "button", disabled: !canAnswer,
      onclick: () => send({ type: "answer", choice: i }),
    }, h("span", { class: "letter" }, LETTERS[i]), h("span", { class: "ctext" }, label));
  }));

  let status;
  if (isHost) {
    status = h("div", {},
      h("p", { class: "status-line" }, `${plural(s.live.answered, "candidat a répondu", "candidats ont répondu")} sur ${s.live.alive} en jeu.`),
      h("p", { class: "muted" }, `Difficulté estimée : ${q.difficulty} % de réussite. La bonne réponse est surlignée pour toi seul.`),
      h("div", { class: "actions" },
        h("button", { class: "btn primary big", type: "button", onclick: () => send({ type: "reveal" }) }, "Révéler la réponse")));
  } else if (canAnswer) {
    status = h("p", { class: "status-line" }, selected !== null
      ? "Réponse enregistrée. Tu peux encore la changer jusqu'à la fin du temps."
      : "Choisis ta réponse avant la fin du temps.");
  } else if (s.role === "player") {
    status = h("p", { class: "status-line" }, "Tu es éliminé. Tu suis la suite depuis les gradins.");
  } else {
    status = h("p", { class: "status-line" }, "Tu regardes la partie en spectateur.");
  }

  return h("section", { class: "card stage-card" },
    q.final ? h("div", { class: "badge gold" }, "Question ultime : les survivants qui trouvent se partagent la cagnotte") : null,
    h("div", { class: "timer", id: "timer" },
      h("span", { class: "timer-num", id: "timer-num" }, "–"),
      h("div", { class: "timebar" }, h("span", { id: "timer-fill" }))),
    h("p", { class: "question-text" }, q.text),
    q.figure ? h("pre", { class: "figure" }, q.figure) : null,
    choices,
    status);
}

function tick() {
  if (!state || state.status !== "question") return;
  const num = document.getElementById("timer-num");
  if (!num) return;
  const remaining = (state.deadline - (Date.now() + clockOffset)) / 1000;
  num.textContent = String(Math.max(0, Math.ceil(remaining)));
  document.getElementById("timer-fill").style.width = `${Math.max(0, Math.min(100, (remaining / state.timer) * 100))}%`;
  const box = document.getElementById("timer");
  box.classList.toggle("urgent", remaining <= 5 && remaining > 0);
  box.classList.toggle("over", remaining <= 0);
  const choices = document.querySelector(".choices");
  if (choices) choices.classList.toggle("locked", remaining <= 0);
}

/* -- révélation -- */
function revealView(s, isHost) {
  const q = s.question;
  const r = s.result;
  const selected = s.me ? s.me.answer : null;

  const choices = h("div", { class: "choices" }, q.choices.map((label, i) => {
    const pct = r.participants ? Math.round((100 * r.counts[i]) / r.participants) : 0;
    const cls = ["choice"];
    if (i === q.correct) cls.push("correct");
    else if (selected === i) cls.push("wrong");
    const fill = h("span", { class: "fill" });
    requestAnimationFrame(() => { fill.style.width = `${pct}%`; });
    return h("div", { class: cls.join(" ") },
      fill,
      h("span", { class: "letter" }, LETTERS[i]),
      h("span", { class: "ctext" }, label),
      h("span", { class: "pct" }, `${pct} %`));
  }));

  const pctRight = r.participants ? Math.round((100 * r.right) / r.participants) : 0;
  let elim;
  if (r.mercy) {
    elim = h("div", { class: "verdict info" }, "Tous les candidats se sont trompés : personne n'est éliminé.");
  } else if (r.eliminated.length) {
    const shown = r.eliminated.slice(0, 12).join(", ");
    const more = r.eliminated.length > 12 ? ` et ${r.eliminated.length - 12} autres` : "";
    elim = h("p", {}, h("strong", {}, `${plural(r.eliminated.length, "éliminé", "éliminés")} : `), shown + more);
  } else {
    elim = h("p", {}, "Aucune élimination sur cette question.");
  }

  let mine = null;
  if (s.role === "player" && s.me) {
    if (s.me.out_at === s.qi + 1) {
      mine = h("div", { class: "verdict bad" }, selected === null ? "Pas de réponse : tu es éliminé." : "Mauvaise réponse : tu es éliminé.");
    } else if (s.me.alive && selected === q.correct) {
      mine = h("div", { class: "verdict ok" }, "Bonne réponse, tu restes en jeu.");
    } else if (s.me.alive && r.mercy) {
      mine = h("div", { class: "verdict info" }, "Ta réponse était fausse, mais tout le monde s'est trompé : tu restes en jeu.");
    }
  }

  return h("section", { class: "card stage-card" },
    q.final ? h("div", { class: "badge gold" }, "Question ultime") : null,
    h("p", { class: "question-text" }, q.text),
    q.figure ? h("pre", { class: "figure" }, q.figure) : null,
    choices,
    h("p", { class: "status-line" }, `${r.right} candidat${r.right > 1 ? "s" : ""} sur ${r.participants} ${r.right > 1 ? "ont" : "a"} trouvé (${pctRight} %). Difficulté estimée : ${q.difficulty} %.`),
    mine,
    elim,
    q.explanation ? h("div", { class: "explain" }, h("strong", {}, "Explication : "), q.explanation) : null,
    isHost ? h("div", { class: "actions" },
      h("button", { class: "btn primary big", type: "button", onclick: () => send({ type: "next" }) },
        q.final ? "Voir le résultat final" : "Question suivante")) : null);
}

/* -- fin de partie -- */
function finishedView(s, isHost) {
  const f = s.final;
  const iWon = s.role === "player" && s.me && s.me.alive && !f.lost;
  const shown = f.winners.slice(0, 20).join(", ") + (f.winners.length > 20 ? ` et ${f.winners.length - 20} autres` : "");
  return h("section", { class: "card stage-card" },
    h("h2", {}, f.lost ? "Cagnotte perdue" : (f.winners.length > 1 ? "Les gagnants" : "Le gagnant")),
    f.lost
      ? h("p", {}, `Personne n'a trouvé la réponse à la question ultime : la cagnotte de ${eur.format(f.pot)} n'est pas remportée.`)
      : h("div", {},
        h("div", { class: "big-prize" }, eur.format(f.share)),
        h("p", {}, f.winners.length > 1
          ? `chacun pour ${plural(f.winners.length, "gagnant", "gagnants")} (cagnotte de ${eur.format(f.pot)}).`
          : `Cagnotte de ${eur.format(f.pot)} remportée.`),
        h("p", { class: "winners" }, shown)),
    iWon ? h("div", { class: "verdict ok" }, `Bravo, tu repars avec ${eur.format(f.share)} !`) : null,
    h("div", { class: "actions" },
      isHost ? [
        h("button", { class: "btn primary", type: "button", onclick: () => send({ type: "reset" }) }, "Rejouer avec les mêmes questions"),
        h("button", { class: "btn ghost", type: "button", onclick: closeRoom }, "Fermer la salle"),
      ] : h("a", { class: "btn", href: "#/" }, "Quitter la salle")));
}

/* -- pupitres des candidats -- */
function playersGrid(s, isHost) {
  const canKick = isHost && s.status === "lobby";
  const myId = s.players.find((p) => s.role === "player" && s.me && p.name === me.username);
  const tiles = s.players.map((p) => {
    const cls = ["tile"];
    if (!p.alive) cls.push("out");
    if (!p.connected) cls.push("offline");
    if (p.answered) cls.push("answered");
    if (myId && p.id === myId.id) cls.push("me");
    let meta = "";
    if (!p.alive) meta = `éliminé Q${p.out_at}`;
    else if (p.answered) meta = "a répondu";
    else if (!p.connected) meta = "hors ligne";
    return h("div", { class: cls.join(" ") },
      h("div", { class: "nm", title: p.name }, p.name),
      h("div", { class: "meta" }, meta),
      canKick ? h("button", {
        class: "kick", type: "button", "aria-label": `Retirer ${p.name}`, onclick: () => send({ type: "kick", user_id: p.id }),
      }, "×") : null);
  });
  return h("section", {},
    h("div", { class: "plateau-head" },
      h("h3", {}, "Les candidats"),
      h("span", { class: "muted" }, `${s.players.length} / ${s.max_players}`)),
    s.players.length ? h("div", { class: "plateau" }, tiles) : h("p", { class: "empty-note" }, "Personne n'est encore dans la salle."));
}

/* ------------------------------------------------------------------ départ */
(async function boot() {
  try { me = await api("/api/me"); } catch (_) { me = null; }
  window.addEventListener("hashchange", route);
  route();
})();
