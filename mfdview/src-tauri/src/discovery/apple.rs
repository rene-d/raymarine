//! Découverte par le démon Bonjour du système (`mDNSResponder`), via l'API
//! DNS-SD de libSystem — `dns_sd.h`, présente sur macOS comme sur iOS et liée
//! automatiquement (les symboles vivent dans libSystem, pas de `#[link]`).
//!
//! **Pourquoi pas `mdns-sd` ici** : c'est une pile mDNS en Rust pur, elle ouvre
//! elle-même une socket multicast. Sur iOS, le multicast brut exige
//! l'entitlement `com.apple.developer.networking.multicast`, accordé par Apple
//! sur demande et au cas par cas. En passant par DNS-SD, c'est le démon système
//! qui fait le multicast pour nous : il suffit de déclarer les types de service
//! dans `NSBonjourServices` (`Info.ios.plist`) et l'app reste ordinaire.
//! `NWBrowser` en Swift ferait la même chose, en passant par le même démon,
//! mais imposerait un plugin Tauri mobile et un pont vers Rust.
//!
//! Le déroulé est celui de Bonjour, en trois temps : `DNSServiceBrowse` rend
//! des *noms d'instance*, `DNSServiceResolve` transforme un nom en *hôte*
//! (`AXIOM-1234.local`), `DNSServiceGetAddrInfo` transforme l'hôte en
//! *adresse*.
//!
//! Le troisième temps a l'air superflu — `TcpStream::connect` sait résoudre un
//! nom `.local`, par ce même démon. Il coûte pourtant **5 s à chaque
//! connexion** : `getaddrinfo` demande A et AAAA, et le MFD n'annonçant pas
//! d'IPv6, la question AAAA reste sans réponse jusqu'au délai de garde — il n'y
//! a pas de « non » en mDNS. En demandant ici IPv4 seule, la réponse arrive en
//! quelques millisecondes.
//!
//! L'API est asynchrone par nature : chaque `DNSServiceRef` porte un descripteur
//! (`DNSServiceRefSockFD`) qui devient lisible quand une réponse arrive, et
//! `DNSServiceProcessResult` la lit puis appelle notre callback — sur ce
//! thread, jamais en parallèle. Un `poll()` sur ces descripteurs suffit donc à
//! garder ici une fonction bloquante à budget, comme sur les autres
//! plateformes.

use std::ffi::{CStr, CString};
use std::os::raw::{c_char, c_int, c_uchar, c_void};
use std::time::{Duration, Instant};

/// Services Raymarine porteurs de l'adresse du MFD. Ces deux chaînes sont à
/// tenir en phase avec `NSBonjourServices` dans `Info.ios.plist` : iOS filtre
/// silencieusement toute recherche non déclarée.
const SERVICES: [&str; 2] = ["_raydb._tcp", "_rym_rrc._tcp"];

/// `kDNSServiceInterfaceIndexAny` — toutes les interfaces.
const IFACE_ANY: u32 = 0;
/// `kDNSServiceFlagsAdd` — service apparu (par opposition à un retrait).
const FLAG_ADD: u32 = 0x2;
/// `kDNSServiceProtocol_IPv4` — ne pas demander d'AAAA, cf. l'en-tête.
const PROTOCOL_IPV4: u32 = 0x1;

// ---------------------------------------------------------------- FFI dns_sd

/// Handle opaque de l'API DNS-SD (`struct _DNSServiceRef_t *`).
#[repr(C)]
struct DnsService {
    _opaque: [u8; 0],
}
type DnsServiceRef = *mut DnsService;

type BrowseReply = extern "C" fn(
    DnsServiceRef,
    u32,           // flags
    u32,           // interfaceIndex
    i32,           // errorCode
    *const c_char, // serviceName
    *const c_char, // regtype
    *const c_char, // replyDomain
    *mut c_void,   // context
);

type ResolveReply = extern "C" fn(
    DnsServiceRef,
    u32,            // flags
    u32,            // interfaceIndex
    i32,            // errorCode
    *const c_char,  // fullname
    *const c_char,  // hosttarget
    u16,            // port, en ordre réseau — ignoré
    u16,            // txtLen
    *const c_uchar, // txtRecord
    *mut c_void,    // context
);

type AddrReply = extern "C" fn(
    DnsServiceRef,
    u32,                   // flags
    u32,                   // interfaceIndex
    i32,                   // errorCode
    *const c_char,         // hostname
    *const libc::sockaddr, // address
    u32,                   // ttl
    *mut c_void,           // context
);

extern "C" {
    fn DNSServiceBrowse(
        sd: *mut DnsServiceRef,
        flags: u32,
        interface: u32,
        regtype: *const c_char,
        domain: *const c_char,
        reply: BrowseReply,
        context: *mut c_void,
    ) -> i32;

    fn DNSServiceResolve(
        sd: *mut DnsServiceRef,
        flags: u32,
        interface: u32,
        name: *const c_char,
        regtype: *const c_char,
        domain: *const c_char,
        reply: ResolveReply,
        context: *mut c_void,
    ) -> i32;

    fn DNSServiceGetAddrInfo(
        sd: *mut DnsServiceRef,
        flags: u32,
        interface: u32,
        protocol: u32,
        hostname: *const c_char,
        reply: AddrReply,
        context: *mut c_void,
    ) -> i32;

    fn DNSServiceRefSockFD(sd: DnsServiceRef) -> c_int;
    fn DNSServiceProcessResult(sd: DnsServiceRef) -> i32;
    fn DNSServiceRefDeallocate(sd: DnsServiceRef);
}

/// Une requête en cours, libérée à la sortie de portée. Tant qu'elle vit, le
/// démon peut appeler notre callback avec le contexte qu'on lui a confié : les
/// `Ref` doivent donc mourir **avant** les contextes.
struct Query(DnsServiceRef);

impl Query {
    fn fd(&self) -> c_int {
        unsafe { DNSServiceRefSockFD(self.0) }
    }

    /// Lit une réponse en attente et appelle le callback correspondant.
    fn process(&self) {
        unsafe { DNSServiceProcessResult(self.0) };
    }
}

impl Drop for Query {
    fn drop(&mut self) {
        unsafe { DNSServiceRefDeallocate(self.0) }
    }
}

// ------------------------------------------------------------------ contextes

/// Instance annoncée, telle que `DNSServiceResolve` la réclame.
struct Instance {
    name: CString,
    regtype: CString,
    domain: CString,
    interface: u32,
}

/// Contexte des recherches : les instances vues depuis le dernier tour.
#[derive(Default)]
struct Seen {
    instances: Vec<Instance>,
}

/// Contexte des résolutions : les hôtes obtenus depuis le dernier tour, avec
/// l'interface d'où ils viennent.
#[derive(Default)]
struct Hosts {
    hosts: Vec<(CString, u32)>,
}

/// Contexte des demandes d'adresse : la première adresse obtenue gagne.
#[derive(Default)]
struct Found {
    addr: Option<String>,
}

fn owned(s: *const c_char) -> Option<CString> {
    (!s.is_null()).then(|| unsafe { CStr::from_ptr(s) }.to_owned())
}

extern "C" fn on_browse(
    _sd: DnsServiceRef,
    flags: u32,
    interface: u32,
    error: i32,
    name: *const c_char,
    regtype: *const c_char,
    domain: *const c_char,
    context: *mut c_void,
) {
    // Un retrait (`Add` absent) ne nous apprend rien : on cherche une adresse,
    // pas à tenir l'inventaire du réseau.
    if error != 0 || flags & FLAG_ADD == 0 {
        return;
    }
    let (Some(name), Some(regtype), Some(domain)) = (owned(name), owned(regtype), owned(domain))
    else {
        return;
    };
    // SÛRETÉ : `context` vient du `Box::into_raw` de `discover`, vivant tant que
    // la requête l'est, et ce callback n'est appelé que depuis notre appel à
    // `DNSServiceProcessResult`, sur le thread de `discover`.
    let seen = unsafe { &mut *(context as *mut Seen) };
    seen.instances.push(Instance {
        name,
        regtype,
        domain,
        interface,
    });
}

extern "C" fn on_resolve(
    _sd: DnsServiceRef,
    _flags: u32,
    interface: u32,
    error: i32,
    _fullname: *const c_char,
    hosttarget: *const c_char,
    _port: u16, // 49111 pour `_raydb._tcp` : mensonger, cf. l'en-tête du module
    _txt_len: u16,
    _txt: *const c_uchar,
    context: *mut c_void,
) {
    if error != 0 {
        return;
    }
    let Some(host) = owned(hosttarget) else {
        return;
    };
    // SÛRETÉ : voir `on_browse`.
    let hosts = unsafe { &mut *(context as *mut Hosts) };
    hosts.hosts.push((host, interface));
}

extern "C" fn on_addr(
    _sd: DnsServiceRef,
    _flags: u32,
    _interface: u32,
    error: i32,
    _hostname: *const c_char,
    address: *const libc::sockaddr,
    _ttl: u32,
    context: *mut c_void,
) {
    if error != 0 || address.is_null() {
        return;
    }
    // On n'a demandé que de l'IPv4, mais le démon reste maître de ce qu'il rend.
    let sa = unsafe { &*address };
    if i32::from(sa.sa_family) != libc::AF_INET {
        return;
    }
    let sin = unsafe { &*(address as *const libc::sockaddr_in) };
    // `s_addr` est en ordre réseau ; `Ipv4Addr::from(u32)` attend l'ordre hôte.
    let ip = std::net::Ipv4Addr::from(u32::from_be(sin.sin_addr.s_addr));
    // SÛRETÉ : voir `on_browse`.
    let found = unsafe { &mut *(context as *mut Found) };
    found.addr.get_or_insert_with(|| ip.to_string());
}

// -------------------------------------------------------------------- boucle

fn browse(service: &str, context: *mut Seen) -> Option<Query> {
    let regtype = CString::new(service).ok()?;
    let mut sd: DnsServiceRef = std::ptr::null_mut();
    let err = unsafe {
        DNSServiceBrowse(
            &mut sd,
            0,
            IFACE_ANY,
            regtype.as_ptr(),
            std::ptr::null(), // domaine par défaut : local.
            on_browse,
            context as *mut c_void,
        )
    };
    if err != 0 {
        // `_rym_rrc._tcp` porte un underscore au milieu du nom, ce que le démon
        // peut refuser là où le MFD l'annonce sans façon : on continue avec
        // l'autre service plutôt que d'abandonner la recherche.
        eprintln!("dns-sd: recherche de {service} refusée (erreur {err})");
        return None;
    }
    (!sd.is_null()).then_some(Query(sd))
}

fn resolve(instance: &Instance, context: *mut Hosts) -> Option<Query> {
    let mut sd: DnsServiceRef = std::ptr::null_mut();
    let err = unsafe {
        DNSServiceResolve(
            &mut sd,
            0,
            instance.interface,
            instance.name.as_ptr(),
            instance.regtype.as_ptr(),
            instance.domain.as_ptr(),
            on_resolve,
            context as *mut c_void,
        )
    };
    (err == 0 && !sd.is_null()).then_some(Query(sd))
}

fn addr_of(host: &CString, interface: u32, context: *mut Found) -> Option<Query> {
    let mut sd: DnsServiceRef = std::ptr::null_mut();
    let err = unsafe {
        DNSServiceGetAddrInfo(
            &mut sd,
            0,
            interface,
            PROTOCOL_IPV4,
            host.as_ptr(),
            on_addr,
            context as *mut c_void,
        )
    };
    (err == 0 && !sd.is_null()).then_some(Query(sd))
}

/// Attend qu'une requête ait de quoi lire, puis la laisse appeler son callback.
fn pump(queries: &[Query], budget: Duration) {
    let mut fds: Vec<libc::pollfd> = queries
        .iter()
        .map(|q| libc::pollfd {
            fd: q.fd(),
            events: libc::POLLIN,
            revents: 0,
        })
        .collect();
    let ms = budget.as_millis().min(c_int::MAX as u128) as c_int;
    let ready = unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as libc::nfds_t, ms) };
    if ready <= 0 {
        return; // délai écoulé, ou interruption : l'appelant décidera
    }
    for (pfd, query) in fds.iter().zip(queries) {
        if pfd.revents & libc::POLLIN != 0 {
            query.process();
        }
    }
}

/// Première adresse IPv4 annoncée, ou None si rien n'est vu dans le délai.
pub fn discover(budget: Duration) -> Option<String> {
    let deadline = Instant::now() + budget;
    // Contextes confiés au démon : pointeurs bruts et rien d'autre, pour qu'il
    // n'existe jamais deux voies d'accès au même état pendant un callback.
    let seen = Box::into_raw(Box::new(Seen::default()));
    let hosts = Box::into_raw(Box::new(Hosts::default()));
    let found = Box::into_raw(Box::new(Found::default()));

    let mut queries: Vec<Query> = SERVICES.iter().filter_map(|s| browse(s, seen)).collect();
    let mut addr = None;

    while addr.is_none() && !queries.is_empty() {
        let Some(left) = deadline.checked_duration_since(Instant::now()) else {
            break;
        };
        pump(&queries, left);
        // Chaque réponse fait naître la requête du temps suivant, dont le
        // descripteur rejoint le `poll` du tour d'après : instance → hôte,
        // hôte → adresse.
        let instances = std::mem::take(unsafe { &mut (*seen).instances });
        queries.extend(instances.iter().filter_map(|i| resolve(i, hosts)));
        let resolved = std::mem::take(unsafe { &mut (*hosts).hosts });
        queries.extend(
            resolved
                .iter()
                .filter_map(|(host, iface)| addr_of(host, *iface, found)),
        );
        addr = unsafe { (*found).addr.take() };
    }

    drop(queries); // avant les contextes : plus personne ne doit y toucher
    unsafe {
        drop(Box::from_raw(seen));
        drop(Box::from_raw(hosts));
        drop(Box::from_raw(found));
    }
    addr
}
