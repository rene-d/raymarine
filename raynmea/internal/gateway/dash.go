// dash.go — l'état affichable d'une session : les six valeurs qu'on regarde en
// naviguant, l'état de la connexion, et les compteurs.
//
// C'est un Observer, et c'est le seul endroit où l'on convertit et où l'on met
// en forme : la TUI (`tui.go`) et l'app de la barre de menus (`cmd/raynmea-menu`)
// en prennent des instantanés et se contentent de les dessiner. Deux affichages,
// un seul modèle — sans quoi les deux finiraient par ne plus dire la même chose.
package gateway

import (
	"fmt"
	"sync"
	"time"
)

// Au-delà de cet âge, une valeur est « rassise » : le MFD pousse plusieurs fois
// par seconde, cinq secondes de silence sont donc déjà une anomalie.
const staleAfter = 5 * time.Second

// Chemins RayDB dont l'affichage a besoin.
var dashPaths = []string{
	"data/sog", "data/cog", "data/position", "data/position/accuracy",
	"data/depth", "data/wind/speed/true", "data/wind/direction/true",
	"data/wind/speed/apparent", "data/wind/direction/apparent",
	PathBoatName,
}

type sample struct {
	num  float64
	str  string
	when time.Time
}

// Reading est une valeur prête à écrire : le texte, son unité, et de quoi la
// nuancer — jamais reçue, ou plus rafraîchie.
type Reading struct {
	Text  string // « 6.7 », « 48°19.231'N  004°48.273'W », « — » si inconnue
	Unit  string // « kn », « ° », « m », « °R » ; vide pour la position
	Known bool   // la valeur a été reçue au moins une fois
	Stale bool   // ... mais plus depuis staleAfter
}

// String rend « 6.7 kn », de quoi écrire une ligne de menu sans y penser.
func (r Reading) String() string {
	if r.Unit == "" {
		return r.Text
	}
	return r.Text + " " + r.Unit
}

// Snapshot est l'état à un instant, détaché : une fois rendu, il ne bouge plus
// et se lit sans verrou.
type Snapshot struct {
	Status    string
	Updates   int
	Sentences int

	// La liaison, telle que le moteur la voit, et le nom que le MFD donne au
	// bateau — vide tant qu'il ne l'a pas envoyé (il faut l'avoir souscrit,
	// cf. PathBoatName).
	IP        string
	Connected bool
	Boat      string

	SOG, COG, Depth, TWS, TWA, Position Reading
	AWS, AWA                            Reading
}

// Dashboard suit les valeurs au fil des UPDATE. Sûr à partager : Run l'alimente
// depuis son fil, l'affichage l'interroge depuis le sien.
type Dashboard struct {
	mu        sync.Mutex
	values    map[string]sample
	status    string
	link      Link
	updates   int
	sentences int
}

func NewDashboard() *Dashboard {
	return &Dashboard{values: map[string]sample{},
		status: "démarrage…"}
}

// Note retient l'état de la connexion. Les notes `quiet` (hello, abonnements)
// n'y ont pas leur place : elles chasseraient un « connecté à … » plus utile.
func (d *Dashboard) Note(_ time.Time, text string, quiet bool) {
	if quiet {
		return
	}
	d.mu.Lock()
	d.status = text
	d.mu.Unlock()
}

// Link retient l'état de la liaison : de quoi afficher le MFD suivi sans avoir
// à relire le texte des notes.
func (d *Dashboard) Link(l Link) {
	d.mu.Lock()
	d.link = l
	d.mu.Unlock()
}

// Update retient ce qui s'affiche et compte le reste.
func (d *Dashboard) Update(u Update) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.updates++
	d.sentences += u.Sentences
	for _, p := range dashPaths {
		if p == u.Path {
			// `Text` porte ici la chaîne de la valeur : data/position est une
			// chaîne « lat,lon », que formatLatLon relit.
			d.values[p] = sample{when: u.When, str: u.Text, num: u.Num}
			return
		}
	}
}

// Status rend le seul état de la connexion, sans passer par un instantané.
func (d *Dashboard) Status() string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.status
}

// Snapshot fige l'état pour l'affichage.
func (d *Dashboard) Snapshot() Snapshot {
	d.mu.Lock()
	status, updates, sentences := d.status, d.updates, d.sentences
	link := d.link
	values := make(map[string]sample, len(d.values))
	for k, v := range d.values {
		values[k] = v
	}
	d.mu.Unlock()

	now := time.Now()
	read := func(path, unit, format string, conv func(float64) float64) Reading {
		s, ok := values[path]
		if !ok {
			return Reading{Text: "—", Unit: unit}
		}
		v := s.num
		if conv != nil {
			v = conv(v)
		}
		return Reading{Text: fmt.Sprintf(format, v), Unit: unit,
			Known: true, Stale: now.Sub(s.when) > staleAfter}
	}
	// Les vitesses RayDB sont en m/s ; on les regarde en nœuds.
	speed := func(v float64) float64 { return v * msToKn }

	snap := Snapshot{Status: status, Updates: updates, Sentences: sentences,
		IP: link.IP, Connected: link.Connected}
	if s, ok := values[PathBoatName]; ok {
		snap.Boat = s.str
	}
	snap.SOG = read("data/sog", "kn", "%.1f", speed)
	snap.COG = read("data/cog", "°", "%.1f", deg360)
	snap.Depth = read("data/depth", "m", "%.1f", nil)
	snap.TWS = read("data/wind/speed/true", "kn", "%.1f", speed)

	// Le vent vrai s'affiche en angle relatif — bâbord ou tribord —, ce qui met
	// le côté dans l'unité plutôt que dans le nombre.
	snap.TWA = Reading{Text: "—", Unit: "°"}
	if s, ok := values["data/wind/direction/true"]; ok {
		a, side := angleLR(s.num)
		snap.TWA = Reading{Text: fmt.Sprintf("%.1f", a), Unit: "°" + side,
			Known: true, Stale: now.Sub(s.when) > staleAfter}
	}

	snap.AWS = read("data/wind/speed/apparent", "kn", "%.1f", speed)
	snap.AWA = Reading{Text: "—", Unit: "°"}
	if s, ok := values["data/wind/direction/apparent"]; ok {
		a, side := angleLR(s.num)
		snap.AWA = Reading{Text: fmt.Sprintf("%.1f", a), Unit: "°" + side,
			Known: true, Stale: now.Sub(s.when) > staleAfter}
	}

	snap.Position = Reading{Text: "—"}
	if s, ok := values["data/position"]; ok {
		text := formatLatLon(s.str)
		if acc, ok := values["data/position/accuracy"]; ok {
			text += fmt.Sprintf("   ±%.1f m", acc.num)
		}
		snap.Position = Reading{Text: text, Known: true,
			Stale: now.Sub(s.when) > staleAfter}
	}
	return snap
}

// formatLatLon met « 48.3206,-4.8043 » sous la forme « 48°19.236'N 004°48.258'W ».
func formatLatLon(s string) string {
	lat, ns, lon, ew, ok := latlonNMEA(s)
	if !ok {
		return "—"
	}
	// latlonNMEA rend « ddmm.mmmm » / « dddmm.mmmm » : on n'a plus qu'à couper,
	// et à laisser tomber la dernière décimale de minute — elle vaut 18 cm.
	return fmt.Sprintf("%s°%s'%s  %s°%s'%s",
		lat[:2], lat[2:len(lat)-1], ns, lon[:3], lon[3:len(lon)-1], ew)
}
