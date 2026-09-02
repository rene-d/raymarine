//! `WifiManager.MulticastLock`, pris par JNI directe le temps d'une recherche
//! mDNS — sans lui, la plupart des téléphones filtrent les trames multicast
//! (224.0.0.251:5353) dès que l'écran s'éteint, pour économiser la radio
//! Wi-Fi. `mdns-sd` ouvrirait bien sa socket, mais ne recevrait jamais rien.
//!
//! Pas de greffon Tauri ici, juste des appels JNI à la main : même choix que
//! `discovery/apple.rs` côté Bonjour, et pour la même raison — l'API visée
//! (`android.net.wifi.WifiManager`) n'a pas de crate Rust dédiée qui vaille
//! une dépendance de plus, ni les allers-retours JS d'un greffon.
//!
//! `ndk_context` — le moyen habituel de retrouver le contexte Android depuis
//! Rust — ne marche pas ici : le greffon Android de Tauri 2 (une activité
//! Kotlin qui charge cette bibliothèque par `System.loadLibrary`) n'appelle
//! jamais `ndk_context::initialize_android_context`, ni `wry` ni `tao` non
//! plus. On capte donc le `JavaVM` nous-mêmes dans `JNI_OnLoad`, que le
//! système appelle une fois au chargement de la bibliothèque — avant toute
//! commande Tauri.

use std::ffi::c_void;
use std::sync::{Mutex, OnceLock};

use jni::objects::{GlobalRef, JObject, JValue};
use jni::{JNIEnv, JavaVM};

static JVM: OnceLock<JavaVM> = OnceLock::new();

/// Point d'entrée JNI, appelé par le système au chargement de la bibliothèque
/// (`System.loadLibrary`). Le nom et la signature sont imposés par la JVM —
/// c'est elle qui le cherche par ce symbole, rien ne l'appelle depuis Rust.
#[no_mangle]
pub extern "system" fn JNI_OnLoad(vm: *mut jni::sys::JavaVM, _reserved: *mut c_void) -> jni::sys::jint {
    if let Ok(vm) = unsafe { JavaVM::from_raw(vm) } {
        let _ = JVM.set(vm);
    }
    jni::sys::JNI_VERSION_1_6
}

fn vm() -> Result<&'static JavaVM, String> {
    JVM.get().ok_or_else(|| "JavaVM Android indisponible (JNI_OnLoad non exécuté)".to_string())
}

/// Le contexte `Application`, via `ActivityThread.currentApplication()` —
/// pas besoin de capter l'`Activity`, de toute façon indisponible depuis
/// `JNI_OnLoad`.
fn app_context<'local>(env: &mut JNIEnv<'local>) -> Result<JObject<'local>, String> {
    let app = env
        .call_static_method(
            "android/app/ActivityThread",
            "currentApplication",
            "()Landroid/app/Application;",
            &[],
        )
        .map_err(|e| format!("currentApplication : {e}"))?
        .l()
        .map_err(|e| format!("objet Application : {e}"))?;
    if app.is_null() {
        return Err("currentApplication() a rendu null".into());
    }
    Ok(app)
}

static LOCK: Mutex<Option<GlobalRef>> = Mutex::new(None);

/// Poignée RAII sur le verrou : `acquire()` le prend, `Drop` le relâche.
/// Ainsi aucune sortie anticipée de `discover` (`ServiceDaemon::new` en échec,
/// budget épuisé) ne peut oublier de le relâcher.
pub struct MulticastGuard(());

impl MulticastGuard {
    /// Best-effort : un échec n'empêche pas la recherche mDNS de tenter sa
    /// chance — elle marchera simplement moins bien sur les téléphones qui
    /// filtrent le multicast à l'écran éteint.
    pub fn acquire() -> Self {
        if let Err(e) = try_acquire() {
            eprintln!("MulticastLock non pris : {e}");
        }
        MulticastGuard(())
    }
}

impl Drop for MulticastGuard {
    fn drop(&mut self) {
        if let Err(e) = try_release() {
            eprintln!("MulticastLock non relâché : {e}");
        }
    }
}

fn try_acquire() -> Result<(), String> {
    if LOCK.lock().unwrap().is_some() {
        return Ok(()); // déjà pris — une seule recherche à la fois
    }
    let vm = vm()?;
    let mut env = vm.attach_current_thread().map_err(|e| format!("attach : {e}"))?;
    let context = app_context(&mut env)?;
    let result = acquire_inner(&mut env, &context);
    let _ = env.exception_clear();
    result.map_err(|e| format!("MulticastLock : {e}"))
}

fn acquire_inner(env: &mut JNIEnv, context: &JObject) -> Result<(), jni::errors::Error> {
    // wifi = context.getSystemService(Context.WIFI_SERVICE)
    let svc = env.new_string("wifi")?;
    let wifi = env
        .call_method(
            context,
            "getSystemService",
            "(Ljava/lang/String;)Ljava/lang/Object;",
            &[JValue::Object(&svc)],
        )?
        .l()?;

    // lock = wifi.createMulticastLock("mfdview-mdns")
    let tag = env.new_string("mfdview-mdns")?;
    let lock = env
        .call_method(
            &wifi,
            "createMulticastLock",
            "(Ljava/lang/String;)Landroid/net/wifi/WifiManager$MulticastLock;",
            &[JValue::Object(&tag)],
        )?
        .l()?;

    // setReferenceCounted(false) : un seul release() suffit à le lever, quel
    // que soit le nombre de recherches passées par acquire() depuis.
    env.call_method(&lock, "setReferenceCounted", "(Z)V", &[JValue::Bool(0)])?;
    env.call_method(&lock, "acquire", "()V", &[])?;

    // Référence globale : la locale ci-dessus ne survivrait pas à ce retour
    // de fonction, ni aux allers-retours attach/detach du fil suivant.
    *LOCK.lock().unwrap() = Some(env.new_global_ref(lock)?);
    Ok(())
}

fn try_release() -> Result<(), String> {
    let Some(lock) = LOCK.lock().unwrap().take() else {
        return Ok(()); // jamais pris (échec d'acquire) — rien à faire
    };
    let vm = vm()?;
    let mut env = vm.attach_current_thread().map_err(|e| format!("attach : {e}"))?;
    let result = env.call_method(&lock, "release", "()V", &[]);
    let _ = env.exception_clear();
    drop(lock); // libère la référence globale JNI
    result.map(|_| ()).map_err(|e| format!("release : {e}"))
}
