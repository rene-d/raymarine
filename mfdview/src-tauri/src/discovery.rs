//! Découverte du MFD par mDNS / Bonjour — portage de `_discover_via_mdns`
//! (`raydb_client.py`).
//!
//! Le MFD annonce trois services ; deux portent son adresse joignable :
//! `_raydb._tcp` et `_rym_rrc._tcp`. On ne retient que **l'hôte**.
//!
//! **Le port annoncé est ignoré, volontairement** : le MFD publie 49111 pour
//! `_raydb._tcp` alors que RayDB écoute sur 23333 (le simulateur reproduit ce
//! piège, cf. `mfdsim/README.md`). Un client qui fait confiance à l'annonce se
//! connecte dans le vide.
//!
//! Deux implémentations derrière la même fonction :
//!
//! - **Apple** (`apple.rs`) — l'API DNS-SD de libSystem, c'est-à-dire le démon
//!   `mDNSResponder` du système. Aucune socket multicast n'est ouverte par
//!   l'application : c'est ce qui permet à l'app iOS de se passer de
//!   l'entitlement `com.apple.developer.networking.multicast`. Utilisée aussi
//!   sur macOS, pour que le chemin iOS se mette au point sans Xcode.
//! - **Ailleurs** (`mdns_sd.rs`) — la pile mDNS en Rust pur `mdns-sd`, qui ouvre
//!   sa propre socket multicast. Sans histoire sous Linux et Windows ; sur
//!   Android, `mdns_sd/android.rs` prend un `MulticastLock` par JNI directe le
//!   temps de la recherche.

#[cfg(any(target_os = "ios", target_os = "macos"))]
mod apple;
#[cfg(any(target_os = "ios", target_os = "macos"))]
pub use apple::discover;

#[cfg(not(any(target_os = "ios", target_os = "macos")))]
mod mdns_sd;
#[cfg(not(any(target_os = "ios", target_os = "macos")))]
pub use mdns_sd::discover;
