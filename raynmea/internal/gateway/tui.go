// tui.go — TUI minimaliste : SOG, COG, GPS, profondeur, TWS, TWA.
//
// Six valeurs, celles qu'on regarde en naviguant, et rien d'autre : pas de
// bibliothèque d'écran, quelques séquences ANSI suffisent. L'écran alterné
// (`?1049h`) rend le terminal tel qu'il était à la sortie, et une valeur qui
// n'est plus rafraîchie s'affiche en grisé — un capteur muet se voit ainsi sans
// avoir à afficher son âge.
//
// L'état affiché vient du `Dashboard` (cf. dash.go), que l'app de la barre de
// menus lit tout pareil : ici, il ne reste que le dessin.
package gateway

import (
	"fmt"
	"os"
	"strings"
	"time"
)

const tuiRefresh = 250 * time.Millisecond

// TUI est un Observer qui dessine l'écran de veille.
type TUI struct {
	*Dashboard
	dest string // libellé de la diffusion, pour le pied de page
	// stopped est fermé quand l'écran a rendu le terminal : personne ne doit
	// écrire sur stdout avant, sous peine de laisser un terminal muet.
	stopped chan struct{}
}

func NewTUI(dest string) *TUI {
	return &TUI{Dashboard: NewDashboard(), dest: dest,
		stopped: make(chan struct{})}
}

// Stopped est fermé quand l'écran a rendu le terminal.
func (t *TUI) Stopped() <-chan struct{} { return t.stopped }

// Run dessine jusqu'à l'arrêt, puis rend le terminal intact.
func (t *TUI) Run(done <-chan struct{}) {
	out := os.Stdout
	defer close(t.stopped)
	fmt.Fprint(out, "\x1b[?1049h\x1b[?25l")
	defer fmt.Fprint(out, "\x1b[?25h\x1b[?1049l")
	tick := time.NewTicker(tuiRefresh)
	defer tick.Stop()
	for {
		fmt.Fprint(out, t.render())
		select {
		case <-done:
			return
		case <-tick.C:
		}
	}
}

func (t *TUI) render() string {
	s := t.Snapshot()

	// field cadre « valeur unité » et grise ce qui n'est plus rafraîchi.
	field := func(r Reading) string {
		text := fmt.Sprintf("%9s %-3s", r.Text, r.Unit)
		if !r.Known || r.Stale {
			return dim(text)
		}
		return text
	}
	gps := func(r Reading) string {
		if !r.Known || r.Stale {
			return dim(r.Text)
		}
		return r.Text
	}

	lines := []string{
		bold(" raynmea ") + " " + s.Status,
		"",
		"   SOG " + field(s.SOG) + "        COG " + field(s.COG),
		"   GPS       " + gps(s.Position),
		"  FOND " + field(s.Depth),
		"   TWS " + field(s.TWS) + "        TWA " + field(s.TWA),
		"",
		fmt.Sprintf(" %d updates · %d phrases%s", s.Updates, s.Sentences, t.dest),
		dim(" Ctrl-C pour quitter"),
	}
	var b strings.Builder
	b.WriteString("\x1b[H")
	for _, l := range lines {
		b.WriteString(l)
		b.WriteString("\x1b[K\r\n")
	}
	b.WriteString("\x1b[J")
	return b.String()
}

func dim(s string) string  { return "\x1b[2m" + s + "\x1b[0m" }
func bold(s string) string { return "\x1b[1m\x1b[7m" + s + "\x1b[0m" }

// TTY dit si stdout est un terminal — la TUI n'a de sens que là.
func TTY() bool {
	st, err := os.Stdout.Stat()
	return err == nil && st.Mode()&os.ModeCharDevice != 0
}
