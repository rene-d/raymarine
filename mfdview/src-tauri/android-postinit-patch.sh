#!/usr/bin/env bash
#
# Ajoute au AndroidManifest.xml généré les autorisations que MFDView réclame :
# INTERNET (le TCP vers le MFD), ACCESS_WIFI_STATE et
# CHANGE_WIFI_MULTICAST_STATE (le MulticastLock de
# `src/discovery/mdns_sd/android.rs`). `gen/android` est hors dépôt et
# régénéré à chaque `cargo tauri android init` — comme `gen/apple` côté iOS —
# donc ce script s'exécute après coup, plutôt qu'une modification à la main
# qui se perdrait à la prochaine régénération. Idempotent : sans effet si les
# autorisations sont déjà là.
#
# Appelé par `just android-init`.

set -euo pipefail

manifest="$(cd "$(dirname "$0")" && pwd)/gen/android/app/src/main/AndroidManifest.xml"

if [ ! -f "$manifest" ]; then
    echo "AndroidManifest.xml introuvable à $manifest — lancer 'cargo tauri android init' d'abord" >&2
    exit 1
fi

python3 - "$manifest" <<'PY'
import re
import sys

path = sys.argv[1]
src = open(path, encoding="utf-8").read()

perms = [
    "android.permission.INTERNET",
    "android.permission.ACCESS_WIFI_STATE",
    "android.permission.CHANGE_WIFI_MULTICAST_STATE",
]
missing = [p for p in perms if p not in src]
if not missing:
    print("permissions déjà présentes — rien à faire")
    sys.exit(0)

block = "\n    " + "\n    ".join(f'<uses-permission android:name="{p}" />' for p in missing)
new = re.sub(r"(<manifest\b[^>]*>)", r"\1" + block, src, count=1)
if new == src:
    sys.exit("balise <manifest> introuvable dans " + path)

open(path, "w", encoding="utf-8").write(new)
print("ajoutées :", ", ".join(missing))
PY
