// Package desktop — ce que les deux apps de bureau partagent : la barre de
// menus de macOS et la zone de notification de Windows (`cmd/raynmea-menu`).
//
// Ni l'une ni l'autre n'ajoute rien au moteur (`internal/gateway`) : elles le
// présentent et le règlent. Ce paquet tient la partie qui ne dépend pas de
// l'affichage — les options et leur traduction en `gateway.Config`, et la
// supervision qui refait une session à chaque réglage. Chaque app n'apporte que
// son dessin et l'endroit où elle garde ses options (`Store`).
//
// Une option qui change relance le moteur — la reconnexion RayDB est immédiate,
// et c'est plus sûr que de reconfigurer une session en cours.
package desktop

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/rene-d/raymarine/raynmea/internal/gateway"
)

// MaxLogBytes : le journal d'une app qui tourne des semaines tient en deux
// fichiers de 8 Mo au plus (cf. gateway.Config.MaxLogBytes). C'est aussi le
// plafond proposé à l'enregistrement, quand on le laisse plafonné.
const MaxLogBytes = 8 << 20

// ------------------------------------------------------------- les options ---

// Options est ce que le menu règle, et ce que l'app garde d'un lancement à
// l'autre. `Version` marque simplement qu'elles ont déjà été écrites : sans
// elle, on ne distinguerait pas « diffusion coupée » de « jamais configuré ».
type Options struct {
	Version int      `json:"version"`
	IP      string   `json:"ip"` // vide : découverte mDNS
	Dests   []string `json:"dests"`
	UDP     bool     `json:"udp"`

	// L'enregistrement : un fichier par séance, nommé quand on l'arme, et
	// gardé dans les options — un autre réglage relance le moteur, et
	// l'enregistrement doit reprendre dans le *même* fichier.
	Record     bool   `json:"record"`
	RecordFile string `json:"record_file"`
	RecordCap  bool   `json:"record_cap"` // plafonner à MaxLogBytes
}

// optionsVersion marque la forme des options gardées : elle sert à reconnaître
// des options déjà écrites, et à rattraper les anciennes.
const optionsVersion = 2

// DefaultOptions : la diffusion vers 127.0.0.1:10110, la découverte mDNS, et le
// plafond d'enregistrement armé.
func DefaultOptions() Options {
	return Options{Version: optionsVersion, Dests: []string{gateway.UDPDefault},
		UDP: true, RecordCap: true}
}

// Store est l'endroit où une app garde ses options : les NSUserDefaults sur
// macOS, un fichier JSON sur Windows.
type Store interface {
	// Load remplit `o` ; des options jamais écrites le laissent tel quel
	// (Version à zéro), sans erreur.
	Load(o *Options) error
	Save(o Options) error
}

// LoadOptions lit les options gardées, ou rend celles par défaut.
func LoadOptions(s Store) Options {
	var o Options
	if err := s.Load(&o); err != nil || o.Version == 0 {
		return DefaultOptions()
	}
	if o.Version < 2 {
		// La v1 ignorait l'enregistrement : son plafond est armé, comme il
		// l'est pour qui n'a jamais rien réglé.
		o.RecordCap = true
		o.Version = optionsVersion
	}
	return o
}

// Config traduit les options en configuration du moteur. Le suivi va toujours
// dans `suivi.log`, l'enregistrement dans le fichier de la séance, tous deux
// sous logDir.
func (o Options) Config(logDir string) gateway.Config {
	cfg := gateway.Config{
		IP: o.IP,
		// Le nom du bateau est un réglage, pas une donnée : il faut le demander
		// en plus de l'arbre de navigation (cf. gateway.PathBoatName).
		Paths:       append(gateway.PathsDefault(), gateway.PathBoatName),
		NoteOut:     filepath.Join(logDir, "suivi.log"),
		MaxLogBytes: MaxLogBytes,
	}
	if o.UDP {
		cfg.Dests = o.Dests
	}
	if o.Record && o.RecordFile != "" {
		cfg.TraceOut = filepath.Join(logDir, o.RecordFile)
		if o.RecordCap {
			cfg.TraceMax = MaxLogBytes
		}
	}
	return cfg
}

// ToggleRecord arme ou désarme l'enregistrement. Le nom du fichier est choisi
// au moment où l'on arme, et gardé : les autres réglages relancent le moteur,
// et la séance doit se poursuivre dans le même fichier plutôt que d'en semer un
// nouveau à chaque clic.
func (o *Options) ToggleRecord(now time.Time) {
	o.Record = !o.Record
	if o.Record {
		o.RecordFile = "raynmea-" + now.Format("20060102-150405") + ".log"
		return
	}
	o.RecordFile = ""
}

// ------------------------------------------------------------- le moteur -----

// Engine tient la passerelle pour une app : les options en cours, la session
// qui les applique, et le tableau de bord qu'elle alimente. C'est l'Observer du
// moteur.
type Engine struct {
	logDir string
	store  Store
	reload chan struct{} // capacité 1, coalescé : « relis les options »

	mu   sync.Mutex
	opts Options
	dash *gateway.Dashboard // refait à chaque session
}

// NewEngine charge les options gardées dans store. Le moteur ne tourne
// qu'une fois Supervise lancé.
func NewEngine(logDir string, store Store) *Engine {
	return &Engine{logDir: logDir, store: store, reload: make(chan struct{}, 1),
		opts: LoadOptions(store), dash: gateway.NewDashboard()}
}

// LogDir est le répertoire du suivi et des enregistrements.
func (e *Engine) LogDir() string { return e.logDir }

// Supervise tient une session, et la refait à chaque changement d'option,
// jusqu'à l'annulation de root.
func (e *Engine) Supervise(root context.Context) {
	for {
		e.mu.Lock()
		opts := e.opts
		e.dash = gateway.NewDashboard()
		e.mu.Unlock()

		ctx, cancel := context.WithCancel(root)
		errc := make(chan error, 1)
		go func() { errc <- gateway.Run(ctx, opts.Config(e.logDir), e) }()

		select {
		case <-root.Done():
			cancel()
			<-errc
			return
		case <-e.reload:
			cancel()
			<-errc
		case err := <-errc:
			// Run ne rend la main de lui-même que si une sortie refuse de
			// s'ouvrir : une destination illisible, un journal impossible. Rien
			// ne se réparera tout seul — on l'affiche et on attend un réglage.
			cancel()
			if err != nil {
				e.mu.Lock()
				e.dash.Note(time.Now(), "erreur : "+err.Error(), false)
				e.mu.Unlock()
			}
			select {
			case <-root.Done():
				return
			case <-e.reload:
			}
		}
	}
}

// Apply retient les options, les enregistre, et relance le moteur. Rafraîchir
// le menu reste l'affaire de l'app.
func (e *Engine) Apply(change func(*Options)) {
	e.mu.Lock()
	change(&e.opts)
	opts := e.opts
	e.mu.Unlock()
	_ = e.store.Save(opts)
	select {
	case e.reload <- struct{}{}:
	default: // une relance déjà demandée relira les mêmes options
	}
}

// Options rend les options en cours.
func (e *Engine) Options() Options {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.opts
}

// ------------------------------------------------- ce que le moteur rend -----

// Note, Update, Link et Discovery font de l'Engine l'Observer du moteur : ils
// passent au tableau de bord de la session en cours.

func (e *Engine) Note(ts time.Time, text string, quiet bool) {
	e.dashboard().Note(ts, text, quiet)
}

func (e *Engine) Update(u gateway.Update) { e.dashboard().Update(u) }

func (e *Engine) Link(l gateway.Link) { e.dashboard().Link(l) }

// Discovery fait aussi de l'Engine un gateway.DiscoveryObserver.
func (e *Engine) Discovery(d gateway.Discovery) { e.dashboard().Discovery(d) }

// Snapshot est l'état de la session en cours, à dessiner.
func (e *Engine) Snapshot() gateway.Snapshot { return e.dashboard().Snapshot() }

func (e *Engine) dashboard() *gateway.Dashboard {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.dash
}

// ------------------------------------------------------------- utilitaires ---

// Size écrit une taille de fichier comme on la lit, virgule comprise.
func Size(n int64) string {
	switch {
	case n >= 1<<20:
		return strings.Replace(fmt.Sprintf("%.1f Mo", float64(n)/(1<<20)), ".", ",", 1)
	case n >= 1<<10:
		return fmt.Sprintf("%d ko", n>>10)
	}
	return fmt.Sprintf("%d octets", n)
}

// With ajoute v à la liste s'il n'y est pas, sans toucher à l'original.
func With(list []string, v string) []string {
	for _, x := range list {
		if x == v {
			return list
		}
	}
	return append(append([]string(nil), list...), v)
}

// Without retire v de la liste, sans toucher à l'original.
func Without(list []string, v string) []string {
	out := make([]string, 0, len(list))
	for _, x := range list {
		if x != v {
			out = append(out, x)
		}
	}
	return out
}
