#!/usr/bin/env bash
#
# Environnement Android — JDK, SDK, NDK posés par `just setup-android` (voir
# README.md § Android). `just` exécute les recettes par /bin/sh, qui ne lit
# pas ~/.bashrc : les variables posées là restent donc invisibles des
# recettes, d'où ce fichier, à sourcer plutôt que dupliquer la découverte
# dans chaque recette (`android-init`, `android`, `android-build`,
# `emulator`).
#
#     source android-env.sh
#
# Erreur explicite si l'un des trois manque, plutôt que le cryptique
# « emulator: not found » une fois la commande réelle lancée.

jdk=$(find "$HOME/android-toolchain" -maxdepth 1 -type d -name 'jdk-17*' 2>/dev/null | sort -V | tail -1)
if [ -z "$jdk" ]; then
    echo "JDK 17 introuvable sous ~/android-toolchain — voir README.md § Android" >&2
    exit 1
fi
export JAVA_HOME="$jdk"

export ANDROID_HOME="$HOME/Android/sdk"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
if [ ! -d "$ANDROID_HOME" ]; then
    echo "SDK Android introuvable à $ANDROID_HOME — voir README.md § Android" >&2
    exit 1
fi

ndk=$(ls -d "$ANDROID_HOME"/ndk/*/ 2>/dev/null | sort -V | tail -1)
if [ -z "$ndk" ]; then
    echo "NDK introuvable sous $ANDROID_HOME/ndk — voir README.md § Android" >&2
    exit 1
fi
export ANDROID_NDK_HOME="${ndk%/}"

export PATH="$JAVA_HOME/bin:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH"
