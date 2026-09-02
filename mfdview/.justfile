# MFDView — recettes de développement (https://just.systems).
#
#     just              la liste des recettes
#     just ios          compile et lance sur le simulateur iPhone
#
# Tout se passe dans `src-tauri`, où vivent Cargo.toml et tauri.conf.json ; les
# recettes s'y placent d'office. Le MFD simulé, lui, est dans `../mfdsim`.
#
# `just` ne garde que la *dernière* ligne de commentaire comme description :
# d'où les descriptions d'une ligne, et le détail dans les en-têtes de section.

set working-directory := 'src-tauri'

# Simulateur visé par défaut — `just ios "iPhone 17 Pro"` pour un autre.
default_sim := "iPhone 17"

# Identifiant de l'app, tel que déclaré dans tauri.conf.json.
bundle := "com.mfdview.instruments"

# Liste les recettes.
default:
    @just --list --unsorted

# ---------------------------------------------------------------- bureau ----

# Lance l'app sur ce Mac. `just run 127.0.0.1` force l'adresse du MFD.
run ip="":
    MFD_IP={{ ip }} cargo run

# Idem, carte comprise — la caractéristique `map` compile SQLite (une minute).
run-map ip="":
    MFD_IP={{ ip }} cargo run --features map

# Compile en release et empaquette (target/release/bundle), sans la carte.
build:
    cargo tauri build

# Idem, carte comprise. Le jeu de tuiles n'est pas empaqueté pour autant : il
# reste cherché à l'exécution (voir « La carte » dans README.md).
build-map:
    cargo tauri build --features map

# ------------------------------------------------------------------ iOS ----
#
# `ios-build` produit une archive signée : poser l'équipe de développement dans
# l'environnement, `TAURI_APPLE_DEVELOPMENT_TEAM=XXXXXXXXXX just ios-build`. Un
# compte gratuit installe sur son propre iPhone, pour sept jours ; TestFlight
# demande un compte payant.

# (Re)génère le projet Xcode dans gen/apple — hors dépôt, régénérable.
ios-init: && icons
    cargo tauri ios init

# Compile et lance sur le simulateur, journaux de l'app compris.
ios sim=default_sim:
    cargo tauri ios dev "{{ sim }}"

# Compile et lance sur un iPhone relié en USB (demande une signature).
iphone:
    cargo tauri ios dev --host

# Archive signée, pour TestFlight ou une installation ad hoc.
ios-build:
    cargo tauri ios build

# Installe dans le simulateur une version autonome, sans serveur de dev.
ios-install:
    # Le .app de la fois d'avant fait échouer le renommage final de la CLI
    # (« Directory not empty ») : on retire la cible d'abord.
    rm -rf gen/apple/build/arm64-sim/MFDView.app
    cargo tauri ios build --debug --target aarch64-sim
    xcrun simctl install booted gen/apple/build/arm64-sim/MFDView.app

# Régénère les icônes de toutes les plateformes depuis icons/icon.png.
icons:
    #!/usr/bin/env bash
    set -euo pipefail
    # La CLI réécrit sa propre source au passage — un ré-encodage sans
    # conséquence à l'œil, mais jamais deux fois le même : sans cette mise de
    # côté, chaque exécution laisserait une modification fantôme dans le dépôt.
    source=$(mktemp) && cp icons/icon.png "$source"
    # `--ios-color` bouche les coins transparents de l'icône : iOS pose son
    # propre masque arrondi, et une icône déjà arrondie ressortirait rognée deux
    # fois. #111110 est le fond de la page, le raccord ne se voit pas.
    cargo tauri icon icons/icon.png --ios-color '#111110'
    # Puis on retire le canal alpha, qu'Apple refuse sur une icône (voir le
    # script). À refaire après chaque `ios-init`, qui repart des icônes de Tauri.
    # Sans objet si gen/apple n'existe pas (Android seul, sur une machine sans
    # Quartz/PyObjC — Linux, typiquement) : android-init peut alors régénérer
    # les icônes sans passer par un Mac.
    if [ -d gen/apple/Assets.xcassets/AppIcon.appiconset ]; then
        python3 icons/no-alpha.py gen/apple/Assets.xcassets/AppIcon.appiconset/*.png
    fi
    mv "$source" icons/icon.png

# -------------------------------------------------------------- Android ----
#
# Contrairement à iOS, tout s'installe aussi sous Linux : pas besoin d'un Mac
# pour ce palier. Il faut un SDK Android (`ANDROID_HOME`), un NDK dedans, un
# JDK 17 (`JAVA_HOME`), et `cargo-tauri` — voir `just setup-android` pour les
# cibles Rust. Détails dans README.md § Android.
#
# Chaque recette source `android-env.sh` plutôt que de compter sur le profil
# shell : `just` exécute les recettes par /bin/sh, qui ne lit pas ~/.bashrc,
# donc des variables posées là-bas resteraient invisibles ici.

# (Re)génère le projet Gradle dans gen/android — hors dépôt, régénérable.
android-init: && icons
    #!/usr/bin/env bash
    set -euo pipefail
    source ./android-env.sh
    cargo tauri android init
    ./android-postinit-patch.sh

# Lance l'émulateur "mfdview" (Pixel 6, Android 14, x86_64) — dans un autre
# terminal, avant `just android`. Accélération KVM si /dev/kvm est accessible.
emulator:
    #!/usr/bin/env bash
    set -euo pipefail
    source ./android-env.sh
    emulator -avd mfdview

# Compile et lance sur un appareil ou un émulateur branché (`just emulator`,
# ou un téléphone en USB avec le débogage activé).
android:
    #!/usr/bin/env bash
    set -euo pipefail
    source ./android-env.sh
    cargo tauri android dev

# APK de debug, pour un essai hors développement.
android-build:
    #!/usr/bin/env bash
    set -euo pipefail
    source ./android-env.sh
    cargo tauri android build --apk --debug

# Les cibles Rust pour Android — le SDK, le NDK et un JDK 17 restent à poser
# à la main (Android Studio, ou sdkmanager en ligne de commande).
setup-android:
    rustup target add aarch64-linux-android armv7-linux-androideabi i686-linux-android x86_64-linux-android

# ------------------------------------------------- simulateur, inspection ---
#
# **`relaunch` suppose `just ios` en marche.** La version de développement
# n'embarque pas la page : elle la charge depuis le serveur que tient
# `cargo tauri ios dev` sur le port 1430. Relancée seule, elle affiche
# « Failed to request http://127.0.0.1:1430/ ». Pour une app qui tient debout
# toute seule — page embarquée, comme sur un vrai iPhone —, `just ios-install`.
#
# `relaunch` rejoue le démarrage sans recompiler : c'est le moment où la page
# réclame l'état par « ready », donc là où se voient les ratés de rattrapage.
# `discover` interroge le démon Bonjour du système, la source même de
# `discovery/apple.rs` — ce qu'il montre est ce que l'app verra.

# Capture l'écran du simulateur (hors du dépôt, par défaut).
shot file="/tmp/mfdview.png":
    xcrun simctl io booted screenshot --type=png "{{ file }}"

# Suit les journaux de l'app dans le simulateur (ce qu'écrit `status()`).
logs:
    xcrun simctl spawn booted log stream --style compact --predicate 'process == "MFDView"'

# Relance l'app dans le simulateur, sans recompiler.
relaunch:
    #!/usr/bin/env bash
    set -euo pipefail
    app=$(xcrun simctl get_app_container booted {{ bundle }} app)
    # Une version de développement porte l'adresse du serveur de la CLI en dur.
    # La relancer sans ce serveur donne un écran d'erreur et rien d'autre :
    # autant le dire ici plutôt que de laisser chercher.
    if strings "$app/MFDView" | grep -q '127.0.0.1:1430' && ! nc -z 127.0.0.1 1430 2>/dev/null; then
        echo "L'app installée est une version de développement : elle charge sa page" >&2
        echo "depuis http://127.0.0.1:1430, où rien n'écoute." >&2
        echo "  just ios          relance le serveur, et l'app avec" >&2
        echo "  just ios-install  installe une version autonome, qui s'en passe" >&2
        exit 1
    fi
    xcrun simctl terminate booted {{ bundle }} 2>/dev/null || true
    xcrun simctl launch booted {{ bundle }}

# Lance un MFD simulé sur ce Mac : RayDB et mDNS, sans vidéo ni SSH.
mfd:
    cd {{ justfile_directory() }}/../mfdsim && uv run run.py --no-rtsp --no-ssh --no-8182

# Montre les services Raymarine annoncés sur le réseau (Ctrl-C pour arrêter).
discover:
    dns-sd -B _raydb._tcp

# --------------------------------------------------------------- entretien --
#
# `check` ne vérifie que la bibliothèque sur les cibles iOS : le binaire n'y
# sert pas, c'est le projet Xcode qui lie `mfdview_lib` et appelle `run()`.
#
# La carte est hors du build par défaut ; sans la passe `--features map`, son
# code ne serait plus compilé par personne et pourrirait sans qu'on le voie.

# Vérifie sans produire de binaire, sur les trois cibles Apple, carte comprise.
check:
    cargo check
    cargo check --features map
    cargo check --lib --target aarch64-apple-ios
    cargo check --lib --target aarch64-apple-ios-sim

# Clippy, sans indulgence — la carte aussi.
lint:
    cargo clippy --all-targets -- -D warnings
    cargo clippy --all-targets --features map -- -D warnings

# Passe rustfmt. Séparé de `lint` à dessein : le code est mis en forme à la
# main ici, et rustfmt défait quelques alignements voulus (`raydb.rs`).
fmt:
    cargo fmt

# L'outillage iOS : cibles Rust, CLI Tauri, CocoaPods.
setup:
    rustup target add aarch64-apple-ios aarch64-apple-ios-sim
    cargo install tauri-cli --locked --version "^2"
    command -v pod >/dev/null || brew install cocoapods

# Efface les artefacts (target/ passe le gigaoctet).
clean:
    cargo clean
    rm -rf gen/apple/build
