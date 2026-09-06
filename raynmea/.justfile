# raynmea — recettes de développement (https://just.systems).
#
#     just              la liste des recettes
#     just build        le binaire ./raynmea
#     just test         les tests du paquet
#     just install      le binaire dans ~/.local/bin
#     just app          raynmea.app, la passerelle dans la barre de menus
#
# Le paquet et le binaire portent le même nom : `go build` dépose donc
# `raynmea` *dans* raynmea/ — d'où la ligne qui l'exclut du dépôt (.gitignore
# à la racine), et le `-o` explicite ici.
#
# `just` ne garde que la *dernière* ligne de commentaire comme description :
# d'où les descriptions d'une ligne, et le détail dans les en-têtes de section.

bin := "raynmea"

# Répertoire d'installation de `just install` (`just prefix=/usr/local/bin install`).
prefix := home_directory() / ".local/bin"

# L'app macOS de la barre de menus (cmd/raynmea-menu).
app := "raynmea.app"
app_id := "local.raynmea.menu"
app_version := "1.0"

# Identité de signature. « - » est la signature ad hoc : elle suffit à faire
# tourner l'app en local, mais macOS considère alors chaque recompilation comme
# une app *nouvelle* et redemande l'autorisation « réseau local ». Avec une vraie
# identité (`security find-identity -v -p codesigning`), l'autorisation tient :
#
#     RAYNMEA_CODESIGN_ID="Apple Development: …" just app
#
# L'horodatage sécurisé suit l'identité : impossible en ad hoc, et *exigé* par la
# notarisation — d'où le choix automatique ci-dessous.
codesign_id := env("RAYNMEA_CODESIGN_ID", "-")

# Cible du Raspberry Pi du bord, pour la recette `cross`.
cross_goos := "linux"
cross_goarch := "arm64"

# Liste les recettes.
default:
    @just --list --unsorted

# ------------------------------------------------------------ compilation ---

# Compile ./raynmea.
build:
    go build -o {{ bin }} .

# Compile pour le Raspberry Pi du bord (linux/arm64) : raynmea-linux-arm64.
cross:
    GOOS={{ cross_goos }} GOARCH={{ cross_goarch }} go build \
        -o {{ bin }}-{{ cross_goos }}-{{ cross_goarch }} .

# ------------------------------------------------------------ app macOS -----
#
# `raynmea-menu` est un binaire à part : il passe par cgo et AppKit (menuet),
# que le binaire `raynmea` — et donc `just cross` — ne voit jamais. menuet exige
# un bundle : hors `.app`, il meurt sur une exception ObjC.

# Construit et signe raynmea.app (barre de menus).
app:
    #!/usr/bin/env bash
    set -euo pipefail
    rm -rf {{ app }}
    mkdir -p {{ app }}/Contents/MacOS {{ app }}/Contents/Resources
    go build -o {{ app }}/Contents/MacOS/raynmea-menu ./cmd/raynmea-menu
    # menubar.pdf est vectoriel : macOS le tinte (template) et le rend net à
    # toutes les échelles. raynmea.icns est l'icône du Finder et des Réglages.
    cp cmd/raynmea-menu/icons/menubar.pdf cmd/raynmea-menu/icons/raynmea.icns \
        {{ app }}/Contents/Resources/
    cat > {{ app }}/Contents/Info.plist <<PLIST
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
      <key>CFBundleName</key><string>raynmea</string>
      <key>CFBundleDisplayName</key><string>raynmea</string>
      <key>CFBundleExecutable</key><string>raynmea-menu</string>
      <key>CFBundleIdentifier</key><string>{{ app_id }}</string>
      <key>CFBundleVersion</key><string>{{ app_version }}</string>
      <key>CFBundleShortVersionString</key><string>{{ app_version }}</string>
      <key>CFBundlePackageType</key><string>APPL</string>
      <key>CFBundleIconFile</key><string>raynmea</string>
      <key>LSMinimumSystemVersion</key><string>14.0</string>
      <key>LSUIElement</key><true/>
      <key>NSLocalNetworkUsageDescription</key>
      <string>raynmea cherche le MFD Raymarine sur le réseau du bord (mDNS) et lui diffuse les phrases NMEA en UDP.</string>
    </dict>
    </plist>
    PLIST
    # Le heredoc ci-dessus est indenté comme la recette : just retire cette
    # indentation du corps, pas de son contenu — d'où le sed.
    sed -i '' 's/^    //' {{ app }}/Contents/Info.plist
    stamp=--timestamp
    [ "{{ codesign_id }}" != "-" ] || stamp=--timestamp=none
    codesign --force --sign "{{ codesign_id }}" --options runtime "$stamp" {{ app }}
    echo "construit : $PWD/{{ app }} (signé « {{ codesign_id }} »)"

# Construit l'app et la lance (elle remplace celle qui tourne déjà).
app-run: app
    #!/usr/bin/env bash
    set -euo pipefail
    pkill -f "{{ app }}/Contents/MacOS/raynmea-menu" || true
    open {{ app }}

# Suit le journal de l'app.
app-log:
    tail -f ~/Library/Logs/raynmea/suivi.log

# Installe l'app dans ~/Applications.
app-install: app
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p ~/Applications
    rm -rf ~/Applications/{{ app }}
    cp -R {{ app }} ~/Applications/
    echo "installée : ~/Applications/{{ app }}"

# Regénère les icônes depuis les SVG (rsvg-convert, iconutil).
icons:
    #!/usr/bin/env bash
    set -euo pipefail
    cd {{ justfile_directory() }}/cmd/raynmea-menu/icons
    # 24 px de haut = 18 pt : la taille d'un glyphe dans une barre de 22 pt.
    rsvg-convert -h 24 -f pdf menubar.svg -o menubar.pdf
    rm -rf raynmea.iconset && mkdir raynmea.iconset
    for s in 16 32 128 256 512; do
        rsvg-convert -w $s -h $s -f png app.svg -o raynmea.iconset/icon_${s}x${s}.png
        rsvg-convert -w $((s * 2)) -h $((s * 2)) -f png app.svg \
            -o raynmea.iconset/icon_${s}x${s}@2x.png
    done
    iconutil -c icns raynmea.iconset -o raynmea.icns
    rm -rf raynmea.iconset
    echo "icônes refaites : menubar.pdf, raynmea.icns"

# Oublie les options de l'app (NSUserDefaults) — l'app doit être arrêtée.
app-reset:
    defaults delete {{ app_id }} || true

# --------------------------------------------------------------- installation --

# Installe le binaire dans ~/.local/bin.
install:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p "{{ prefix }}"
    # GOBIN détourne go install vers le répertoire voulu ; le reste du cache
    # de compilation est partagé avec `just build`.
    GOBIN="{{ prefix }}" go install .
    echo "installé : {{ prefix }}/{{ bin }}"

# Retire le binaire installé par `just install`.
uninstall:
    rm -f {{ prefix }}/{{ bin }}

# Efface les binaires produits, l'app, et le cache de test.
clean:
    rm -rf {{ bin }} {{ bin }}-{{ cross_goos }}-{{ cross_goarch }} {{ app }}
    go clean -testcache

# ---------------------------------------------------------------- exécution --
#
# `run` passe ses arguments tels quels : `just run -tui`, `just run 192.168.42.1`,
# `just run -no-udp -nmea -`. Sans argument, c'est le défaut du programme —
# découverte mDNS et diffusion UDP vers 127.0.0.1:10110.

# Lance depuis les sources (`just run -tui`, `just run 192.168.42.1`, …).
run *args:
    go run . {{ args }}

# L'écran de veille : SOG, COG, GPS, profondeur, TWS, TWA.
tui:
    go run . -tui

# ------------------------------------------------------------------ tests ----

# Les tests du paquet : trames, décodage, phrases NMEA, adoption d'une annonce.
test:
    go test ./...

# Idem, en détaillant chaque test (et sans le cache).
test-v:
    go test -v -count=1 ./...

# Les tests sous le détecteur de courses (le fil d'événements est partagé).
race:
    go test -race -count=1 ./...

# Couverture, puis le rapport annoté dans le navigateur.
cover:
    go test -coverprofile=/tmp/{{ bin }}-cover.out ./...
    go tool cover -html=/tmp/{{ bin }}-cover.out

# ------------------------------------------------------------------ qualité --

# Met en forme les sources (gofmt).
fmt:
    gofmt -w *.go

# Le vet du toolchain.
vet:
    go vet ./...

# staticcheck, récupéré à la volée (rien à installer d'avance).
lint:
    go run honnef.co/go/tools/cmd/staticcheck@latest ./...

# Mise en forme vérifiée (sans réécrire), vet, puis les tests.
check:
    #!/usr/bin/env bash
    set -euo pipefail
    mal=$(gofmt -l *.go)
    if [ -n "$mal" ]; then
        echo "gofmt à passer sur : $mal" >&2
        exit 1
    fi
    go vet ./...
    go test ./...

# Met à jour go.mod/go.sum (dépendances rangées, sommes complètes).
tidy:
    go mod tidy

# ------------------------------------------------------ essai sans MFD -------
#
# `mfd` lance le simulateur du dépôt, mDNS comprise, dans ce terminal ; `just
# tui` dans un autre trouve alors le MFD comme sur le bateau. Le port annoncé
# est forcé à 23333 par MFD_RAYDB_MDNS_PORT : raynmea l'ignore de toute façon
# (le MFD réel annonce 49111 et écoute sur 23333), mais le simulateur devient
# ainsi conforme au chemin nominal.

# Lance un MFD simulé sur ce Mac : RayDB et mDNS, sans vidéo, SSH ni télécommande.
mfd:
    cd {{ justfile_directory() }}/../mfdsim && MFD_RAYDB_MDNS_PORT=23333 \
        uv run run.py --no-rtsp --no-ssh --no-8182 --no-control

# Montre les phrases diffusées en UDP (Ctrl-C pour arrêter).
listen port="10110":
    socat -u UDP4-RECV:{{ port }},reuseaddr -

# Montre les services RayDB annoncés sur le réseau (Ctrl-C pour arrêter).
discover:
    dns-sd -B _raydb._tcp
