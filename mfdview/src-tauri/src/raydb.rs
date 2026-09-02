//! Protocole RayDB (TCP 23333) — portage Rust de `raydb_decode.py`.
//!
//! Le module ne connaît que le protocole : construire HELLO et SUBSCRIBE,
//! découper le flux en trames, décoder un UPDATE. Le rangement des valeurs et
//! leur envoi vers la page sont dans `main.rs`.
//!
//! Trame :  [u32 len][u32 msg_type][u8 op][u32 path_len][u32 pad][path][valeur]
//! `len` couvre tout ce qui suit le champ len lui-même. Tout est little-endian.
//!
//! Bloc valeur : [3 octets réservés][u32 type][valeur][4 octets de queue].
//!   0 bool(1)  1 i8(1)  2 i16(2)  3 i32(4)  4 i64(8)  7 u32(4)
//!   9 f32(4)  10 f64(8)  11 chaîne [u64 len][octets]
//!   13 liste : [u64 n] puis n × ([u32 type][valeur])
//!   14 table : [u64 n] puis n × ([u64 klen][clé][u32 type][valeur])
//! Une valeur suit immédiatement son type ; seules les chaînes portent une
//! longueur. Voir « docs/2. protocole-raydb-23333.md » §5.1.

use serde_json::{json, Map, Value};

pub const RAYDB_PORT: u16 = 23333;

const MSG_TYPE: u32 = 1;
const OP_SUBSCRIBE: u8 = 3;
const OP_UPDATE: u8 = 4;
const OP_HELLO: u8 = 7;
const TYPE_STR: u32 = 11; // [u64 len][octets]
const TYPE_LIST: u32 = 13; // [u64 n] puis n valeurs typées
const TYPE_NAMED: u32 = 14; // table [u64 n] puis n × [clé, valeur]

fn u32le(b: &[u8], o: usize) -> Option<u32> {
    Some(u32::from_le_bytes(b.get(o..o + 4)?.try_into().ok()?))
}

fn u64le(b: &[u8], o: usize) -> Option<u64> {
    Some(u64::from_le_bytes(b.get(o..o + 8)?.try_into().ok()?))
}

/// Le MFD laisse traîner de vieux octets derrière le NUL final : on coupe.
fn cstr(b: &[u8]) -> String {
    let end = b.iter().position(|&c| c == 0).unwrap_or(b.len());
    String::from_utf8_lossy(&b[..end]).into_owned()
}

/// Trame de requête. Les 3 octets « réservés » sont opaques : repris tels quels
/// des captures (ils diffèrent selon l'opcode) pour rester acceptés par le MFD.
fn request(op: u8, path: &str, reserved: [u8; 3]) -> Vec<u8> {
    let p = path.as_bytes();
    let mut body = Vec::with_capacity(29 + p.len());
    body.extend_from_slice(&MSG_TYPE.to_le_bytes());
    body.push(op);
    body.extend_from_slice(&(p.len() as u32).to_le_bytes());
    body.extend_from_slice(&0u32.to_le_bytes()); // pad / flags
    body.extend_from_slice(p);
    body.extend_from_slice(&reserved); // bloc valeur « vide »
    body.extend_from_slice(&7u32.to_le_bytes());
    body.extend_from_slice(&0u64.to_le_bytes());

    let mut frame = Vec::with_capacity(4 + body.len());
    frame.extend_from_slice(&(body.len() as u32).to_le_bytes());
    frame.extend_from_slice(&body);
    frame
}

/// Le client s'annonce — mêmes octets que le vrai client Raymarine.
pub fn hello() -> Vec<u8> {
    request(OP_HELLO, "RayDBRemoteClient", [0x00, 0x01, 0x01])
}

/// Abonnement à un chemin (`data/#` = tout le sous-arbre navigation).
pub fn subscribe(path: &str) -> Vec<u8> {
    request(OP_SUBSCRIBE, path, [0x01, 0x00, 0x00])
}

/// Extrait du tampon toutes les trames complètes, et ne laisse que le reste.
/// Une lecture TCP peut couper au milieu d'une trame ou en livrer plusieurs.
pub fn take_frames(buf: &mut Vec<u8>) -> Vec<Vec<u8>> {
    let mut frames = Vec::new();
    let mut o = 0;
    while buf.len() - o >= 4 {
        let len = match u32le(buf, o) {
            Some(v) => v as usize,
            None => break,
        };
        if len < 13 {
            break; // longueur invalide : flux désynchronisé, on n'avance plus
        }
        let total = 4 + len;
        if buf.len() - o < total {
            break; // trame incomplète : on attend la suite
        }
        frames.push(buf[o..o + total].to_vec());
        o += total;
    }
    buf.drain(..o);
    frames
}

/// Lit la valeur de type `vtype` à l'offset `o`, et rend l'offset de fin.
///
/// L'énumération des types se lit comme une suite d'entiers de largeur
/// croissante, puis les composites (§5.1 de la spec). Les listes et les tables
/// s'imbriquent, d'où la récursion. 5, 6, 8 et 12 n'ont jamais été observés ;
/// leur place dans la suite est déduite de leurs voisins.
fn read_value(b: &[u8], o: usize, vtype: u32) -> Option<(Value, usize)> {
    let fixed = |n: usize| -> Option<&[u8]> { b.get(o..o + n) };
    match vtype {
        0 => Some((json!(*b.get(o)? != 0), o + 1)),
        1 => Some((json!(*b.get(o)? as i8), o + 1)),
        2 => Some((json!(i16::from_le_bytes(fixed(2)?.try_into().ok()?)), o + 2)),
        3 => Some((json!(i32::from_le_bytes(fixed(4)?.try_into().ok()?)), o + 4)),
        4 => Some((json!(i64::from_le_bytes(fixed(8)?.try_into().ok()?)), o + 8)),
        5 => Some((json!(*b.get(o)?), o + 1)),
        6 => Some((json!(u16::from_le_bytes(fixed(2)?.try_into().ok()?)), o + 2)),
        7 => Some((json!(u32le(b, o)?), o + 4)),
        8 => Some((json!(u64le(b, o)?), o + 8)),
        9 => Some((
            json!(f32::from_le_bytes(fixed(4)?.try_into().ok()?) as f64),
            o + 4,
        )),
        10 => Some((json!(f64::from_le_bytes(fixed(8)?.try_into().ok()?)), o + 8)),
        TYPE_STR => {
            let n = u64le(b, o)? as usize;
            Some((json!(cstr(b.get(o + 8..o + 8 + n)?)), o + 8 + n))
        }
        TYPE_LIST | TYPE_NAMED => {
            let n = u64le(b, o)? as usize;
            if n > b.len() {
                return None; // compteur aberrant : bloc tronqué ou mal cadré
            }
            let mut o = o + 8;
            let mut items = Vec::new();
            let mut map = Map::new();
            for _ in 0..n {
                let mut key = String::new();
                if vtype == TYPE_NAMED {
                    let klen = u64le(b, o)? as usize;
                    o += 8;
                    key = cstr(b.get(o..o + klen)?);
                    o += klen;
                }
                let (value, end) = read_value(b, o + 4, u32le(b, o)?)?;
                o = end;
                if vtype == TYPE_NAMED {
                    map.insert(key, value);
                } else {
                    items.push(value);
                }
            }
            let out = if vtype == TYPE_NAMED {
                Value::Object(map)
            } else {
                Value::Array(items)
            };
            Some((out, o))
        }
        _ => None,
    }
}

/// Bloc valeur d'un UPDATE : [3 réservés][u32 type][valeur][4 octets de queue].
fn typed(b: &[u8]) -> Option<Value> {
    if b.len() < 8 || b[0..3] != [0, 0, 0] {
        return None;
    }
    let vtype = u32le(b, 3)?;
    // Table à entrée unique — la forme des `diag/…` : le nom réencodé y double
    // le plus souvent le dernier segment du chemin, que la page a déjà comme
    // clé, donc on ne rend que la valeur. Au-delà d'une entrée, la table est
    // rendue telle quelle : n'en garder que la première perdait le reste en
    // silence (`data/exportedCamera/…` porte six champs).
    if vtype == TYPE_NAMED && u64le(b, 7)? == 1 {
        let klen = u64le(b, 15)? as usize;
        let o = 23 + klen;
        return Some(read_value(b, o + 4, u32le(b, o)?)?.0);
    }
    Some(read_value(b, 7, vtype)?.0)
}

/// (chemin, valeur) d'une trame UPDATE ; None pour les autres opcodes.
pub fn decode_update(frame: &[u8]) -> Option<(String, Value)> {
    if frame.len() < 17 || frame[8] != OP_UPDATE {
        return None;
    }
    let plen = u32le(frame, 9)? as usize;
    let path = cstr(frame.get(17..17 + plen)?);
    Some((path, typed(frame.get(17 + plen..)?)?))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Blocs valeur relevés tels quels dans `pcap/boat-c_*.pcap` — un par type.
    fn block(hex: &str) -> Vec<u8> {
        (0..hex.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
            .collect()
    }

    #[test]
    fn scalaires() {
        // Diagnostics/CrashLogCount_3712024258 = 337 (i32, type 3)
        assert_eq!(typed(&block("000000030000005101000000000000")), Some(json!(337)));
        // Settings/Data/-/10/2/-/-/-/- = 1 (i64, type 4)
        assert_eq!(
            typed(&block("00000004000000010000000000000000000000")),
            Some(json!(1))
        );
    }

    #[test]
    fn liste() {
        // AvailableCartoVendorClient/availableSysCarto = [1]
        let b = block("0000000d000000010000000000000007000000010000000000000000");
        assert_eq!(typed(&b), Some(json!([1])));
    }

    #[test]
    fn table_a_plusieurs_entrees() {
        // Settings/Data/-/3/89/-/-/-/- relevé dans pcap/boat-c_axiom7.pcap :
        // cinq entrées i32, que l'ancien décodeur réduisait à la première.
        let b = block(concat!(
            "0000000e00000005000000000000000300000000000000484c",
            "5303000000000000000300000000000000484d530300000000",
            "00000003000000000000004c4c530300000000000000030000",
            "00000000004c4d530300000000000000060000000000000056",
            "656e646f72030000000100000000000000"
        ));
        assert_eq!(
            typed(&b),
            Some(json!({"HLS": 0, "HMS": 0, "LLS": 0, "LMS": 0, "Vendor": 1}))
        );
    }

    #[test]
    fn table_a_entree_unique_rend_la_valeur_nue() {
        // diag/… : [count=1][klen=3]["HLS"][type 3][valeur 0]
        let b = block("0000000e00000001000000000000000300000000000000484c530300000000000000");
        assert_eq!(typed(&b), Some(json!(0)));
    }
}
