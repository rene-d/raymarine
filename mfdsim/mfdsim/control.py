"""
control.py — API REST de pilotage du simulateur (TCP 8088 par défaut).

Sert à conduire à chaud ce qui ne s'exprime pas en options de ligne de commande :
router le bateau, changer d'allure, mouiller, faire déraper le mouillage,
appareiller, relire l'état. Le MFD réel n'expose évidemment rien de tel — c'est
une commande de simulateur, sur un port à part, hors de tout protocole
rétro-conçu.

    curl localhost:8088/state
    curl -X POST 'localhost:8088/position?lat=48.35&lon=-4.9'
    curl -X POST 'localhost:8088/heading?heading=310'
    curl -X POST 'localhost:8088/speed?speed=9'
    curl -X POST 'localhost:8088/sail?allure=grand+largue&amure=babord'
    curl -X POST localhost:8088/anchor                      # mouille ici
    curl -X POST 'localhost:8088/anchor?lat=48.6543&lon=-3.8792'
    curl -X POST localhost:8088/drag                        # cap au hasard, 0,5 nd
    curl -X POST 'localhost:8088/drag?course=120&speed=0.8'
    curl -X POST localhost:8088/drag -d '{"course": 120}'
    curl -X POST 'localhost:8088/underway?heading=310&speed=8'

Les paramètres se passent indifféremment en query string ou en corps JSON, pour
rester utilisable d'un simple `curl` sans échappement.

Deux pages HTML autonomes (aucune ressource externe) accompagnent l'API :
`GET /` pilote la simulation à la souris, `GET /help` liste les endpoints pour
la scripter — cette table-là est engendrée depuis `ROUTES`, donc elle ne peut
pas diverger du routage.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config
from .sim import DEG, KNOT, POINTS_OF_SAIL, TACKS, Simulation

log = logging.getLogger("control")

ALLURES = ", ".join(POINTS_OF_SAIL)


@dataclass(frozen=True)
class Route:
    """Une entrée de l'API : ce que le routage applique et ce que l'aide affiche.

    Décrire les routes une seule fois évite le sort ordinaire des tables d'aide,
    qui prennent du retard sur le code qu'elles documentent.
    """

    method: str
    path: str
    summary: str
    #: "" = toutes situations, sinon le mode exigé — les autres reçoivent un 409.
    mode: str = ""
    params: tuple[tuple[str, str], ...] = ()
    #: Query string d'exemple, reprise telle quelle dans la page d'aide.
    example: str = ""

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"

    def curl(self, host: str) -> str:
        # L'URL est apostrophée dès qu'elle porte une query string : `?` et `&`
        # sont des métacaractères de shell, et zsh refuse carrément la commande.
        target = f"{host}{self.path}{self.example}"
        if self.example:
            target = f"'{target}'"
        verb = "curl" if self.method == "GET" else "curl -X POST"
        return f"{verb} {target}"


LAT = ("lat", "latitude, en degrés décimaux")
LON = ("lon", "longitude, en degrés décimaux")

ROUTES = (
    Route("GET", "/", "page de pilotage : l'état rafraîchi chaque seconde, et "
                      "les commandes du mode en cours en formulaires"),
    Route("GET", "/help", "cette page — la liste des endpoints",
          params=(("format", "json — la même table en JSON, pour un script"),)),
    Route("GET", "/state", "état complet en JSON : position, cap, route, vitesses, "
                           "vent, sonde, consignes, et le mouillage s'il y a lieu"),
    Route("POST", "/anchor", "mouille : sur place, l'ancre tombant par l'avant ; ou "
                             "au point donné, que le bateau rejoint (déjà au "
                             "mouillage) ou où il se retrouve (en navigation)",
          params=(("lat", "latitude de l'ancre (défaut : position du bateau)"),
                  ("lon", "longitude de l'ancre")),
          example="?lat=48.6543&lon=-3.8792"),
    Route("POST", "/underway", "appareille — l'ancre est relevée, le bateau repart "
                               "d'où il mouillait — et pose au passage les consignes "
                               "données ; déjà en route, ne fait que les poser",
          params=(("heading", "cap vrai visé, en degrés"),
                  ("speed", "vitesse surface visée, en nœuds"),
                  ("allure", f"{ALLURES}, ou un angle en degrés"),
                  ("amure", "tribord ou babord")),
          example="?heading=310&speed=8"),
    Route("POST", "/position", "téléporte le bateau ; le cap et la vitesse ne "
                               "changent pas", mode="passage",
          params=(LAT, LON), example="?lat=48.35&lon=-4.90"),
    Route("POST", "/heading", "change le cap visé ; le bateau y abat, à 6°/s au plus",
          mode="passage", params=(("heading", "cap vrai, en degrés"),),
          example="?heading=310"),
    Route("POST", "/speed", "change la vitesse surface visée ; le vent réel suit, "
                            "au double environ", mode="passage",
          params=(("speed", "vitesse, en nœuds"),), example="?speed=9"),
    Route("POST", "/sail", "change l'allure, donc le vent réel à l'étrave (TWA), "
                           "donc le vent apparent", mode="passage",
          params=(("allure", f"{ALLURES}, ou un angle en degrés, négatif à bâbord amure"),
                  ("amure", f"{', '.join(TACKS)} (défaut : l'amure en cours)")),
          example="?allure=travers&amure=babord"),
    Route("POST", "/drag", "l'ancre décroche : le bateau file en ligne droite et "
                           "s'éloigne au-delà du point de mouillage", mode="mouillage",
          params=(("course", "cap de la dérive, en degrés (défaut : au hasard)"),
                  ("speed", "vitesse de la dérive, en nœuds (défaut : 0.5)")),
          example="?course=120&speed=0.8"),
)

ROUTE_BY_KEY = {r.key: r for r in ROUTES}
#: Forme courte, servie avec les 404 pour dépanner sans quitter le terminal.
ROUTE_SUMMARY = {r.key: r.summary for r in ROUTES}

STYLE = """
 body { font: 14px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 52rem;
        padding: 0 1rem; color: #202124; background: #fff; }
 h1 { font-size: 1.2rem; } h2 { font-size: 1rem; margin: 1.5rem 0 .5rem; }
 a { color: #1a73e8; }
 table { border-collapse: collapse; width: 100%; }
 th, td { text-align: left; padding: .15rem .5rem .15rem 0; vertical-align: top; }
 code, td.v { font-family: ui-monospace, monospace; }
 form { margin: .35rem 0; }
 input, select, button { font: inherit; padding: .15rem .3rem; }
 input { width: 7rem; }
 label { color: #5f6368; margin-right: .5rem; }
 pre { background: #f1f3f4; padding: .5rem; overflow-x: auto; min-height: 1rem; }
 .note { color: #5f6368; font-size: .9rem; border-left: 3px solid #dadce0; padding-left: .6rem; }
 [hidden] { display: none; }
 @media (prefers-color-scheme: dark) {
   body { color: #e8eaed; background: #202124; }
   a { color: #8ab4f8; }
   label, .note { color: #9aa0a6; }
   pre { background: #303134; } .note { border-color: #5f6368; }
 }
"""

PAGE = """<!doctype html>
<html lang="fr">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mfdsim — pilotage</title>
<style>{{STYLE}}
 #state th { font-weight: normal; color: #5f6368; width: 14rem; }
 /* En bout de formulaire, la note est une glose, pas un encadré. */
 form .note { border: 0; padding: 0; margin-left: .4rem; }
 @media (prefers-color-scheme: dark) { #state th { color: #9aa0a6; } }
</style>
<h1>mfdsim — pilotage de la simulation</h1>
<p><a href="/help">liste des endpoints, pour scripter le simulateur →</a></p>
<table id="state"></table>

<section id="passage" hidden>
 <h2>Navigation</h2>
 <form data-post="/position">
  <label>position</label><input name="lat" placeholder="lat"> <input name="lon" placeholder="lon">
  <button>Téléporter</button></form>
 <form data-post="/heading">
  <label>cap vrai</label><input name="heading" placeholder="°"> <button>Router</button></form>
 <form data-post="/speed">
  <label>vitesse</label><input name="speed" placeholder="nœuds"> <button>Régler</button></form>
 <form data-post="/sail">
  <label>allure</label><select name="allure"></select>
  <select name="amure"><option value="tribord">tribord amure</option>
   <option value="babord">bâbord amure</option></select>
  <button>Border</button></form>
 <form data-post="/anchor">
  <label>mouiller</label><input name="lat" placeholder="lat"> <input name="lon" placeholder="lon">
  <button>Mouiller</button>
  <span class="note">sans coordonnées : ici même, l'ancre par l'avant</span></form>
</section>

<section id="anchor" hidden>
 <h2>Mouillage</h2>
 <form data-post="/drag">
  <label>dérapage</label><input name="course" placeholder="cap °"> <input name="speed" placeholder="nœuds">
  <button>Déraper</button>
  <span class="note">sans consigne : direction au hasard, 0,5 nd</span></form>
 <form data-post="/anchor">
  <label>ancre</label><input name="lat" placeholder="lat"> <input name="lon" placeholder="lon">
  <button>Remouiller</button></form>
 <form data-post="/underway">
  <label>appareiller</label><input name="heading" placeholder="cap °"> <input name="speed" placeholder="nœuds">
  <button>Appareiller</button>
  <span class="note">sans consigne : les réglages d'avant le mouillage</span></form>
</section>

<h2>Réponse</h2>
<pre id="reply"></pre>
<p class="note">La simulation n'avance que lorsqu'un client RayDB est connecté :
 sans client l'état reste figé, et les commandes ne se propagent qu'à la
 connexion suivante.</p>

<script>
const $ = (s) => document.querySelector(s);
const LABELS = {
  mode: "mode", "position.lat": "latitude", "position.lon": "longitude",
  heading_deg: "cap vrai (°)", cog_deg: "route fond (°)", sog_kn: "vitesse fond (nd)",
  stw_kn: "vitesse surface (nd)", depth_m: "sonde (m)",
  "wind.tws_kn": "vent réel (nd)", "wind.twa_deg": "vent réel / étrave (°)",
  "wind.aws_kn": "vent apparent (nd)", "wind.awa_deg": "vent apparent / étrave (°)",
  "target.course_deg": "cap visé (°)", "target.speed_kn": "vitesse visée (nd)",
  "target.allure": "allure", "target.amure": "amure",
  "anchor.lat": "ancre latitude", "anchor.lon": "ancre longitude",
  distance_m: "distance à l'ancre (m)", swing_radius_m: "rayon d'évitage (m)",
  wind_from_deg: "vent venant de (°)", drift: "dérive",
  "drift.course_deg": "dérive cap (°)", "drift.speed_kn": "dérive vitesse (nd)",
};

/* Aplatit le JSON d'état en lignes « clé pointée / valeur », ce qui affiche
   les deux modes sans que la page connaisse leurs champs respectifs. */
function flatten(value, prefix, rows) {
  for (const [k, v] of Object.entries(value)) {
    const key = prefix ? prefix + "." + k : k;
    if (v && typeof v === "object" && !Array.isArray(v)) flatten(v, key, rows);
    else rows.push([key, Array.isArray(v) ? v.join(" – ") : v === null ? "—" : v]);
  }
  return rows;
}

let loaded = false;   /* les menus ne sont alignés sur l'état qu'au chargement,
                         pour ne pas défaire un choix en cours de saisie. */

async function refresh() {
  let state;
  try { state = await (await fetch("/state")).json(); }
  catch (e) { $("#state").innerHTML = "<tr><td>simulateur injoignable</td></tr>"; return; }
  $("#state").innerHTML = flatten(state, "", [])
    .map(([k, v]) => `<tr><th>${LABELS[k] || k}</th><td class="v">${v}</td></tr>`).join("");
  const anchored = state.mode !== "passage";
  $("#passage").hidden = anchored;
  $("#anchor").hidden = !anchored;
  if (!loaded && state.target) {
    loaded = true;
    if (state.target.allure) $("select[name=allure]").value = state.target.allure;
    $("select[name=amure]").value = state.target.amure;
  }
}

for (const form of document.querySelectorAll("form[data-post]")) {
  form.onsubmit = async (event) => {
    event.preventDefault();
    /* Les champs vides sont retirés : chaque route a ses propres défauts
       (dérapage au hasard, mouillage sur place, consignes conservées). */
    const params = new URLSearchParams(
      [...new FormData(form)].filter(([, v]) => v.trim() !== ""));
    const reply = await fetch(form.dataset.post + "?" + params, { method: "POST" });
    $("#reply").textContent = await reply.text();
    refresh();
  };
}

$("select[name=allure]").innerHTML = {{ALLURES}}
  .map((a) => `<option value="${a}">${a}</option>`).join("");
refresh();
setInterval(refresh, 1000);
</script>
</html>
"""

HELP = """<!doctype html>
<html lang="fr">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mfdsim — endpoints de pilotage</title>
<style>{{STYLE}}
 article { border-top: 1px solid #dadce0; padding: .9rem 0 1rem; }
 article h3 { font-size: 1rem; font-weight: 600; margin: 0 0 .3rem; }
 article p { margin: .2rem 0 .6rem; }
 article pre { margin: 0; }
 .mode { font-size: .78rem; font-weight: normal; color: #5f6368; margin-left: .6rem;
         border: 1px solid #dadce0; border-radius: 1rem; padding: .05rem .55rem; }
 dl { display: grid; grid-template-columns: max-content 1fr; gap: .15rem .9rem;
      margin: 0 0 .7rem; font-size: .95rem; }
 dt { font-family: ui-monospace, monospace; }
 dd { margin: 0; color: #5f6368; }
 @media (prefers-color-scheme: dark) {
   article { border-color: #3c4043; }
   .mode { color: #9aa0a6; border-color: #5f6368; } dd { color: #9aa0a6; }
 }
</style>
<h1>mfdsim — endpoints de pilotage</h1>
<p><a href="/">← page de pilotage</a> — API de commande du simulateur, sur
 <code>{{BASE}}</code>. Rien de tout ceci n'existe sur un MFD réel.</p>

{{ROUTES}}

<h2>Conventions</h2>
<ul>
 <li>Les paramètres se passent en <b>query string</b> ou en <b>corps JSON</b>
  (<code>-d '{"course": 120}'</code>), indifféremment.</li>
 <li>Toute réponse est un objet JSON : l'état complet du bateau en cas de
  succès, <code>{"error": "…"}</code> sinon.</li>
 <li><code>200</code> succès — <code>400</code> paramètre manquant, non
  numérique ou hors bornes, ou corps JSON invalide — <code>409</code> commande
  étrangère au mode en cours — <code>404</code> route inconnue (la réponse
  liste alors les routes).</li>
 <li>Les consignes (cap, vitesse, allure) ne sont pas des téléportations : le
  bateau abat, accélère et change d'allure en quelques secondes. <code>/state</code>
  publie la valeur instantanée et la consigne côte à côte.</li>
</ul>

<h2>Scripter un scénario</h2>
<pre>{{SCRIPT}}</pre>
<p class="note">La simulation n'avance que lorsqu'un client RayDB est connecté —
 elle est pilotée par les demandes de changements, pas par une horloge propre.
 Sans client, <code>/state</code> reste figé et une commande ne prend effet
 qu'à la connexion suivante. Le port se change par <code>MFD_CONTROL_PORT</code>,
 et <code>--no-control</code> supprime l'API et ces deux pages.</p>
</html>
"""

SCRIPT = """BASE={{BASE}}

# appareiller au large d'Ouessant, cap au nord-ouest, 8 nœuds au largue
curl -sX POST "$BASE/position?lat=48.35&lon=-4.90"           > /dev/null
curl -sX POST "$BASE/underway?heading=310&speed=8&allure=largue" > /dev/null

# … puis mouiller en baie de Morlaix, et faire déraper l'ancre
curl -sX POST "$BASE/anchor?lat=48.6543&lon=-3.8792"         > /dev/null
curl -sX POST "$BASE/drag?course=120&speed=0.8"              > /dev/null

# surveiller l'éloignement du point de mouillage
while sleep 5; do
  curl -s "$BASE/state" | jq -r '"\\(.mode) — écart \\(.distance_m) m"'
done"""


def _routes_html(host: str) -> str:
    """Corps de la page d'aide, engendré depuis `ROUTES`.

    Une pastille de mode n'apparaît que sur les routes qui en exigent un : sur
    les autres, elle ne dirait rien.
    """
    out = []
    for route in ROUTES:
        mode = f'<span class="mode">{route.mode}</span>' if route.mode else ""
        params = "".join(f"<dt>{name}</dt><dd>{desc}</dd>"
                         for name, desc in route.params)
        out.append(
            f"<article><h3><code>{route.method} {route.path}</code>{mode}</h3>"
            f"<p>{route.summary}</p>"
            + (f"<dl>{params}</dl>" if params else "")
            + f"<pre>{route.curl(host)}</pre></article>")
    return "\n".join(out)


class _Handler(BaseHTTPRequestHandler):
    server_version = "mfdsim-control/1.0"

    # -------------------------------------------------------------- socle ---
    def _reply(self, code: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode() + b"\n"
        self._send(code, "application/json; charset=utf-8", body)

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host(self) -> str:
        """Base des exemples : l'hôte tel que le client l'a demandé."""
        return f"http://{self.headers.get('Host') or f'localhost:{config.CONTROL_PORT}'}"

    def _params(self) -> dict[str, object]:
        """Paramètres de la requête : query string, puis corps JSON éventuel."""
        params: dict[str, object] = {
            k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError as e:
                raise ValueError(f"corps JSON invalide : {e}") from e
            if not isinstance(body, dict):
                raise ValueError("le corps JSON doit être un objet")
            params.update(body)
        return params

    @staticmethod
    def _number(params: dict[str, object], key: str) -> float | None:
        """Lit un paramètre numérique optionnel, quel que soit son type source."""
        if params.get(key) in (None, ""):
            return None
        try:
            return float(params[key])  # type: ignore[arg-type]
        except (TypeError, ValueError) as e:
            raise ValueError(f"paramètre « {key} » non numérique : {params[key]!r}") from e

    @classmethod
    def _required(cls, params: dict[str, object], key: str) -> float:
        value = cls._number(params, key)
        if value is None:
            raise ValueError(f"paramètre « {key} » attendu")
        return value

    def log_message(self, fmt: str, *args: object) -> None:
        log.debug("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------ routage ---
    def _dispatch(self, method: str) -> Route | None:
        """Route demandée, après contrôle du mode. `None` = réponse déjà émise."""
        path = urlparse(self.path).path.rstrip("/") or "/"
        route = ROUTE_BY_KEY.get(f"{method} {path}")
        if route is None:
            self._reply(404, {"error": "route inconnue", "routes": ROUTE_SUMMARY})
            return None
        sim: Simulation = self.server.sim  # type: ignore[attr-defined]
        if route.mode == "passage" and sim.anchored:
            self._reply(409, {"error": "le bateau est au mouillage — sa route y est "
                                       "imposée par l'ancre ; appareiller par "
                                       "POST /underway"})
            return None
        if route.mode == "mouillage" and not sim.anchored:
            self._reply(409, {"error": "le bateau n'est pas au mouillage — mouiller "
                                       "par POST /anchor"})
            return None
        return route

    def do_GET(self) -> None:
        route = self._dispatch("GET")
        if route is None:
            return
        sim: Simulation = self.server.sim  # type: ignore[attr-defined]
        host = self._host()
        if route.path == "/":
            # La liste des allures vient de `sim.py` : le menu de la page suit
            # la table sans qu'il faille la recopier dans le HTML.
            page = (PAGE.replace("{{STYLE}}", STYLE)
                        .replace("{{ALLURES}}", json.dumps(list(POINTS_OF_SAIL))))
            self._send(200, "text/html; charset=utf-8", page.encode())
        elif route.path == "/help":
            params = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            if params.get("format") == "json":
                self._reply(200, {
                    "base": host,
                    "routes": [{"method": r.method, "path": r.path,
                                "mode": r.mode or "tous", "summary": r.summary,
                                "params": dict(r.params), "example": r.curl(host)}
                               for r in ROUTES],
                })
            else:
                page = (HELP.replace("{{STYLE}}", STYLE)
                            .replace("{{ROUTES}}", _routes_html(host))
                            .replace("{{SCRIPT}}", SCRIPT)
                            .replace("{{BASE}}", host))
                self._send(200, "text/html; charset=utf-8", page.encode())
        else:
            self._reply(200, sim.status())

    def do_POST(self) -> None:
        route = self._dispatch("POST")
        if route is None:
            return
        sim: Simulation = self.server.sim  # type: ignore[attr-defined]
        boat = sim.boat

        try:
            params = self._params()
            if route.path == "/position":
                status = boat.set_position(self._required(params, "lat"),
                                           self._required(params, "lon"))
                log.info("position imposée : %s", status["position"])
            elif route.path == "/heading":
                status = boat.set_heading(self._required(params, "heading"))
                log.info("cap visé : %.0f°", boat.course / DEG)
            elif route.path == "/speed":
                status = boat.set_speed(self._required(params, "speed"))
                log.info("vitesse visée : %.1f nd, vent réel %.1f nd",
                         boat.speed / KNOT, boat.wind_speed / KNOT)
            elif route.path == "/sail":
                status = boat.set_sail(params.get("allure"), params.get("amure"))
                log.info("allure %s, %s amure — TWA %.0f°",
                         boat.allure or "libre", boat.amure, boat.wind_angle / DEG)
            elif route.path == "/anchor":
                status = sim.anchor(self._number(params, "lat"),
                                    self._number(params, "lon"))
                log.info("mouillage sur %s", status["anchor"])
            elif route.path == "/underway":
                status = sim.underway(self._number(params, "heading"),
                                      self._number(params, "speed"),
                                      params.get("allure"), params.get("amure"))
                log.info("en route — cap visé %.0f°, %.1f nd",
                         sim.boat.course / DEG, sim.boat.speed / KNOT)
            else:   # /drag
                course = self._number(params, "course")
                speed = self._number(params, "speed")
                status = boat.drag(course) if speed is None else boat.drag(course, speed)
                log.info("dérapage : cap %.0f°, %.1f nd",
                         status["drift"]["course_deg"], status["drift"]["speed_kn"])  # type: ignore[index]
        except ValueError as e:
            self._reply(400, {"error": str(e)})
        else:
            self._reply(200, status)


class ControlServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host: str, port: int, sim: Simulation) -> None:
        super().__init__((host, port), _Handler)
        self.sim = sim


def serve(sim: Simulation, port: int = config.CONTROL_PORT) -> ControlServer:
    """Démarre l'API de contrôle dans un thread et la renvoie."""
    srv = ControlServer("0.0.0.0", port, sim)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("API de contrôle à l'écoute sur 0.0.0.0:%d — pilotage "
             "http://localhost:%d/ , endpoints http://localhost:%d/help",
             port, port, port)
    return srv
