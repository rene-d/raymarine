// raynmea-menu — la passerelle raynmea dans la zone de notification de Windows.
//
// C'est la jumelle de l'app macOS (main_darwin.go) : même moteur
// (`internal/gateway`), mêmes options et même supervision (`internal/desktop`),
// seul l'affichage change. Il passe par Win32, au travers de
// `github.com/tailscale/walk` — sans cgo : le `.exe` se compile depuis le Mac
// (`just win`), et rien de tout cela n'entre dans le binaire `raynmea`.
//
// La zone de notification n'offre qu'une icône de 16 px, sans texte à côté :
// la vitesse fond ne peut pas s'y afficher comme dans la barre de menus. D'où
// la répartition suivante :
//
//   - l'icône dit si la liaison tient (grisée sinon), son infobulle donne le
//     bateau, le MFD et la vitesse ;
//   - un clic gauche ouvre le tableau de bord, une petite fenêtre qui montre
//     les valeurs en gros, et qu'on peut garder au premier plan ;
//   - un clic droit ouvre le menu : les mêmes valeurs et les mêmes réglages que
//     sur macOS, plus le dossier des journaux et le démarrage avec Windows.
//
// Les options vivent dans %APPDATA%\raynmea\options.json, le suivi et les
// enregistrements dans %LOCALAPPDATA%\raynmea\Logs.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
	"unicode/utf16"

	"github.com/tailscale/walk"
	decl "github.com/tailscale/walk/declarative"
	"github.com/tailscale/win"
	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"

	"github.com/rene-d/raymarine/raynmea/internal/desktop"
	"github.com/rene-d/raymarine/raynmea/internal/gateway"
)

const (
	appName = "raynmea"

	// Rythme de rafraîchissement de l'infobulle et du tableau de bord.
	refresh = time.Second

	// La clé où Windows cherche les programmes à lancer à l'ouverture de
	// session, pour l'utilisateur seul.
	runKey = `Software\Microsoft\Windows\CurrentVersion\Run`

	// Le mutex qui garantit une seule instance par session : deux passerelles
	// diffuseraient chaque phrase en double. macOS l'assure de lui-même pour un
	// bundle, Windows non.
	mutexName = `Local\raynmea-menu`
)

// Couleurs des valeurs du tableau de bord : fraîches, ou grisées quand plus
// rien ne les rafraîchit — la convention de la TUI et du menu macOS.
var (
	colorFresh = walk.RGB(0x1f, 0x1f, 0x1f)
	colorStale = walk.RGB(0x9a, 0x9a, 0x9a)

	// L'avertissement d'un repli : visible sans crier.
	colorWarning = walk.RGB(0xb3, 0x5c, 0x00)
)

// ------------------------------------------------------------- les options ---

// settings est le contenu de options.json : les options communes aux deux apps,
// et ce que seule l'app Windows règle.
type settings struct {
	Options desktop.Options `json:"options"`
	OnTop   bool            `json:"on_top"` // tableau de bord au premier plan
}

// fileStore garde les réglages dans un fichier JSON. C'est le desktop.Store de
// l'app ; il garde aussi ce qui ne regarde que Windows (OnTop).
type fileStore struct {
	path string
	mu   sync.Mutex
	s    settings
}

func (f *fileStore) Load(o *desktop.Options) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	data, err := os.ReadFile(f.path)
	if errors.Is(err, os.ErrNotExist) {
		return nil // jamais écrites : desktop.LoadOptions prendra les défauts
	}
	if err != nil {
		return err
	}
	if err := json.Unmarshal(data, &f.s); err != nil {
		return err
	}
	*o = f.s.Options
	return nil
}

func (f *fileStore) Save(o desktop.Options) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.s.Options = o
	return f.write()
}

func (f *fileStore) onTop() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.s.OnTop
}

func (f *fileStore) setOnTop(v bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.s.OnTop = v
	_ = f.write()
}

// write écrit le fichier à côté puis le renomme : une coupure au mauvais moment
// laisse l'ancien fichier intact plutôt qu'un fichier tronqué. os.Rename
// remplace la cible sous Windows (MoveFileEx, MOVEFILE_REPLACE_EXISTING).
func (f *fileStore) write() error {
	data, err := json.MarshalIndent(f.s, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(f.path), 0o755); err != nil {
		return err
	}
	tmp := f.path + ".tmp"
	if err := os.WriteFile(tmp, append(data, '\n'), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, f.path)
}

// optionsPath rend %APPDATA%\raynmea\options.json : les réglages suivent
// l'utilisateur (profil itinérant), les journaux non.
func optionsPath() string {
	dir, err := os.UserConfigDir()
	if err != nil {
		dir = os.TempDir()
	}
	return filepath.Join(dir, appName, "options.json")
}

// logDir rend %LOCALAPPDATA%\raynmea\Logs, créé au besoin. Si le répertoire ne
// se crée pas, on se rabat sur le répertoire temporaire : l'app doit démarrer.
func logDir() string {
	base, err := os.UserCacheDir() // %LOCALAPPDATA% sous Windows
	if err != nil {
		return os.TempDir()
	}
	dir := filepath.Join(base, appName, "Logs")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return os.TempDir()
	}
	return dir
}

// ------------------------------------------------------------------ l'app ----

type app struct {
	eng   *desktop.Engine
	store *fileStore

	ni      *walk.NotifyIcon
	iconOn  *walk.Icon // liaison établie
	iconOff *walk.Icon // on cherche, ou la liaison est perdue
	menus   []*walk.Menu
	dash    *dashboard // créé au premier clic

	// Ce que l'icône montre déjà : on ne touche à la zone de notification que
	// lorsque cela change. Lus et écrits sur le fil de l'interface seulement.
	shownOn  bool
	shownTip string
}

func main() {
	if _, err := walk.InitApp(); err != nil {
		os.Exit(1) // sans walk, pas même de quoi afficher l'erreur
	}

	// Le handle reste ouvert jusqu'à la fin du processus : c'est lui qui tient
	// le verrou. Une seconde instance trouve le nom déjà pris, et s'efface.
	if _, err := windows.CreateMutex(nil, false,
		windows.StringToUTF16Ptr(mutexName)); errors.Is(err, windows.ERROR_ALREADY_EXISTS) {
		walk.MsgBox(nil, appName, "raynmea tourne déjà : son icône est dans la "+
			"zone de notification (au besoin, derrière la flèche ^).",
			walk.MsgBoxIconInformation)
		return
	}

	store := &fileStore{path: optionsPath()}
	a := &app{eng: desktop.NewEngine(logDir(), store), store: store}
	if err := a.initIcon(); err != nil {
		walk.MsgBox(nil, appName, "Impossible de créer l'icône de notification : "+
			err.Error(), walk.MsgBoxIconError)
		os.Exit(1)
	}
	defer a.ni.Dispose()

	// Les fils lancés par walk.App().Go sont attendus à la sortie de Run : le
	// moteur referme ses journaux avant que le processus ne rende la main.
	walk.App().Go(a.eng.Supervise)
	walk.App().Go(a.refreshLoop)
	code := walk.App().Run()
	a.ni.Dispose() // os.Exit court-circuite les defer
	os.Exit(code)
}

// initIcon pose l'icône dans la zone de notification. Les deux icônes, « APP »
// et « OFF », sont des ressources du .exe (cf. winres/), en plusieurs tailles :
// Windows prend celle qui convient à l'échelle de l'écran.
func (a *app) initIcon() error {
	var err error
	if a.iconOn, err = walk.NewIconFromResource("APP"); err != nil {
		return err
	}
	if a.iconOff, err = walk.NewIconFromResource("OFF"); err != nil {
		return err
	}
	if a.ni, err = walk.NewNotifyIcon(); err != nil {
		return err
	}
	_ = a.ni.SetIcon(a.iconOff)
	a.shownTip = appName + "\nrecherche du MFD…"
	_ = a.ni.SetToolTip(a.shownTip)

	// Clic gauche (ou Entrée au clavier) : le tableau de bord. Le clic droit
	// ouvre le menu, reconstruit juste avant de s'afficher.
	a.ni.MouseUp().Attach(func(_, _ int, button walk.MouseButton) {
		if button == walk.LeftButton {
			a.showDashboard()
		}
	})
	a.ni.ShowingContextMenu().Attach(func() bool {
		a.buildMenu()
		return true
	})
	return a.ni.SetVisible(true)
}

// ------------------------------------------------------------ l'icône --------

// refreshLoop tient l'icône, l'infobulle et le tableau de bord à jour. Le
// dessin se fait sur le fil de l'interface (Synchronize), l'instantané ici.
func (a *app) refreshLoop(ctx context.Context) {
	tick := time.NewTicker(refresh)
	defer tick.Stop()
	for {
		s := a.eng.Snapshot()
		walk.App().Synchronize(func() { a.render(s) })
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
	}
}

func (a *app) render(s gateway.Snapshot) {
	if s.Connected != a.shownOn {
		icon := a.iconOff
		if s.Connected {
			icon = a.iconOn
		}
		if a.ni.SetIcon(icon) == nil {
			a.shownOn = s.Connected
		}
	}
	if tip := toolTip(s); tip != a.shownTip {
		if a.ni.SetToolTip(tip) == nil {
			a.shownTip = tip
		}
	}
	if a.dash != nil && a.dash.mw.Visible() {
		a.dash.render(s, a.options())
	}
}

// toolTip : le nom de l'app, à qui l'on parle, et la vitesse fond — ce que la
// barre de menus montre sans clic sur macOS. Un repli de la découverte s'y
// ajoute : il se voit ainsi sans ouvrir le menu, et sans fenêtre qui
// s'imposerait.
func toolTip(s gateway.Snapshot) string {
	tip := appName + " — " + headerText(s)
	if s.Connected {
		tip += "\nSOG " + s.SOG.String() + " · COG " + s.COG.String()
	}
	if s.Discovery.Fallback != "" {
		tip += "\n⚠ mDNS de Windows en échec (repli)"
	}
	return truncUTF16(tip, 127)
}

// fallbackShort annonce le repli de la découverte (cf. gateway.Discovery) dans
// le menu et le tableau de bord, qui y ajoutent l'erreur de l'API. L'infobulle,
// limitée à 127 caractères, en a sa version courte.
const fallbackShort = "mDNS de Windows en échec : repli sur le client intégré"

// truncUTF16 coupe un texte à n unités UTF-16 : l'infobulle d'une icône de
// notification tient en 128 caractères larges, zéro final compris, et walk
// refuse ce qui dépasse.
func truncUTF16(text string, n int) string {
	if len(utf16.Encode([]rune(text))) <= n {
		return text
	}
	var b strings.Builder
	units := 0
	for _, r := range text {
		w := utf16.RuneLen(r)
		if units+w > n-1 {
			break
		}
		b.WriteRune(r)
		units += w
	}
	return b.String() + "…"
}

// headerText : à qui l'on parle. Le nom du bateau vient du MFD (il peut
// manquer : réglage jamais souscrit, ou MFD qui ne le sert pas), l'adresse
// vient de la liaison.
func headerText(s gateway.Snapshot) string {
	switch {
	case s.Connected && s.Boat != "":
		return s.Boat + " — " + s.IP
	case s.Connected:
		return "MFD " + s.IP
	case s.IP != "":
		return s.IP + " — déconnecté"
	}
	return "recherche du MFD…"
}

func countersText(s gateway.Snapshot) string {
	return fmt.Sprintf("%d updates · %d phrases", s.Updates, s.Sentences)
}

// ------------------------------------------------------------- le menu -------

func (a *app) options() desktop.Options { return a.eng.Options() }

// apply retient les options, les enregistre et relance le moteur. Le menu,
// lui, se reconstruit à chaque ouverture : rien à redessiner.
func (a *app) apply(change func(*desktop.Options)) { a.eng.Apply(change) }

// buildMenu refait le menu contextuel à partir de l'état du moment. Win32 le
// dessine à l'ouverture et le fige : contrairement à macOS, il n'y a pas de
// menu ouvert à tenir à jour.
func (a *app) buildMenu() {
	actions := a.ni.ContextMenu().Actions()
	_ = actions.Clear()
	for _, m := range a.menus {
		m.Dispose()
	}
	a.menus = nil

	s := a.eng.Snapshot()
	o := a.options()

	head := item(headerText(s), nil)
	_ = head.SetEnabled(s.Connected)
	add(actions, head)
	add(actions, disabled(countersText(s)))
	if s.Discovery.Fallback != "" {
		add(actions, item("⚠ "+fallbackShort, nil))
	}
	add(actions, walk.NewSeparatorAction())

	add(actions, reading("SOG", s.SOG.String(), fresh(s.SOG)))
	add(actions, reading("COG", s.COG.String(), fresh(s.COG)))
	add(actions, reading("GPS", s.Position.String(), fresh(s.Position)))
	add(actions, reading("FOND", s.Depth.String(), fresh(s.Depth)))
	add(actions, reading("TWS/TWA", s.TWS.String()+" / "+s.TWA.String(),
		fresh(s.TWS) && fresh(s.TWA)))
	add(actions, reading("AWS/AWA", s.AWS.String()+" / "+s.AWA.String(),
		fresh(s.AWS) && fresh(s.AWA)))
	add(actions, walk.NewSeparatorAction())

	show := item("Tableau de bord", a.showDashboard)
	_ = show.SetDefault(true) // en gras : c'est aussi ce que fait le clic gauche
	add(actions, show)
	add(actions, walk.NewSeparatorAction())

	add(actions, check("Diffusion UDP", o.UDP, func() {
		a.apply(func(o *desktop.Options) { o.UDP = !o.UDP })
	}))
	a.submenu(actions, "Destinations", a.destinations(o))
	a.submenu(actions, "MFD", a.mfd(o, s.Discovery))
	add(actions, walk.NewSeparatorAction())

	add(actions, a.recordItem(o))
	add(actions, check("Limiter l'enregistrement à 8 Mo", o.RecordCap, func() {
		a.apply(func(o *desktop.Options) { o.RecordCap = !o.RecordCap })
	}))
	add(actions, item("Ouvrir le dossier des journaux", func() {
		// explorer.exe rend 1 même quand tout va bien : on ne l'attend pas.
		_ = exec.Command("explorer.exe", a.eng.LogDir()).Start()
	}))
	add(actions, walk.NewSeparatorAction())

	add(actions, check("Démarrer avec Windows", startAtLogin(), func() {
		if err := setStartAtLogin(!startAtLogin()); err != nil {
			walk.MsgBox(nil, appName, "Démarrage avec Windows : "+err.Error(),
				walk.MsgBoxIconError)
		}
	}))
	add(actions, item("Quitter", func() { walk.App().Exit(0) }))
}

// recordItem arme ou désarme l'enregistrement (cf. desktop.Options.ToggleRecord).
// Armé, il montre le fichier de la séance et sa taille.
func (a *app) recordItem(o desktop.Options) *walk.Action {
	text := "Enregistrer le journal"
	if o.Record {
		sub := o.RecordFile
		if st, err := os.Stat(filepath.Join(a.eng.LogDir(), o.RecordFile)); err == nil {
			sub += " · " + desktop.Size(st.Size())
		}
		text += "\t" + sub
	}
	return check(text, o.Record, func() {
		a.apply(func(o *desktop.Options) { o.ToggleRecord(time.Now()) })
	})
}

// destinations : la liste des destinations UDP. Un clic en retire une — c'est
// la seule action qu'une ligne puisse porter, et « Ajouter… » fait le reste.
func (a *app) destinations(o desktop.Options) []*walk.Action {
	var items []*walk.Action
	for _, d := range o.Dests {
		dest := d
		items = append(items, check(gateway.DestLabel(dest)+"\tcliquer pour retirer",
			true, func() {
				a.apply(func(o *desktop.Options) { o.Dests = desktop.Without(o.Dests, dest) })
			}))
	}
	if len(items) == 0 {
		items = append(items, disabled("aucune destination"))
	}
	return append(items, walk.NewSeparatorAction(),
		item("Ajouter une destination…", later(a.askDest)),
		item("Diffuser en broadcast (255.255.255.255)", func() {
			a.apply(func(o *desktop.Options) { o.Dests = desktop.With(o.Dests, "255.255.255.255") })
		}),
	)
}

// mfd : découverte mDNS, ou l'adresse qu'on impose. Pendant la découverte, une
// ligne dit par où elle passe — et, en cas de repli, l'erreur de l'API.
func (a *app) mfd(o desktop.Options, d gateway.Discovery) []*walk.Action {
	items := []*walk.Action{
		check("Découverte mDNS", o.IP == "", func() {
			a.apply(func(o *desktop.Options) { o.IP = "" })
		}),
	}
	if o.IP == "" {
		switch {
		case d.Fallback != "":
			items = append(items,
				disabled("    par le client intégré (repli)"),
				disabled("    "+d.Fallback))
		case d.Native:
			items = append(items, disabled("    par le service mDNS de Windows"))
		}
	}
	if o.IP != "" {
		items = append(items, check("IP imposée : "+o.IP, true, nil))
	}
	return append(items, walk.NewSeparatorAction(),
		item("Imposer une IP…", later(a.askIP)))
}

func (a *app) askDest() {
	dest, ok := askText("Nouvelle destination",
		fmt.Sprintf("Hôte, ou hôte:port (%d par défaut).", gateway.UDPPort),
		"192.168.1.42", "", "Ajouter")
	if !ok || dest == "" {
		return
	}
	a.apply(func(o *desktop.Options) { o.Dests = desktop.With(o.Dests, dest) })
}

func (a *app) askIP() {
	ip, ok := askText("Adresse du MFD",
		"L'IP du MFD, si la découverte mDNS ne passe pas. "+
			"Laisser vide pour revenir à la découverte.",
		"192.168.42.1", a.options().IP, "Utiliser")
	if !ok {
		return
	}
	a.apply(func(o *desktop.Options) { o.IP = ip })
}

// submenu ajoute un sous-menu ; il sera libéré à la reconstruction suivante.
func (a *app) submenu(actions *walk.ActionList, text string, items []*walk.Action) {
	m, err := walk.NewMenu()
	if err != nil {
		return
	}
	for _, it := range items {
		add(m.Actions(), it)
	}
	a.menus = append(a.menus, m)
	if act, err := actions.AddMenu(m); err == nil {
		_ = act.SetText(text)
	}
}

// ------------------------------------------------- éléments de menu ----------

// menuText protège les « & » d'un texte venu d'ailleurs (nom du bateau) :
// Win32 les prend pour l'annonce d'un raccourci clavier.
func menuText(text string) string { return strings.ReplaceAll(text, "&", "&&") }

// item : une ligne de menu, cliquable si handler n'est pas nil.
func item(text string, handler func()) *walk.Action {
	act := walk.NewAction()
	_ = act.SetText(menuText(text))
	if handler != nil {
		act.Triggered().Attach(handler)
	}
	return act
}

func disabled(text string) *walk.Action {
	act := item(text, nil)
	_ = act.SetEnabled(false)
	return act
}

func check(text string, checked bool, handler func()) *walk.Action {
	act := item(text, handler)
	_ = act.SetCheckable(true)
	_ = act.SetChecked(checked)
	return act
}

// reading écrit une valeur, alignée à droite par la tabulation (la colonne des
// raccourcis d'un menu Win32), grisée quand plus rien ne la rafraîchit.
func reading(label, value string, isFresh bool) *walk.Action {
	act := item(label+"\t"+value, nil)
	_ = act.SetEnabled(isFresh)
	return act
}

func fresh(r gateway.Reading) bool { return r.Known && !r.Stale }

func add(actions *walk.ActionList, act *walk.Action) { _ = actions.Add(act) }

// later remet une action à la boucle de messages : un dialogue modal ne s'ouvre
// pas de l'intérieur du menu qui vient de se refermer.
func later(f func()) func() {
	return func() { walk.App().Synchronize(f) }
}

// ------------------------------------------------------- les dialogues -------

// askText demande une ligne de texte. Le second résultat dit si l'on a validé.
func askText(title, prompt, placeholder, value, okText string) (string, bool) {
	var (
		dlg      *walk.Dialog
		edit     *walk.LineEdit
		ok, quit *walk.PushButton
	)
	err := decl.Dialog{
		AssignTo:      &dlg,
		Title:         title,
		DefaultButton: &ok,
		CancelButton:  &quit,
		MinSize:       decl.Size{Width: 380},
		Layout:        decl.VBox{},
		Children: []decl.Widget{
			decl.Label{Text: prompt},
			decl.LineEdit{AssignTo: &edit, Text: value, CueBanner: placeholder},
			decl.Composite{
				Layout: decl.HBox{MarginsZero: true},
				Children: []decl.Widget{
					decl.HSpacer{},
					decl.PushButton{AssignTo: &ok, Text: okText,
						OnClicked: func() { dlg.Accept() }},
					decl.PushButton{AssignTo: &quit, Text: "Annuler",
						OnClicked: func() { dlg.Cancel() }},
				},
			},
		},
	}.Create(nil)
	if err != nil {
		return "", false
	}
	// Sans fenêtre propriétaire, le dialogue pourrait s'ouvrir derrière celle
	// qui a le focus : on le ramène devant.
	dlg.Starting().Attach(func() { win.SetForegroundWindow(dlg.Handle()) })
	if dlg.Run() != walk.DlgCmdOK {
		return "", false
	}
	return strings.TrimSpace(edit.Text()), true
}

// ---------------------------------------------------- démarrer avec Windows --

// startAtLogin dit si la clé Run lance *ce* .exe. Une entrée qui pointe
// ailleurs (l'app a été déplacée) compte comme absente : la cocher la refait.
func startAtLogin() bool {
	k, err := registry.OpenKey(registry.CURRENT_USER, runKey, registry.QUERY_VALUE)
	if err != nil {
		return false
	}
	defer k.Close()
	v, _, err := k.GetStringValue(appName)
	if err != nil {
		return false
	}
	exe, err := os.Executable()
	return err == nil && strings.EqualFold(v, `"`+exe+`"`)
}

func setStartAtLogin(on bool) error {
	k, _, err := registry.CreateKey(registry.CURRENT_USER, runKey, registry.SET_VALUE)
	if err != nil {
		return err
	}
	defer k.Close()
	if !on {
		if err := k.DeleteValue(appName); err != nil && !errors.Is(err, registry.ErrNotExist) {
			return err
		}
		return nil
	}
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	return k.SetStringValue(appName, `"`+exe+`"`)
}

// ------------------------------------------------------ le tableau de bord ---

// dashboard est la fenêtre du clic gauche : les valeurs qu'on regarde en
// naviguant, en gros et en chasse fixe. La fermer la cache seulement.
type dashboard struct {
	mw       *walk.MainWindow
	header   *walk.Label
	counters *walk.Label
	values   [6]*walk.Label // SOG, COG, GPS, FOND, TWS/TWA, AWS/AWA
	warning  *walk.Label    // repli de la découverte ; vide sinon
	status   *walk.Label
	onTop    *walk.CheckBox
}

var dashLabels = [6]string{"SOG", "COG", "GPS", "FOND", "TWS/TWA", "AWS/AWA"}

func (a *app) showDashboard() {
	if a.dash == nil {
		d, err := a.newDashboard()
		if err != nil {
			walk.MsgBox(nil, appName, "Tableau de bord : "+err.Error(),
				walk.MsgBoxIconError)
			return
		}
		a.dash = d
		// Sans taille donnée, Windows choisit la sienne (CW_USEDEFAULT), bien
		// trop grande, et walk ne l'ajuste pas : la fenêtre étant fixe, on la
		// ramène une fois pour toutes au minimum de sa disposition, après un
		// premier rendu pour que les labels aient leur texte. SetBoundsPixels
		// relève toute taille à ce minimum, d'où la demande à 0×0.
		d.render(a.eng.Snapshot(), a.options())
		b := d.mw.BoundsPixels()
		_ = d.mw.SetBoundsPixels(walk.Rectangle{X: b.X, Y: b.Y})
	}
	a.dash.render(a.eng.Snapshot(), a.options())
	a.dash.mw.Show()
	win.SetForegroundWindow(a.dash.mw.Handle())
}

func (a *app) newDashboard() (*dashboard, error) {
	d := &dashboard{}
	nameFont := decl.Font{Family: "Segoe UI", PointSize: 10}
	valueFont := decl.Font{Family: "Consolas", PointSize: 16}

	var rows []decl.Widget
	for i, name := range dashLabels {
		rows = append(rows,
			decl.Label{Text: name, Font: nameFont, TextColor: colorStale},
			decl.Label{AssignTo: &d.values[i], Text: "—", Font: valueFont,
				TextColor: colorStale},
		)
	}

	err := decl.MainWindow{
		AssignTo:        &d.mw,
		Title:           appName,
		Icon:            a.iconOn,
		MinSize:         decl.Size{Width: 480},
		DisableMaximize: true,
		DisableResizing: true,
		Layout:          decl.VBox{},
		Children: []decl.Widget{
			decl.Label{AssignTo: &d.header, Font: decl.Font{Family: "Segoe UI",
				PointSize: 12, SemiBold: true}},
			decl.Label{AssignTo: &d.counters, Font: nameFont, TextColor: colorStale},
			decl.Composite{
				Layout:   decl.Grid{Columns: 2, MarginsZero: true},
				Children: rows,
			},
			// Toujours là, vide quand tout va bien : la fenêtre est de taille
			// fixe, un label qui apparaîtrait n'y trouverait pas sa place.
			// L'erreur entière est dans le sous-menu MFD.
			decl.Label{AssignTo: &d.warning, Font: nameFont, TextColor: colorWarning,
				EllipsisMode: decl.EllipsisEnd},
			decl.Label{AssignTo: &d.status, Font: nameFont, TextColor: colorStale,
				EllipsisMode: decl.EllipsisEnd},
			decl.CheckBox{AssignTo: &d.onTop, Text: "Toujours au premier plan",
				Checked: a.store.onTop(),
				OnCheckedChanged: func() {
					a.store.setOnTop(d.onTop.Checked())
					d.applyOnTop()
				}},
		},
	}.Create()
	if err != nil {
		return nil, err
	}

	// La fenêtre n'est pas l'app : la fermer la cache, et c'est « Quitter »,
	// dans le menu, qui arrête tout.
	d.mw.SetExitOnClose(false)
	d.mw.Closing().Attach(func(canceled *bool, _ walk.CloseReason) {
		*canceled = true
		d.mw.Hide()
	})
	d.applyOnTop()
	return d, nil
}

// applyOnTop pose la fenêtre au premier plan, ou l'en retire, selon la case.
func (d *dashboard) applyOnTop() {
	after := win.HWND_NOTOPMOST
	if d.onTop.Checked() {
		after = win.HWND_TOPMOST
	}
	win.SetWindowPos(d.mw.Handle(), after, 0, 0, 0, 0,
		win.SWP_NOMOVE|win.SWP_NOSIZE|win.SWP_NOACTIVATE)
}

func (d *dashboard) render(s gateway.Snapshot, o desktop.Options) {
	setLabel(d.header, headerText(s), s.Connected)
	_ = d.counters.SetText(countersText(s))

	vals := [6]struct {
		text  string
		fresh bool
	}{
		{s.SOG.String(), fresh(s.SOG)},
		{s.COG.String(), fresh(s.COG)},
		{s.Position.String(), fresh(s.Position)},
		{s.Depth.String(), fresh(s.Depth)},
		{s.TWS.String() + " / " + s.TWA.String(), fresh(s.TWS) && fresh(s.TWA)},
		{s.AWS.String() + " / " + s.AWA.String(), fresh(s.AWS) && fresh(s.AWA)},
	}
	for i, v := range vals {
		setLabel(d.values[i], v.text, v.fresh)
	}

	// Le repli de la découverte reste affiché tant qu'il dure : sa note, elle,
	// est vite chassée de la ligne d'état par les suivantes.
	warn := ""
	if s.Discovery.Fallback != "" {
		warn = "⚠ " + fallbackShort + " — " + s.Discovery.Fallback
	}
	if d.warning.Text() != warn {
		_ = d.warning.SetText(warn)
	}

	// La dernière note du suivi : une erreur (destination illisible, journal
	// impossible) s'y lit sans aller ouvrir suivi.log.
	status := s.Status
	if !o.UDP {
		status = "diffusion UDP coupée · " + status
	}
	_ = d.status.SetText(status)
}

// setLabel n'écrit que ce qui change : un SetText à chaque seconde ferait
// clignoter la fenêtre.
func setLabel(l *walk.Label, text string, isFresh bool) {
	if l.Text() != text {
		_ = l.SetText(text)
	}
	color := colorStale
	if isFresh {
		color = colorFresh
	}
	if l.TextColor() != color {
		l.SetTextColor(color)
	}
}
