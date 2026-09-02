// raynmea — passerelle RayDB → NMEA 0183, en Go.
//
// Équivalent de `raydb_client.py --udp` : découverte mDNS permanente du MFD,
// connexion RayDB (TCP 23333) avec reconnexion, abonnement à `data/#`,
// traduction des UPDATE en phrases NMEA 0183, diffusion UDP, journal des UPDATE,
// et une TUI minimaliste.
//
// Une seule dépendance, `github.com/hashicorp/mdns`, pour la découverte.
//
// La diffusion est le mode **par défaut** : sans rien dire, les phrases partent
// en UDP vers 127.0.0.1:10110, où les écoute un traceur local (OpenCPN, Avalon,
// `socat -u UDP4-RECV:10110,reuseaddr -`).
//
// Usage :
//
//	raynmea                              # diffusion UDP 127.0.0.1:10110, MFD trouvé en mDNS
//	raynmea 192.168.42.1                 # IP imposée (pas de découverte)
//	raynmea -udp-to 192.168.1.42:10110   # diffuser ailleurs (répétable)
//	raynmea -udp-to 255.255.255.255      # ... en broadcast sur tout le réseau
//	raynmea -nmea nmea.log               # diffuser *et* garder la trace des phrases
//	raynmea -nmea -                      # ... ou les voir passer sur stdout
//	raynmea -log updates.log             # journal des UPDATE dans un fichier
//	raynmea -tui                         # SOG/COG/GPS/fond/TWS/TWA à l'écran
//	raynmea -no-udp -nmea -              # pas de diffusion, phrases sur stdout
//	raynmea -knots                       # les vitesses RayDB sont déjà en nœuds
//
// Le suivi (découverte, connexion, abonnements, erreurs) va toujours sur stderr :
// stdout ne porte que ce qu'on redirige — phrases, journal ou TUI.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

// Abonnement par défaut : tout l'arbre de navigation. -path, répétable, le
// remplace — le MFD accepte plusieurs SUBSCRIBE sur une même connexion.
var defaultPaths = []string{"data/#"}

// multiFlag : une option répétable (-udp-to, -path).
type multiFlag []string

func (m *multiFlag) String() string     { return strings.Join(*m, " ") }
func (m *multiFlag) Set(v string) error { *m = append(*m, v); return nil }

func main() {
	var udpTo, paths multiFlag
	flag.Var(&udpTo, "udp-to", fmt.Sprintf(
		"diffuser les phrases vers HÔTE[:PORT] (répétable ; port par défaut : %d)",
		nmeaUDPPort))
	flag.Var(&paths, "path", "chemin RayDB à souscrire, répétable (défaut : "+
		strings.Join(defaultPaths, " ")+")")
	noUDP := flag.Bool("no-udp", false, fmt.Sprintf(
		"ne pas diffuser (la diffusion vers %s:%d est le défaut)",
		nmeaUDPDefault, nmeaUDPPort))
	nmeaOut := flag.String("nmea", "", "où écrire les phrases NMEA : « - » pour stdout, "+
		"sinon un fichier (défaut : « - » si aucune autre sortie n'est demandée)")
	logOut := flag.String("log", "", "où journaliser les UPDATE : « - » pour stdout, "+
		"sinon un fichier")
	withTUI := flag.Bool("tui", false, "afficher SOG/COG/GPS/profondeur/TWS/TWA")
	knots := flag.Bool("knots", false, "les vitesses RayDB sont déjà en nœuds (défaut : m/s)")
	mdnsEvery := flag.Duration("mdns-interval", 10*time.Second,
		"période d'interrogation mDNS")
	verbose := flag.Bool("verbose", false, "détailler la découverte mDNS sur stderr")
	flag.Usage = usage
	flag.Parse()

	ip := ""
	switch flag.NArg() {
	case 0:
	case 1:
		ip = flag.Arg(0)
	default:
		fail("un seul argument : l'IP du MFD")
	}

	// Diffuser, écrire, afficher : les sorties se cumulent. La diffusion est là
	// par défaut — c'est ce qu'on attend d'une passerelle —, vers la boucle locale
	// si aucune destination n'est donnée.
	dests := append(multiFlag{}, udpTo...)
	switch {
	case *noUDP && len(dests) > 0:
		fail("-no-udp et -udp-to : il faut choisir")
	case *noUDP:
		dests = nil
	case len(dests) == 0:
		dests = multiFlag{nmeaUDPDefault}
	}
	// Plus rien ne sortirait : les phrases vont alors sur stdout.
	if *nmeaOut == "" && *logOut == "" && !*withTUI && len(dests) == 0 {
		*nmeaOut = "-"
	}
	if *withTUI {
		if *nmeaOut == "-" || *logOut == "-" {
			fail("la TUI occupe stdout : -nmea/-log veulent un fichier")
		}
		if !tuiTTY() {
			fail("-tui demande un terminal")
		}
	}
	if *nmeaOut == "-" && *logOut == "-" {
		fail("-nmea et -log ne peuvent pas écrire tous deux sur stdout")
	}
	if len(paths) == 0 {
		paths = defaultPaths
	}

	nmeaSink, err := openSink(*nmeaOut)
	if err != nil {
		fail("%v", err)
	}
	defer nmeaSink.close()
	logSink, err := openSink(*logOut)
	if err != nil {
		fail("%v", err)
	}
	defer logSink.close()

	var udpSinks []*udpSink
	var labels []string
	for _, d := range dests {
		u, err := dialUDP(d)
		if err != nil {
			fail("%v", err)
		}
		defer u.close()
		udpSinks = append(udpSinks, u)
		labels = append(labels, u.dest)
	}

	// Le suivi va sur stderr, sauf en TUI où il tient l'en-tête de l'écran.
	var screen *tui
	if *withTUI {
		dest := ""
		if len(labels) > 0 {
			dest = " · UDP → " + strings.Join(labels, ", ")
		}
		screen = newTUI(*knots, dest)
	}
	note := func(ts time.Time, text string) { noteQuiet(screen, ts, text, false) }
	if len(labels) > 0 {
		note(time.Now(), "diffusion UDP → "+strings.Join(labels, ", "))
	}

	ctx, stop := signal.NotifyContext(context.Background(),
		os.Interrupt, syscall.SIGTERM)
	defer stop()

	// Un seul fil d'événements, alimenté par la connexion et par la découverte :
	// les notes se lisent ainsi à leur place parmi les valeurs. L'envoi bloque —
	// si une sortie prend du retard, c'est la lecture du socket qui ralentit,
	// personne ne perd d'événement en silence.
	events := make(chan event, 1024)
	emit := func(ev event) {
		select {
		case events <- ev:
		case <-ctx.Done():
		}
	}

	target := newTarget(ip)
	// Les notes de la découverte passent par le fil, comme celles de la
	// connexion ; le détail (-verbose) y va en « quiet » : il a sa place dans le
	// suivi, pas dans l'en-tête de la TUI.
	discovered := func(text string) { emit(event{when: time.Now(), note: text}) }
	debug := func(text string) {
		if *verbose {
			emit(event{when: time.Now(), note: text, quiet: true})
		}
	}
	// Une IP imposée l'est vraiment : la découverte ne tourne que si l'on doit
	// chercher le MFD, faute de quoi une annonce viendrait défaire le choix de
	// la ligne de commande.
	if ip == "" {
		go newBrowser(target, *mdnsEvery, discovered, debug).run(ctx)
	}

	cli := &client{target: target, paths: paths, emit: emit}
	go cli.run(ctx)

	done := make(chan struct{})
	if screen != nil {
		go screen.run(done)
	}

	bridge := newBridge(*knots)
	for {
		select {
		case <-ctx.Done():
			if screen != nil {
				close(done)
				<-screen.stopped // le temps de rendre le terminal
			}
			return
		case ev := <-events:
			if ev.path == "" {
				noteQuiet(screen, ev.when, ev.note, ev.quiet)
				continue
			}
			logSink.line(fmt.Sprintf("%s  UPDATE %-46s  %s",
				ev.when.Format("15:04:05.000"), ev.path, ev.val))
			sentences := bridge.handle(ev.when, ev.path, ev.val)
			for _, s := range sentences {
				nmeaSink.line(s)
				for _, u := range udpSinks {
					u.send(s, func(text string) { note(time.Now(), text) })
				}
			}
			if screen != nil {
				screen.feed(ev, len(sentences))
			}
		}
	}
}

// noteQuiet écrit une note de suivi : sur stderr, ou dans l'en-tête de la TUI —
// que les notes `quiet` (hello, abonnements) ne prennent pas, l'état de la
// connexion y étant plus utile.
func noteQuiet(screen *tui, ts time.Time, text string, quiet bool) {
	if screen != nil {
		if !quiet {
			screen.setStatus(text)
		}
		return
	}
	fmt.Fprintf(os.Stderr, "%s  # %s\n", ts.Format("15:04:05.000"), text)
}

func usage() {
	out := flag.CommandLine.Output()
	fmt.Fprint(out, `raynmea — passerelle RayDB (MFD Raymarine, TCP 23333) → NMEA 0183.

Usage : raynmea [options] [IP du MFD]

Sans IP, le MFD est découvert par mDNS (_raydb._tcp.local.) et suivi en
permanence : s'il change d'adresse, la connexion suit. Le port mDNS annoncé est
ignoré (le MFD publie 49111, RayDB écoute sur 23333).

Sans option, les phrases NMEA sont diffusées en UDP vers 127.0.0.1:10110.

Options :
`)
	flag.PrintDefaults()
	fmt.Fprint(out, `
Exemples :
  raynmea                              diffusion UDP vers 127.0.0.1:10110
  raynmea -udp-to 192.168.1.42         diffuser ailleurs (port 10110 par défaut)
  raynmea -udp-to 255.255.255.255      en broadcast sur tout le réseau
  raynmea -nmea -                      diffuser et voir les phrases sur stdout
  raynmea -tui                         écran de veille + diffusion
  raynmea -log updates.log             journal des UPDATE dans un fichier
`)
}

func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "raynmea: "+format+"\n", args...)
	os.Exit(2)
}
