//! MFDView — les instruments RayDB en application native.
//!
//! Même page que `webapp/static` (Tauri l'embarque telle quelle), mais le TCP
//! est fait ici, en Rust, au lieu de passer par `raydb_bridge.py` : plus de
//! machine intermédiaire. La page reçoit les mêmes messages `delta` et
//! `status`, cette fois par le bus d'événements Tauri et non par SSE.
//!
//! L'adresse du MFD est trouvée par mDNS (`discovery.rs`) ; poser `MFD_IP` dans
//! l'environnement la force, ce qui sert au banc d'essai (`MFD_IP=127.0.0.1`).
//!
//! Le travail vit dans une **bibliothèque** et non dans `main.rs` : sur mobile
//! il n'y a pas de `main()`, l'app est un projet Xcode (ou Gradle) qui lie
//! cette bibliothèque et appelle `run()` par le point d'entrée que pose
//! `#[tauri::mobile_entry_point]`.

mod discovery;
#[cfg(all(target_os = "macos", feature = "map"))]
mod mbtiles;
mod raydb;

use std::io::{ErrorKind, Read, Write};
use std::net::TcpStream;
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};
use tauri::{AppHandle, Emitter, Listener, RunEvent};
#[cfg(target_os = "macos")]
use tauri::Manager;

/// Deux abonnements : l'arbre de navigation, et le nom du bateau qui vit
/// ailleurs. Ce dernier est « retained » — poussé une fois à l'abonnement.
const SUBSCRIPTIONS: [&str; 2] = ["data/#", "Settings/Data/-/7/13/-/-/-/-"];
const FLUSH: Duration = Duration::from_millis(200); // cadence d'envoi à la page
const RETRY: Duration = Duration::from_secs(2);
const DISCOVERY: Duration = Duration::from_secs(10); // budget d'une recherche mDNS

/// Chemins RayDB retenus → clé lue par la page. `data/position` est une chaîne
/// "lat,lon", éclatée en deux clés (voir `record`).
const NAV_PATHS: &[(&str, &str)] = &[
    ("data/sog", "sog"),
    ("data/cog", "cog"),
    ("data/heading/true", "hdg"),
    ("data/heading/magnetic", "hdgMag"),
    ("data/bearing/variation", "variation"),
    ("data/wind/speed/true", "tws"),
    ("data/wind/direction/true", "twa"),
    ("data/wind/speed/apparent", "aws"),
    ("data/wind/direction/apparent", "awa"),
    ("data/position", "position"),
    ("data/position/accuracy", "posAcc"),
    ("data/depth", "depth"),
    ("Settings/Data/-/7/13/-/-/-/-", "boat"),
];

/// Une attente qu'on peut écourter. Sur iOS, l'application est gelée en
/// arrière-plan : au retour au premier plan, la socket est morte et le thread
/// dort peut-être encore. Le réveiller épargne à l'écran quelques secondes de
/// valeurs figées — les plus trompeuses, puisqu'elles ont l'air fraîches.
#[derive(Default)]
struct Wake {
    poked: Mutex<bool>,
    ready: Condvar,
}

impl Wake {
    fn poke(&self) {
        *self.poked.lock().unwrap() = true;
        self.ready.notify_all();
    }

    /// Dort au plus `budget`, ou jusqu'au prochain `poke`.
    fn sleep(&self, budget: Duration) {
        let mut poked = self.poked.lock().unwrap();
        if !*poked {
            poked = self.ready.wait_timeout(poked, budget).unwrap().0;
        }
        *poked = false;
    }
}

/// Dernier état connu, pour repeupler une page qui a chargé après coup.
///
/// Les événements Tauri ne se rejouent pas, et la webview démarre *après* le
/// thread : sans ce rattrapage, la page ouverte une seconde trop tard affiche
/// « démarrage… » alors que la liaison est établie, et le nom du bateau — poussé
/// une seule fois, à l'abonnement — n'arrive jamais. La page réclame l'état dès
/// que ses écoutes sont en place (événement `ready`).
///
/// Un seul MFD, une seule fenêtre : un état global vaut mieux ici qu'un `Arc`
/// promené dans toutes les signatures.
#[derive(Default)]
struct Snapshot {
    values: Map<String, Value>,
    status: Option<Value>,
}

fn snapshot() -> &'static Mutex<Snapshot> {
    static SNAPSHOT: OnceLock<Mutex<Snapshot>> = OnceLock::new();
    SNAPSHOT.get_or_init(Default::default)
}

fn emit_status(app: &AppHandle, text: &str, target: &str, connected: bool) {
    let payload = json!({ "text": text, "target": target, "connected": connected });
    snapshot().lock().unwrap().status = Some(payload.clone());
    let _ = app.emit("status", payload);
}

/// Changement d'état de la liaison : dit à la page, et au terminal.
fn status(app: &AppHandle, text: &str, target: &str, connected: bool) {
    eprintln!("raydb: {text}");
    emit_status(app, text, target, connected);
}

/// Range une valeur RayDB sous sa clé, si elle nous intéresse.
fn record(pending: &mut Map<String, Value>, path: &str, value: Value) {
    let Some((_, key)) = NAV_PATHS.iter().find(|(p, _)| *p == path) else {
        return;
    };
    if *key != "position" {
        pending.insert((*key).to_string(), value);
        return;
    }
    // "43.295438,5.361960" → lat + lon
    let Some(text) = value.as_str() else { return };
    let Some((lat, lon)) = text.split_once(',') else {
        return;
    };
    if let (Ok(lat), Ok(lon)) = (lat.trim().parse::<f64>(), lon.trim().parse::<f64>()) {
        pending.insert("lat".into(), json!(lat));
        pending.insert("lon".into(), json!(lon));
    }
}

/// Une connexion au MFD, de l'abonnement à la coupure. Rend `Ok(())` quand le
/// MFD ferme proprement, `Err` sur erreur réseau : dans les deux cas l'appelant
/// réessaie.
fn session(app: &AppHandle, mfd: &str) -> std::io::Result<()> {
    let addr = format!("{mfd}:{}", raydb::RAYDB_PORT);
    status(app, &format!("connexion à {addr}…"), mfd, false);

    let mut sock = TcpStream::connect(&addr)?;
    // Lecture non bloquante au-delà de 250 ms : sans données, la boucle doit
    // quand même tourner pour vider `pending` et rester réactive à l'arrêt.
    sock.set_read_timeout(Some(Duration::from_millis(250)))?;
    sock.write_all(&raydb::hello())?;
    for path in SUBSCRIPTIONS {
        sock.write_all(&raydb::subscribe(path))?;
    }
    status(app, &format!("connecté à {addr}"), mfd, true);

    let mut buf: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 65536];
    let mut pending = Map::new();
    let mut last_flush = Instant::now();
    loop {
        match sock.read(&mut chunk) {
            Ok(0) => return Ok(()), // le MFD a fermé la connexion
            Ok(n) => {
                buf.extend_from_slice(&chunk[..n]);
                for frame in raydb::take_frames(&mut buf) {
                    if let Some((path, value)) = raydb::decode_update(&frame) {
                        record(&mut pending, &path, value);
                    }
                }
            }
            Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {}
            Err(e) => return Err(e),
        }
        // Le MFD pousse par rafales ; on regroupe pour n'envoyer qu'à 5 Hz.
        if !pending.is_empty() && last_flush.elapsed() >= FLUSH {
            let delta = std::mem::take(&mut pending);
            snapshot().lock().unwrap().values.extend(delta.clone());
            let _ = app.emit("delta", Value::Object(delta));
            last_flush = Instant::now();
        }
    }
}

/// Hôte du MFD : celui imposé par `MFD_IP`, sinon une recherche mDNS. None si
/// personne n'a répondu dans le délai — l'appelant relancera. `misses` compte
/// les recherches vides consécutives, ce qui change le message affiché.
fn find_mfd(app: &AppHandle, misses: u32) -> Option<String> {
    match std::env::var("MFD_IP") {
        Ok(ip) if !ip.is_empty() => return Some(ip),
        _ => {}
    }
    status(app, "recherche du MFD (mDNS)…", "", false);
    let found = discovery::discover(DISCOVERY);
    if found.is_none() {
        // Une autorisation « Réseau local » refusée ne produit aucune erreur :
        // les recherches rendent simplement le vide, indéfiniment. Au bout de
        // quelques tours, c'est l'explication la plus probable — et la seule
        // que l'utilisateur puisse corriger.
        let text = if misses >= 2 && cfg!(any(target_os = "ios", target_os = "macos")) {
            "aucun MFD annoncé — autoriser le réseau local dans les Réglages ?"
        } else {
            "aucun MFD annoncé — nouvelle recherche…"
        };
        status(app, text, "", false);
    }
    found
}

/// La carte : chargée une fois, servie par le protocole `tiles:`.
///
/// macOS, et seulement avec la caractéristique `map` (voir `Cargo.toml`) : elle
/// est hors du build par défaut. Partout ailleurs — build ordinaire, iOS,
/// Android, page servie par la passerelle Python — `map_available()` rend
/// `null` et la vignette reste cachée, ce qui évite d'avoir à connaître la
/// plateforme côté page.
#[cfg(all(target_os = "macos", feature = "map"))]
mod map {
    use std::path::PathBuf;
    use std::sync::OnceLock;

    use tauri::http::{Request, Response, StatusCode};
    use tauri::{Manager, UriSchemeContext, UriSchemeResponder};

    use crate::mbtiles::Tiles;

    pub const SCHEME: &str = "tiles";

    /// Où chercher le jeu de tuiles, dans l'ordre :
    ///
    /// 1. `MFDVIEW_MBTILES`, qui tranche toujours ;
    /// 2. le premier `*.mbtiles` du dossier de données de l'app — l'endroit où
    ///    déposer sa carte une fois installée ;
    /// 3. `maps/` à la racine du dépôt, pour le développement.
    ///
    /// Le jeu SHOM pèse trois gigaoctets : il n'est ni empaqueté dans l'app ni
    /// versionné, d'où cette recherche plutôt qu'un chemin en dur.
    fn locate(app: &tauri::AppHandle) -> Option<PathBuf> {
        if let Ok(path) = std::env::var("MFDVIEW_MBTILES") {
            if !path.is_empty() {
                return Some(PathBuf::from(path));
            }
        }
        let dev = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../maps");
        let dirs = [app.path().app_data_dir().ok(), Some(dev)];
        dirs.into_iter().flatten().find_map(|dir| {
            std::fs::read_dir(dir).ok()?.flatten().find_map(|entry| {
                let path = entry.path();
                (path.extension()? == "mbtiles").then_some(path)
            })
        })
    }

    fn tiles() -> &'static OnceLock<Option<Tiles>> {
        static TILES: OnceLock<Option<Tiles>> = OnceLock::new();
        &TILES
    }

    /// Ouvre le jeu de tuiles, une fois pour toutes. Un échec n'est pas fatal :
    /// les instruments valent d'être affichés même sans carte.
    pub fn load(app: &tauri::AppHandle) {
        tiles().get_or_init(|| {
            let path = locate(app)?;
            match Tiles::open(&path) {
                Ok(t) => {
                    eprintln!("carte : {} (zoom max {})", t.path().display(), t.max_zoom);
                    Some(t)
                }
                Err(e) => {
                    eprintln!("carte ignorée — {e}");
                    None
                }
            }
        });
    }

    /// Ce que la page a besoin de savoir : jusqu'où zoomer, et où cadrer avant
    /// la première position. `null` s'il n'y a pas de carte.
    pub fn describe() -> serde_json::Value {
        let Some(t) = tiles().get().and_then(|t| t.as_ref()) else {
            return serde_json::Value::Null;
        };
        serde_json::json!({ "maxZoom": t.max_zoom, "bounds": t.bounds })
    }

    /// `tiles://localhost/{z}/{x}/{y}` → l'image, telle qu'elle est stockée.
    ///
    /// Asynchrone : la lecture SQLite se fait hors du thread de la webview, qui
    /// demande une dizaine de tuiles d'un coup à chaque déplacement.
    pub fn serve(_ctx: UriSchemeContext<'_, tauri::Wry>, req: Request<Vec<u8>>, out: UriSchemeResponder) {
        let path = req.uri().path().to_string();
        std::thread::spawn(move || {
            let coords = || -> Option<(u8, u32, u32)> {
                let mut parts = path.trim_start_matches('/').split('/');
                let z = parts.next()?.parse().ok()?;
                let x = parts.next()?.parse().ok()?;
                let y = parts.next()?.parse().ok()?;
                parts.next().is_none().then_some((z, x, y))
            };
            let tile = coords()
                .and_then(|(z, x, y)| tiles().get()?.as_ref()?.tile(z, x, y));

            out.respond(match tile {
                // Les tuiles ne changent jamais : la webview peut les garder.
                Some((blob, mime)) => Response::builder()
                    .header("Content-Type", mime)
                    .header("Cache-Control", "max-age=31536000, immutable")
                    .body(blob)
                    .unwrap(),
                // Hors couverture : Leaflet laisse simplement le fond nu.
                None => Response::builder()
                    .status(StatusCode::NOT_FOUND)
                    .body(Vec::new())
                    .unwrap(),
            });
        });
    }
}

/// Décrit la carte disponible, ou **`null` quand il n'y en a pas** — auquel cas
/// la page n'affiche pas la vignette. C'est le cas partout sauf sur macOS
/// compilé `--features map` et pourvu d'un fichier de tuiles.
///
/// La commande existe dans tous les cas : la page l'appelle sans condition, et
/// c'est cette réponse — non la plateforme — qui décide de la carte.
#[tauri::command]
fn map_available() -> Value {
    #[cfg(all(target_os = "macos", feature = "map"))]
    {
        map::describe()
    }
    #[cfg(not(all(target_os = "macos", feature = "map")))]
    {
        Value::Null
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let wake = Arc::new(Wake::default());

    let builder = tauri::Builder::default().invoke_handler(tauri::generate_handler![map_available]);
    #[cfg(all(target_os = "macos", feature = "map"))]
    let builder = builder.register_asynchronous_uri_scheme_protocol(map::SCHEME, map::serve);

    let app = builder
        .setup({
            let wake = wake.clone();
            move |app| {
                // macOS : un item de menu « Réduire la fenêtre » (⌘−), qui
                // rétrécit la fenêtre d'un cran (85 %) sans passer sous la
                // taille minimale. On repart du menu standard pour garder
                // Quitter, Édition, etc., et on y ajoute un sous-menu « Taille ».
                #[cfg(target_os = "macos")]
                {
                    use tauri::menu::{Menu, MenuItemBuilder, SubmenuBuilder};

                    // Bornes et taille par défaut, alignées sur tauri.conf.json.
                    const MIN_W: f64 = 360.0;
                    const MIN_H: f64 = 520.0;
                    const DEF_W: f64 = 460.0;
                    const DEF_H: f64 = 900.0;
                    const STEP: f64 = 0.85; // un cran de réduction ; l'inverse agrandit

                    // Installation non fatale : si le menu ne se pose pas (ex.
                    // accélérateur refusé), on le signale sans faire échouer le
                    // lancement de l'app.
                    let install = || -> tauri::Result<()> {
                        let reduce =
                            MenuItemBuilder::with_id("win_reduce", "Réduire la fenêtre")
                                .accelerator("Cmd+Minus")
                                .build(app.handle())?;
                        let enlarge =
                            MenuItemBuilder::with_id("win_enlarge", "Agrandir la fenêtre")
                                .accelerator("Cmd+=")
                                .build(app.handle())?;
                        let reset = MenuItemBuilder::with_id("win_reset", "Taille par défaut")
                            .accelerator("Cmd+0")
                            .build(app.handle())?;
                        let (id_reduce, id_enlarge, id_reset) =
                            (reduce.id().clone(), enlarge.id().clone(), reset.id().clone());

                        let menu = Menu::default(app.handle())?;
                        let taille = SubmenuBuilder::new(app.handle(), "Taille")
                            .item(&reduce)
                            .item(&enlarge)
                            .item(&reset)
                            .build()?;
                        menu.append(&taille)?;
                        app.set_menu(menu)?;

                        app.on_menu_event(move |app, event| {
                            let id = event.id();
                            let Some(win) = app.get_webview_window("main") else {
                                return;
                            };
                            let (w, h) = if id == &id_reset {
                                (DEF_W, DEF_H)
                            } else if id == &id_reduce || id == &id_enlarge {
                                let Ok(size) = win.inner_size() else {
                                    return;
                                };
                                let scale = win.scale_factor().unwrap_or(1.0);
                                let cur = size.to_logical::<f64>(scale);
                                let f = if id == &id_reduce { STEP } else { 1.0 / STEP };
                                ((cur.width * f).max(MIN_W), (cur.height * f).max(MIN_H))
                            } else {
                                return;
                            };
                            let _ = win.set_size(tauri::LogicalSize::new(w, h));
                        });
                        Ok(())
                    };
                    if let Err(e) = install() {
                        eprintln!("MFDView : menu « Taille » non installé — {e}");
                    }
                }

                // La carte d'abord : la page interroge `map_available` dès son
                // chargement, qui suit de peu.
                #[cfg(all(target_os = "macos", feature = "map"))]
                map::load(app.handle());

                // La page annonce que ses écoutes sont en place ; on lui sert
                // alors ce qu'elle a manqué. Elle le refait à chaque
                // rechargement, d'où un `listen` et non un `once`.
                let ready = app.handle().clone();
                app.listen("ready", move |_| {
                    let snapshot = snapshot().lock().unwrap();
                    if !snapshot.values.is_empty() {
                        let _ = ready.emit("delta", Value::Object(snapshot.values.clone()));
                    }
                    if let Some(status) = &snapshot.status {
                        let _ = ready.emit("status", status.clone());
                    }
                });

                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    let mut misses = 0;
                    loop {
                        // Redécouverte à chaque tour : le MFD peut avoir changé
                        // d'adresse entre deux connexions (bascule Wi-Fi,
                        // redémarrage du bord).
                        match find_mfd(&handle, misses) {
                            Some(mfd) => {
                                misses = 0;
                                match session(&handle, &mfd) {
                                    Ok(()) => status(
                                        &handle,
                                        "connexion fermée — reconnexion…",
                                        &mfd,
                                        false,
                                    ),
                                    Err(e) => status(
                                        &handle,
                                        &format!("{mfd} : {e} — reconnexion…"),
                                        &mfd,
                                        false,
                                    ),
                                }
                            }
                            None => misses += 1,
                        }
                        wake.sleep(RETRY);
                    }
                });
                Ok(())
            }
        })
        .build(tauri::generate_context!())
        .expect("erreur au lancement de MFDView");

    app.run(move |_app, event| {
        if let RunEvent::Resumed = event {
            wake.poke();
        }
    });
}
