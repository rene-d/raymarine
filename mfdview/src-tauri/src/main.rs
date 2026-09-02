//! Point d'entrée du binaire de bureau — tout le travail est dans `lib.rs`.
//!
//! Sur mobile, ce fichier n'est pas compilé : l'app iOS est un projet Xcode qui
//! lie `mfdview_lib` en bibliothèque statique et appelle `run()` par le point
//! d'entrée posé par `#[tauri::mobile_entry_point]`.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    mfdview_lib::run()
}
