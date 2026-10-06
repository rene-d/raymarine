// raynmea-menu — la passerelle raynmea dans la barre de menus de macOS.
//
// Le moteur est celui du programme en ligne de commande (`internal/gateway`) :
// cette commande n'ajoute qu'un affichage et des options. Elle est **à part**
// exprès — menuet passe par cgo et par AppKit, et rien de tout cela ne doit
// entrer dans le binaire `raynmea`, qui se cross-compile pour le Raspberry Pi
// du bord.
//
// Ce qu'elle montre : la vitesse fond dans la barre, le bateau, le MFD et les
// valeurs qu'on regarde en naviguant dans le menu. Ce qu'elle règle : la
// diffusion UDP et ses destinations, le MFD (mDNS ou IP imposée), et
// l'enregistrement d'une séance.
//
// Deux écritures dans ~/Library/Logs/raynmea : le suivi (`suivi.log`), toujours,
// plafonné et roulé — c'est la boîte noire de l'app ; et l'enregistrement, armé
// depuis le menu, un fichier par séance, où le suivi, les valeurs reçues et les
// phrases émises se suivent dans l'ordre.
//
// Les options vivent dans les NSUserDefaults de l'app : elles survivent au
// redémarrage, et le programme en ligne de commande les ignore complètement.
//
// Les options, la supervision du moteur et sa relance à chaque réglage sont
// celles de `internal/desktop`, partagées avec l'app Windows (main_windows.go) :
// ce fichier n'apporte que la barre de menus.
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/caseymrm/menuet/v2"

	"github.com/rene-d/raymarine/raynmea/internal/desktop"
	"github.com/rene-d/raymarine/raynmea/internal/gateway"
)

const (
	appName = "raynmea"
	// Doit rester l'identifiant du bundle : menuet s'en sert pour les
	// NSUserDefaults (où vivent les options) et pour l'ouverture à la session.
	appLabel = "local.raynmea.menu"

	// La clé des options dans les NSUserDefaults.
	optionsKey = "options"

	// Rythme de rafraîchissement de la barre. Une seconde suffit à un cadran de
	// navigation, et ne fait pas danser la largeur du titre.
	refresh = time.Second
)

// ------------------------------------------------------------- les options ---

// defaults garde les options (desktop.Options) dans les NSUserDefaults.
type defaults struct{}

func (defaults) Load(o *desktop.Options) error {
	return menuet.Defaults().Unmarshal(optionsKey, o)
}

func (defaults) Save(o desktop.Options) error {
	return menuet.Defaults().Marshal(optionsKey, o)
}

// ------------------------------------------------------------------ l'app ----

type app struct {
	eng *desktop.Engine

	mu          sync.Mutex
	fingerprint string // ce que le menu affiche déjà, pour ne le rafraîchir qu'utilement
}

func main() {
	// menuet ne sait vivre que dans un bundle (il parle aux notifications, qui
	// exigent une identité) : hors bundle, il meurt sur une exception ObjC
	// illisible. Autant le dire soi-même.
	if exe, err := os.Executable(); err == nil &&
		!strings.Contains(exe, ".app/Contents/MacOS/") {
		fmt.Fprintln(os.Stderr,
			"raynmea-menu doit tourner depuis raynmea.app — voir `just app`.")
		os.Exit(2)
	}

	a := &app{eng: desktop.NewEngine(logDir(), defaults{})}

	// Le contexte et le groupe d'attente de menuet : le moteur s'arrête avec
	// l'app, et ses sorties se referment avant qu'elle ne rende la main.
	wg, ctx := menuet.App().GracefulShutdownHandles()
	wg.Add(3)
	go func() { defer wg.Done(); a.eng.Supervise(ctx) }()
	go func() { defer wg.Done(); a.refreshLoop(ctx) }()
	go func() { defer wg.Done(); a.hangup(ctx) }()

	menuet.App().Name = appName
	menuet.App().Label = appLabel
	menuet.App().Children = a.menu
	menuet.App().RunApplication()
}

// logDir rend ~/Library/Logs/raynmea, créé au besoin. Si le répertoire ne se
// crée pas, on se rabat sur le répertoire temporaire : l'app doit démarrer.
func logDir() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return os.TempDir()
	}
	dir := filepath.Join(home, "Library", "Logs", appName)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return os.TempDir()
	}
	return dir
}

// hangup relance le moteur sur SIGHUP, avec les options en cours. Un menu ne se
// pilote pas depuis un terminal : c'est par là qu'on éprouve la relance, et
// qu'on force une reconnexion sans quitter l'app.
//
// Il ne *relit* pas les options : menuet mémorise en Go ce qu'il a écrit dans
// les NSUserDefaults (`UserDefaults.String` rend la valeur du cache dès que la
// clé a été écrite une fois), si bien qu'un `defaults write` extérieur reste
// invisible jusqu'au prochain lancement. Pour régler l'app depuis un terminal :
// la quitter, écrire, la relancer.
func (a *app) hangup(ctx context.Context) {
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGHUP)
	defer signal.Stop(sig)
	for {
		select {
		case <-ctx.Done():
			return
		case <-sig:
			a.apply(func(*desktop.Options) {})
		}
	}
}

// ------------------------------------------------------------- le moteur -----

// apply retient les options, les enregistre, relance le moteur, et redessine le
// menu.
func (a *app) apply(change func(*desktop.Options)) {
	a.eng.Apply(change)
	menuet.App().MenuChanged()
}

func (a *app) options() desktop.Options { return a.eng.Options() }

func (a *app) snapshot() gateway.Snapshot { return a.eng.Snapshot() }

// ------------------------------------------------------------ la barre -------

// refreshLoop tient le titre à jour, et ne redessine le menu ouvert que lorsque
// ce qu'il montre a vraiment changé — sans quoi la souris glisserait sur un
// menu reconstruit à chaque seconde.
func (a *app) refreshLoop(ctx context.Context) {
	tick := time.NewTicker(refresh)
	defer tick.Stop()
	for {
		s := a.snapshot()
		menuet.App().SetMenuState(&menuet.MenuState{
			Image: "menubar", Runs: titleRuns(s)})
		fp := fmt.Sprintf("%s|%s|%v|%s|%d|%s|%s|%s|%s|%s|%s|%s|%s", s.Status,
			s.IP, s.Connected, s.Boat, s.Updates,
			s.SOG, s.COG, s.Position, s.Depth, s.TWS, s.TWA, s.AWS, s.AWA)
		a.mu.Lock()
		changed := fp != a.fingerprint
		a.fingerprint = fp
		a.mu.Unlock()
		if changed {
			menuet.App().MenuChanged()
		}
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
	}
}

// titleRuns écrit la vitesse fond à droite de l'icône : le nombre en chasse
// fixe, pour que le titre ne danse pas d'un dixième à l'autre, et l'unité en
// petit. Le voilier, lui, est l'image du status item (menubar.pdf), que macOS
// tinte selon le thème.
func titleRuns(s gateway.Snapshot) []menuet.TextRun {
	color := menuet.LabelPrimary
	if !s.SOG.Known || s.SOG.Stale {
		color = menuet.LabelTertiary
	}
	return []menuet.TextRun{
		{Text: " " + s.SOG.Text, Monospaced: true, Color: color},
		{Text: " " + s.SOG.Unit, FontSize: 10, Color: color},
	}
}

// ------------------------------------------------------------- le menu -------

func (a *app) menu() []menuet.MenuItem {
	s := a.snapshot()
	o := a.options()

	items := []menuet.MenuItem{
		header(s),
		menuet.Separator{},
	}
	items = append(items,
		reading("SOG ", s.SOG), reading("COG ", s.COG),
		reading("GPS ", s.Position), reading("FOND", s.Depth),
		reading2("TWS/TWA ", s.TWS, s.TWA),
		reading2("AWS/AWA ", s.AWS, s.AWA),
		menuet.Separator{},
	)

	items = append(items,
		menuet.Regular{Text: "Diffusion UDP", State: o.UDP, Clicked: func() {
			a.apply(func(o *desktop.Options) { o.UDP = !o.UDP })
		}},
		menuet.Regular{Text: "Destinations", Children: a.destinations},
		menuet.Regular{Text: "MFD", Children: a.mfd},
		menuet.Separator{},
		a.recordItem(o),
		menuet.Regular{Text: "Limiter l'enregistrement à 8 Mo", State: o.RecordCap,
			Subtitle: []menuet.TextRun{{Text: "au-delà, le fichier bascule en « .1 » et repart"}},
			Clicked:  func() { a.apply(func(o *desktop.Options) { o.RecordCap = !o.RecordCap }) }},
	)
	return items
}

// header : à qui l'on parle. Le nom du bateau vient du MFD (il peut manquer :
// réglage jamais souscrit, ou MFD qui ne le sert pas), l'adresse vient de la
// liaison. Le texte de la dernière note ne tient plus cette ligne — une erreur
// UDP passagère y chassait l'essentiel ; elle reste dans `suivi.log`.
func header(s gateway.Snapshot) menuet.MenuItem {
	color := menuet.LabelPrimary
	var text string
	switch {
	case s.Connected && s.Boat != "":
		text = s.Boat + " — " + s.IP
	case s.Connected:
		text = "MFD " + s.IP
	case s.IP != "":
		text = s.IP + " — déconnecté"
		color = menuet.LabelTertiary
	default:
		text = "recherche du MFD…"
		color = menuet.LabelTertiary
	}
	return menuet.Regular{
		Runs: []menuet.TextRun{{Text: text, FontWeight: menuet.WeightSemibold,
			Color: color}},
		Subtitle: []menuet.TextRun{{Text: fmt.Sprintf("%d updates · %d phrases",
			s.Updates, s.Sentences)}},
	}
}

// reading écrit une valeur en chasse fixe, grisée quand plus rien ne la
// rafraîchit — la même convention que la TUI.
func reading(label string, r gateway.Reading) menuet.MenuItem {
	color := menuet.LabelPrimary
	if !r.Known || r.Stale {
		color = menuet.LabelTertiary
	}
	text := fmt.Sprintf("%s  %s", label, r)
	if r.Unit != "" {
		text = fmt.Sprintf("%s  %8s %s", label, r.Text, r.Unit)
	}
	return menuet.Regular{Runs: []menuet.TextRun{
		{Text: text, Monospaced: true, Color: color}}}
}

// reading écrit une valeur en chasse fixe, grisée quand plus rien ne la
// rafraîchit — la même convention que la TUI.
func reading2(label string, r1 gateway.Reading, r2 gateway.Reading) menuet.MenuItem {
	color := menuet.LabelPrimary
	if !r1.Known || r1.Stale || !r2.Known || r2.Stale {
		color = menuet.LabelTertiary
	}
	var text string

	if r1.Unit != "" {
		text = fmt.Sprintf("%s  %8s %s", label, r1.Text, r1.Unit)
	} else {
		text = fmt.Sprintf("%s  %s", label, r1)
	}

	if r2.Unit != "" {
		text = fmt.Sprintf("%s / %8s %s", text, r2.Text, r2.Unit)
	} else {
		text = fmt.Sprintf("%s / %s", text, r2)
	}

	return menuet.Regular{Runs: []menuet.TextRun{
		{Text: text, Monospaced: true, Color: color}}}
}

// recordItem arme ou désarme l'enregistrement (cf. desktop.Options.ToggleRecord).
func (a *app) recordItem(o desktop.Options) menuet.MenuItem {
	sub := "suivi, valeurs reçues et phrases NMEA, dans un fichier"
	if o.Record {
		sub = o.RecordFile
		if st, err := os.Stat(filepath.Join(a.eng.LogDir(), o.RecordFile)); err == nil {
			sub += " · " + desktop.Size(st.Size())
		}
	}
	return menuet.Regular{
		Text:     "Enregistrer le journal",
		State:    o.Record,
		Subtitle: []menuet.TextRun{{Text: sub}},
		Clicked: func() {
			a.apply(func(o *desktop.Options) { o.ToggleRecord(time.Now()) })
		},
	}
}

// destinations : la liste des destinations UDP. Un clic en retire une — c'est
// la seule action qu'une ligne puisse porter, et « Ajouter… » fait le reste.
func (a *app) destinations() []menuet.MenuItem {
	o := a.options()
	var items []menuet.MenuItem
	for _, d := range o.Dests {
		dest := d
		items = append(items, menuet.Regular{
			Text:     gateway.DestLabel(dest),
			State:    true,
			Subtitle: []menuet.TextRun{{Text: "cliquer pour retirer"}},
			Clicked: func() {
				a.apply(func(o *desktop.Options) { o.Dests = desktop.Without(o.Dests, dest) })
			},
		})
	}
	if len(items) == 0 {
		items = append(items, menuet.Regular{Runs: []menuet.TextRun{
			{Text: "aucune destination", Color: menuet.LabelTertiary}}})
	}
	items = append(items, menuet.Separator{},
		menuet.Regular{Text: "Ajouter une destination…", Clicked: func() {
			go a.askDest()
		}},
		menuet.Regular{Text: "Diffuser en broadcast (255.255.255.255)", Clicked: func() {
			a.apply(func(o *desktop.Options) { o.Dests = desktop.With(o.Dests, "255.255.255.255") })
		}},
	)
	return items
}

func (a *app) askDest() {
	r := menuet.App().Alert(menuet.Alert{
		MessageText:     "Nouvelle destination",
		InformativeText: fmt.Sprintf("Hôte, ou hôte:port (%d par défaut).", gateway.UDPPort),
		Buttons:         []string{"Ajouter", "Annuler"},
		Inputs:          []menuet.AlertInput{{Placeholder: "192.168.1.42"}},
	})
	if r.Button != 0 || len(r.Inputs) == 0 {
		return
	}
	dest := strings.TrimSpace(r.Inputs[0])
	if dest == "" {
		return
	}
	a.apply(func(o *desktop.Options) { o.Dests = desktop.With(o.Dests, dest) })
}

// mfd : découverte mDNS, ou l'adresse qu'on impose.
func (a *app) mfd() []menuet.MenuItem {
	o := a.options()
	items := []menuet.MenuItem{
		menuet.Regular{Text: "Découverte mDNS", State: o.IP == "", Clicked: func() {
			a.apply(func(o *desktop.Options) { o.IP = "" })
		}},
	}
	if o.IP != "" {
		items = append(items, menuet.Regular{Text: "IP imposée : " + o.IP, State: true})
	}
	items = append(items, menuet.Separator{},
		menuet.Regular{Text: "Imposer une IP…", Clicked: func() { go a.askIP() }})
	return items
}

func (a *app) askIP() {
	o := a.options()
	r := menuet.App().Alert(menuet.Alert{
		MessageText: "Adresse du MFD",
		InformativeText: "L'IP du MFD, si la découverte mDNS ne passe pas. " +
			"Laisser vide pour revenir à la découverte.",
		Buttons: []string{"Utiliser", "Annuler"},
		Inputs:  []menuet.AlertInput{{Placeholder: "192.168.42.1", Value: o.IP}},
	})
	if r.Button != 0 || len(r.Inputs) == 0 {
		return
	}
	ip := strings.TrimSpace(r.Inputs[0])
	a.apply(func(o *desktop.Options) { o.IP = ip })
}
