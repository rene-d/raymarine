//! Découverte par `mdns-sd`, une pile mDNS en Rust pur — donc une socket
//! multicast ouverte par l'application elle-même.
//!
//! C'est le chemin des plateformes sans démon Bonjour à disposition. Sur
//! Android, `android.rs` prend un `MulticastLock` le temps de la recherche,
//! sans quoi le Wi-Fi filtre le multicast dès que l'écran s'éteint. Sur
//! Apple, voir `apple.rs` : le multicast brut y coûterait un entitlement.

#[cfg(target_os = "android")]
mod android;

use std::net::IpAddr;
use std::time::{Duration, Instant};

use mdns_sd::{ServiceDaemon, ServiceEvent};

/// Services Raymarine porteurs de l'adresse du MFD.
const SERVICES: [&str; 2] = ["_raydb._tcp.local.", "_rym_rrc._tcp.local."];

/// Première adresse IPv4 annoncée, ou None si rien n'est vu dans le délai.
pub fn discover(budget: Duration) -> Option<String> {
    // Pris avant d'ouvrir la socket, relâché par `Drop` à la sortie — y
    // compris sur les retours anticipés ci-dessous. No-op ailleurs
    // qu'Android : le type n'existe même pas hors de ce `cfg`.
    #[cfg(target_os = "android")]
    let _lock = android::MulticastGuard::acquire();

    let daemon = ServiceDaemon::new().ok()?;
    let browsers: Vec<_> = SERVICES
        .iter()
        .filter_map(|service| daemon.browse(service).ok())
        .collect();

    let deadline = Instant::now() + budget;
    let mut found = None;
    while found.is_none() && Instant::now() < deadline {
        // Chaque service a sa file ; on les interroge à tour de rôle plutôt que
        // d'épuiser le délai sur le premier, qui peut n'être annoncé par
        // personne (un MFD sans RayDB actif annonce quand même _rym_rrc).
        for rx in &browsers {
            if let Ok(ServiceEvent::ServiceResolved(info)) =
                rx.recv_timeout(Duration::from_millis(200))
            {
                // `ScopedIp` porte l'interface d'où vient l'annonce (utile en
                // IPv6 lien-local) ; ici seule l'adresse nous intéresse.
                found = info
                    .get_addresses()
                    .iter()
                    .map(|scoped| scoped.to_ip_addr())
                    .find(IpAddr::is_ipv4)
                    .map(|ip| ip.to_string());
                if found.is_some() {
                    break;
                }
            }
        }
    }
    let _ = daemon.shutdown();
    found
}
