/*
 Test d'interface (faux navigateur jsdom) contre un vrai serveur.

   npm install jsdom            (dans un dossier de ton choix)
   BASE_URL=http://127.0.0.1:8000 NODE_PATH=chemin/vers/node_modules node tests/ui_smoke.js

 Le serveur doit déjà tourner (idéalement avec une base vide : DATABASE_PATH=/tmp/test.db).
*/
const { JSDOM, CookieJar, VirtualConsole, requestInterceptor } = require("jsdom");

const BASE = process.env.BASE_URL || "http://127.0.0.1:8000";
const SUFFIX = Math.random().toString(36).slice(2, 7);
const errors = [];

// Pas de réseau externe (polices Google, etc.) : on répond par une ressource vide.
const LOCAL_HOST = new URL(BASE).hostname;
const blockExternal = requestInterceptor((request) => {
  if (new URL(request.url).hostname !== LOCAL_HOST) return new Response("", { status: 200 });
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(fn, label, timeout = 8000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    try { const v = fn(); if (v) return v; } catch (_) { /* pas encore prêt */ }
    await sleep(25);
  }
  throw new Error("Délai dépassé : " + label);
}

async function openUser(name) {
  const jar = new CookieJar();
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(`[${name}] ${e.stack || e}`));
  vc.on("error", (...a) => errors.push(`[${name}] ${a.join(" ")}`));
  const dom = await JSDOM.fromURL(BASE + "/", {
    runScripts: "dangerously", resources: { interceptors: [blockExternal] }, pretendToBeVisual: true,
    cookieJar: jar, virtualConsole: vc,
    beforeParse(window) {
      window.fetch = async (url, opts = {}) => {
        const full = new URL(url, BASE).href;
        const headers = { ...(opts.headers || {}), cookie: jar.getCookieStringSync(full) };
        const res = await fetch(full, { ...opts, headers });
        for (const c of res.headers.getSetCookie()) jar.setCookieSync(c, full);
        return res;
      };
      window.confirm = () => true;
      window.scrollTo = () => {};
    },
  });
  const { window } = dom;
  const doc = window.document;
  const u = {
    name, dom, window, doc,
    text: () => doc.body.textContent.replace(/\s+/g, " "),
    q: (sel) => doc.querySelector(sel),
    qa: (sel) => [...doc.querySelectorAll(sel)],
    button: (label) => [...doc.querySelectorAll("button, a.btn")].find((b) => b.textContent.trim() === label),
    click: (el) => el.click(),
    type(el, value) { el.value = value; el.dispatchEvent(new window.Event("input", { bubbles: true })); },
    submit(form) { form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })); },
    goto(hash) { window.location.hash = hash; },
  };
  await waitFor(() => u.q("form.card"), `${name}: écran de connexion`);
  return u;
}

async function register(u, password = "motdepasse123") {
  u.click(u.button("Créer un compte"));
  await waitFor(() => u.q("h2") && u.q("h2").textContent === "Créer un compte", `${u.name}: formulaire d'inscription`);
  u.type(u.q("#f-user"), u.name);
  u.type(u.q("#f-pass"), password);
  u.submit(u.q("form.card"));
  await waitFor(() => u.text().includes("Salut " + u.name), `${u.name}: accueil`);
}

const ok = (m) => console.log("  ✔ " + m);

(async () => {
  const host = await openUser("host" + SUFFIX);
  const players = [];
  for (let i = 1; i <= 3; i++) players.push(await openUser(`cand${i}${SUFFIX}`));

  // --- erreurs de formulaire
  host.type(host.q("#f-user"), "inconnu" + SUFFIX);
  host.type(host.q("#f-pass"), "mauvais-mot-de-passe");
  host.submit(host.q("form.card"));
  await waitFor(() => host.q(".form-error").textContent.includes("incorrect"), "message d'erreur de connexion");
  ok("connexion refusée : message d'erreur affiché");

  await register(host);
  await Promise.all(players.map((p) => register(p)));
  ok("inscription de l'animateur et de 3 candidats");

  // --- création de partie
  host.goto("#/setup");
  await waitFor(() => host.q(".avail-list .qrow"), "liste des questions");
  host.type(host.q("input[aria-label='Nombre de questions']"), "5");
  host.click(host.button("Générer"));
  await waitFor(() => host.qa("ol.list .qrow").length === 5, "5 questions dans la partie");
  assert(host.text().includes("ultime"), "badge question ultime");
  host.click(host.button("Créer la salle"));
  const code = (await waitFor(() => host.q(".code"), "code de salle")).textContent;
  assert(/^[A-Z2-9]{5}$/.test(code), "format du code : " + code);
  ok(`salle ${code} créée avec 5 questions`);

  // --- les candidats rejoignent par le code
  for (const p of players) {
    p.type(p.q(".code-input"), code.toLowerCase());
    p.submit(p.q(".join-form"));
    await waitFor(() => p.q(".code") && p.text().includes("En attente de l'animateur"), `${p.name}: salle d'attente`);
  }
  await waitFor(() => host.text().includes("3 / 40"), "3 candidats vus par l'animateur");
  assert(host.qa(".tile").length === 3, "3 pupitres");
  ok("3 candidats dans la salle d'attente (vue animateur : 3 / 40)");

  // --- déroulement
  host.click(host.button("Lancer la partie"));
  const everyone = [host, ...players];
  const phase = (u, label) => u.q(".stat:last-child .stat-value").textContent.trim() === label;

  for (let qi = 1; qi <= 5; qi++) {
    await waitFor(() => everyone.every((u) => u.q(".timer") && phase(u, `${qi} / 5`)), `question ${qi} affichée partout`);
    const choicesHost = host.qa(".choices .choice");
    const correct = choicesHost.findIndex((b) => b.classList.contains("correct"));
    assert(correct >= 0, "la bonne réponse est visible pour l'animateur");
    for (const p of players) {
      assert(!p.qa(".choices .choice").some((b) => b.classList.contains("correct")), "pas de fuite chez " + p.name);
    }
    const wrong = (correct + 1) % choicesHost.length;
    players[0].click(players[0].qa(".choices .choice")[correct]);
    players[1].click(players[1].qa(".choices .choice")[correct]);
    // le 3e candidat se trompe à la première question seulement
    players[2].click(players[2].qa(".choices .choice")[qi === 1 ? wrong : correct]);
    const alive3 = qi === 1;
    if (alive3) {
      await waitFor(() => host.text().includes("3 candidats ont répondu"), "3 réponses côté animateur");
    } else {
      await waitFor(() => host.text().includes("2 candidats ont répondu"), "2 réponses côté animateur");
    }
    host.click(host.button("Révéler la réponse"));
    await waitFor(() => everyone.every((u) => u.q(".explain, .status-line") && u.text().includes("Difficulté estimée")), `révélation ${qi}`);
    await waitFor(() => players[0].qa(".choices .choice.correct").length === 1, "bonne réponse révélée");
    if (qi === 1) {
      await waitFor(() => players[0].q(".verdict.ok") && players[2].q(".verdict.bad"), "verdicts Q1");
      assert(players[0].q(".verdict.ok").textContent.includes("Bonne réponse"), "verdict bonne réponse");
      assert(players[2].q(".verdict.bad").textContent.includes("éliminé"), "verdict éliminé");
      await waitFor(() => host.qa(".tile.out").length === 1, "pupitre éliminé grisé");
      assert(host.text().includes("1 000"), "cagnotte 1 000 €");
      ok("Q1 : bonne réponse / élimination affichées, pupitre grisé, cagnotte 1 000 €");
    }
    host.click(host.button(qi === 5 ? "Voir le résultat final" : "Question suivante"));
  }

  await waitFor(() => players[0].q(".big-prize"), "écran final");
  assert(players[0].q(".big-prize").textContent.replace(/\s/g, "").includes("500"), "gain de 500 € chacun : " + players[0].q(".big-prize").textContent);
  assert(players[0].text().includes("Bravo"), "message de victoire");
  assert(!players[2].text().includes("Bravo"), "pas de victoire pour l'éliminé");
  ok("fin de partie : 2 gagnants se partagent 1 000 € (500 € chacun)");

  host.click(host.button("Rejouer avec les mêmes questions"));
  await waitFor(() => players.every((p) => p.text().includes("En attente de l'animateur")), "retour au lobby");
  await waitFor(() => host.qa(".tile.out").length === 0, "tous de nouveau en lice");
  ok("rejouer : retour au lobby, tous les pupitres rallumés");

  // --- mes questions
  host.goto("#/questions");
  await waitFor(() => host.q("textarea#q-text"), "page Mes questions");
  host.type(host.q("#q-text"), "Quel nombre suit 10, 20, 30 ?");
  const ins = host.qa(".choice-input input:not([type=radio])");
  ["31", "40", "50"].forEach((v, i) => host.type(ins[i], v));
  host.qa(".choice-input input[type=radio]")[1].checked = true;
  host.submit(host.q("form.card"));
  await waitFor(() => host.text().includes("Mes questions (1)") && host.text().includes("Bonne réponse : 40"), "question ajoutée");
  host.click(host.button("Supprimer"));
  await waitFor(() => host.text().includes("Mes questions (0)"), "question supprimée");
  ok("mes questions : ajout (avec bonne réponse) puis suppression");

  // --- fermeture de salle
  host.goto("#/");
  await waitFor(() => host.text().includes("Tes salles ouvertes"), "salles ouvertes listées");
  host.click(host.button("Reprendre"));
  await waitFor(() => host.q(".code"), "reprise de la salle");
  host.click(host.button("Fermer la salle"));
  await waitFor(() => players.every((p) => p.text().includes("Salut")), "candidats renvoyés à l'accueil");
  ok("fermeture de salle : tout le monde revient à l'accueil");

  if (errors.length) {
    console.log("\nERREURS JS :\n" + errors.join("\n"));
    process.exit(1);
  }
  console.log("\nTous les tests d'interface ont réussi, aucune erreur JavaScript.");
  process.exit(0);
})().catch((e) => {
  console.error("\nÉCHEC :", e.message);
  if (errors.length) console.error("Erreurs JS :\n" + errors.join("\n"));
  process.exit(1);
});

function assert(cond, msg) {
  if (!cond) throw new Error("Assertion : " + msg);
}
