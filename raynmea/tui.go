// tui.go — TUI minimaliste : SOG, COG, GPS, profondeur, TWS, TWA.
//
// Six valeurs, celles qu'on regarde en naviguant, et rien d'autre : pas de
// bibliothèque d'écran, quelques séquences ANSI suffisent. L'écran alterné
// (`?1049h`) rend le terminal tel qu'il était à la sortie, et une valeur qui
// n'est plus rafraîchie s'affiche en grisé — un capteur muet se voit ainsi sans
// avoir à afficher son âge.
package main

import (
	"fmt"
	"os"
	"strings"
	"sync"
	"time"
)

// Au-delà de cet âge, une valeur est grisée : le MFD pousse plusieurs fois par
// seconde, cinq secondes de silence sont donc déjà une anomalie.
const tuiStale = 5 * time.Second

const tuiRefresh = 250 * time.Millisecond

// Chemins RayDB dont la TUI a besoin.
var tuiPaths = []string{
	"data/sog", "data/cog", "data/position", "data/position/accuracy",
	"data/depth", "data/wind/speed/true", "data/wind/direction/true",
}

type sample struct {
	num  float64
	str  string
	when time.Time
}

type tui struct {
	knots bool
	dest  string // libellé de la diffusion, pour le pied de page
	// stopped est fermé quand l'écran a rendu le terminal : personne ne doit
	// écrire sur stdout avant, sous peine de laisser un terminal muet.
	stopped chan struct{}

	mu        sync.Mutex
	values    map[string]sample
	status    string
	updates   int
	sentences int
}

func newTUI(knots bool, dest string) *tui {
	return &tui{knots: knots, dest: dest, stopped: make(chan struct{}),
		values: map[string]sample{}, status: "démarrage…"}
}

func (t *tui) setStatus(text string) {
	t.mu.Lock()
	t.status = text
	t.mu.Unlock()
}

// feed retient ce qui intéresse l'écran et compte le reste.
func (t *tui) feed(ev event, sentences int) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.updates++
	t.sentences += sentences
	for _, p := range tuiPaths {
		if p == ev.path {
			s := sample{when: ev.when, str: ev.val.Str}
			s.num, _ = ev.val.Number()
			t.values[p] = s
			return
		}
	}
}

// run dessine jusqu'à l'arrêt, puis rend le terminal intact.
func (t *tui) run(done <-chan struct{}) {
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

func (t *tui) render() string {
	t.mu.Lock()
	status, updates, sentences := t.status, t.updates, t.sentences
	values := make(map[string]sample, len(t.values))
	for k, v := range t.values {
		values[k] = v
	}
	t.mu.Unlock()

	now := time.Now()
	// field rend « valeur unité » selon l'âge de la mesure : grisée si le
	// capteur s'est tu, « — » s'il n'a jamais parlé.
	field := func(path, format, unit string, conv func(float64) float64) string {
		s, ok := values[path]
		if !ok {
			return dim(fmt.Sprintf("%9s %-3s", "—", unit))
		}
		v := s.num
		if conv != nil {
			v = conv(v)
		}
		text := fmt.Sprintf("%9s %-3s", fmt.Sprintf(format, v), unit)
		if now.Sub(s.when) > tuiStale {
			return dim(text)
		}
		return text
	}

	twa := func() string {
		s, ok := values["data/wind/direction/true"]
		if !ok {
			return dim(fmt.Sprintf("%9s %-3s", "—", "°"))
		}
		a, side := angleLR(s.num)
		text := fmt.Sprintf("%9s %-3s", fmt.Sprintf("%.1f", a), "°"+side)
		if now.Sub(s.when) > tuiStale {
			return dim(text)
		}
		return text
	}

	gps := func() string {
		s, ok := values["data/position"]
		if !ok {
			return dim("—")
		}
		text := formatLatLon(s.str)
		if acc, ok := values["data/position/accuracy"]; ok {
			text += fmt.Sprintf("   ±%.1f m", acc.num)
		}
		if now.Sub(s.when) > tuiStale {
			return dim(text)
		}
		return text
	}

	speed := func(v float64) float64 {
		if t.knots {
			return v
		}
		return v * msToKn
	}
	degrees := func(v float64) float64 { return deg360(v) }

	lines := []string{
		bold(" raynmea ") + " " + status,
		"",
		"   SOG " + field("data/sog", "%.1f", "kn", speed) +
			"        COG " + field("data/cog", "%.1f", "°", degrees),
		"   GPS       " + gps(),
		"  FOND " + field("data/depth", "%.1f", "m", nil),
		"   TWS " + field("data/wind/speed/true", "%.1f", "kn", speed) +
			"        TWA " + twa(),
		"",
		fmt.Sprintf(" %d updates · %d phrases%s", updates, sentences, t.dest),
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

// tuiTTY dit si stdout est un terminal — la TUI n'a de sens que là.
func tuiTTY() bool {
	st, err := os.Stdout.Stat()
	return err == nil && st.Mode()&os.ModeCharDevice != 0
}
