//! Lecture d'un jeu de tuiles MBTiles — la carte marine, hors ligne.
//!
//! Un MBTiles est une base SQLite : une table `tiles(zoom_level, tile_column,
//! tile_row, tile_data)` où chaque `tile_data` est une image complète. Le module
//! ne fait que la retrouver ; l'affichage est côté page (Leaflet).
//!
//! Deux pièges du format, tous deux vérifiés sur le jeu SHOM du dépôt :
//!
//! - **L'axe Y est inversé.** MBTiles numérote les lignes en TMS, origine en bas,
//!   quand les URL de tuiles (et Leaflet) comptent depuis le haut : d'où le
//!   `2^z - 1 - y` de `tile()`.
//! - **`metadata.format` ment.** Il annonce `png` alors que le jeu mêle ~75 % de
//!   JPEG et ~25 % de PNG, à tous les niveaux de zoom. Le type MIME est donc
//!   déduit des octets de signature, jamais de la métadonnée.
//!
//! macOS uniquement : la carte n'existe pas sur les autres plateformes, et
//! `Cargo.toml` n'y compile même pas SQLite.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use rusqlite::{Connection, OpenFlags};

/// Les tuiles d'un jeu, prêtes à être servies.
///
/// La connexion est sérialisée par un `Mutex` : les lectures sont courtes (un
/// index unique, quelques kilo-octets) et les demandes de la webview arrivent
/// par paquets d'une dizaine. Un pool ne se justifierait qu'à la mesure.
pub struct Tiles {
    db: Mutex<Connection>,
    path: PathBuf,
    /// Zoom maximal réellement présent : au-delà, Leaflet agrandit la dernière
    /// tuile disponible plutôt que d'afficher du vide.
    pub max_zoom: u8,
    /// Emprise `[ouest, sud, est, nord]` en degrés, telle qu'annoncée par la
    /// métadonnée. Sert à cadrer la carte tant qu'aucune position n'est reçue,
    /// et à éviter de demander des tuiles hors couverture.
    pub bounds: Option<[f64; 4]>,
}

/// Type MIME déduit des premiers octets. `None` si ce n'est pas une image
/// connue — le blob serait alors chiffré, ou le fichier corrompu.
fn sniff(blob: &[u8]) -> Option<&'static str> {
    match blob {
        [0xff, 0xd8, 0xff, ..] => Some("image/jpeg"),
        [0x89, b'P', b'N', b'G', ..] => Some("image/png"),
        [b'R', b'I', b'F', b'F', _, _, _, _, b'W', b'E', b'B', b'P', ..] => Some("image/webp"),
        _ => None,
    }
}

/// Rend le blob affichable. C'est ici, et nulle part ailleurs, que viendrait se
/// greffer un déchiffrement (jeu chiffré par tuile) : la couture est unique.
fn decode(blob: Vec<u8>) -> Option<(Vec<u8>, &'static str)> {
    let mime = sniff(&blob)?;
    Some((blob, mime))
}

impl Tiles {
    /// Ouvre un jeu en lecture seule. Échoue si le fichier n'est pas un MBTiles
    /// exploitable — l'appelant s'en passe alors, sans empêcher les instruments
    /// de fonctionner.
    pub fn open(path: &Path) -> Result<Self, String> {
        let db = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
        )
        .map_err(|e| format!("{} : {e}", path.display()))?;

        let max_zoom: u8 = db
            .query_row("SELECT max(zoom_level) FROM tiles", [], |r| r.get(0))
            .map_err(|e| format!("{} : table `tiles` illisible ({e})", path.display()))?;

        // L'emprise est facultative — un jeu sans métadonnée reste utilisable,
        // la carte s'ouvrira simplement sur la position du bateau.
        let bounds = db
            .query_row("SELECT value FROM metadata WHERE name = 'bounds'", [], |r| {
                r.get::<_, String>(0)
            })
            .ok()
            .and_then(|text| {
                let mut it = text.split(',').map(|v| v.trim().parse::<f64>());
                let mut next = || it.next()?.ok();
                Some([next()?, next()?, next()?, next()?])
            });

        Ok(Self {
            db: Mutex::new(db),
            path: path.to_path_buf(),
            max_zoom,
            bounds,
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// La tuile aux coordonnées **XYZ** (origine en haut à gauche, celles des
    /// URL), avec son type MIME. `None` si elle n'est pas dans le jeu — cas
    /// banal : la couverture est un rectangle, pas le monde.
    pub fn tile(&self, z: u8, x: u32, y: u32) -> Option<(Vec<u8>, &'static str)> {
        let row = (1u32 << z).checked_sub(1)?.checked_sub(y)?; // XYZ → TMS
        let db = self.db.lock().ok()?;
        let blob: Vec<u8> = db
            .query_row(
                "SELECT tile_data FROM tiles \
                 WHERE zoom_level = ?1 AND tile_column = ?2 AND tile_row = ?3",
                (z, x, row),
                |r| r.get(0),
            )
            .ok()?;
        decode(blob)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Le jeu SHOM du dépôt : 3 Gio, hors dépôt Git. Les tests qui en dépendent
    /// s'effacent quand il n'est pas là plutôt que d'échouer sur une autre
    /// machine.
    fn fixture() -> Option<Tiles> {
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../maps/shom.mbtiles");
        path.exists().then(|| Tiles::open(&path).unwrap())
    }

    #[test]
    fn sniff_reconnait_les_deux_formats_du_jeu() {
        assert_eq!(sniff(&[0xff, 0xd8, 0xff, 0xe0]), Some("image/jpeg"));
        assert_eq!(sniff(b"\x89PNG\r\n\x1a\n"), Some("image/png"));
        assert_eq!(sniff(b"chiffre?"), None);
    }

    /// Tuile du mouillage de `mfdsim` (48.654 N, 3.879 W) au zoom 16, dont les
    /// coordonnées XYZ ont été calculées à part : elle valide d'un coup
    /// l'ouverture, la bascule TMS et le reniflage.
    #[test]
    fn tuile_du_mouillage() {
        let Some(tiles) = fixture() else { return };
        let (blob, mime) = tiles.tile(16, 32061, 22602).expect("tuile absente");
        assert_eq!(mime, "image/jpeg");
        assert_eq!(blob.len(), 6421);
        assert!(tiles.max_zoom >= 16);

        // L'emprise annoncée doit contenir le mouillage (48.654 N, 3.879 W).
        let [west, south, east, north] = tiles.bounds.expect("emprise absente");
        assert!(west < -3.879 && -3.879 < east);
        assert!(south < 48.654 && 48.654 < north);
    }

    #[test]
    fn hors_couverture_rend_none() {
        let Some(tiles) = fixture() else { return };
        assert!(tiles.tile(16, 0, 0).is_none()); // au large de l'emprise
        assert!(tiles.tile(2, 0, 9).is_none()); // y hors de la grille du zoom
    }
}
