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
//
// Le suivi (découverte, connexion, abonnements, erreurs) va toujours sur stderr :
// stdout ne porte que ce qu'on redirige — phrases, journal ou TUI.
//
// Le moteur est dans `internal/gateway` : ce fichier n'est plus que les options
// et le choix de l'affichage. L'app macOS de la barre de menus part du même
// moteur, avec son propre affichage (cf. `cmd/raynmea-menu`).
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

	"raynmea/internal/gateway"
)

// multiFlag : une option répétable (-udp-to, -path).
type multiFlag []string

func (m *multiFlag) String() string     { return strings.Join(*m, " ") }
func (m *multiFlag) Set(v string) error { *m = append(*m, v); return nil }

func main() {
	var udpTo, paths multiFlag
	flag.Var(&udpTo, "udp-to", fmt.Sprintf(
		"diffuser les phrases vers HÔTE[:PORT] (répétable ; port par défaut : %d)",
		gateway.UDPPort))
	flag.Var(&paths, "path", "chemin RayDB à souscrire, répétable (défaut : "+
		strings.Join(gateway.PathsDefault(), " ")+")")
	noUDP := flag.Bool("no-udp", false, fmt.Sprintf(
		"ne pas diffuser (la diffusion vers %s:%d est le défaut)",
		gateway.UDPDefault, gateway.UDPPort))
	nmeaOut := flag.String("nmea", "", "où écrire les phrases NMEA : « - » pour stdout, "+
		"sinon un fichier (défaut : « - » si aucune autre sortie n'est demandée)")
	logOut := flag.String("log", "", "où journaliser les UPDATE : « - » pour stdout, "+
		"sinon un fichier")
	withTUI := flag.Bool("tui", false, "afficher SOG/COG/GPS/profondeur/TWS/TWA")
	mdnsEvery := flag.Duration("mdns-interval", gateway.MDNSDefault,
		"période d'interrogation mDNS")
	verbose := flag.Bool("verbose", false, "détailler la découverte mDNS sur stderr")
	flag.Usage = usage
	flag.Parse()

	cfg := gateway.Config{
		Paths: paths, MDNSEvery: *mdnsEvery, Verbose: *verbose,
		NMEAOut: *nmeaOut, LogOut: *logOut,
	}
	switch flag.NArg() {
	case 0:
	case 1:
		cfg.IP = flag.Arg(0)
	default:
		fail("un seul argument : l'IP du MFD")
	}

	// Diffuser, écrire, afficher : les sorties se cumulent. La diffusion est là
	// par défaut — c'est ce qu'on attend d'une passerelle —, vers la boucle locale
	// si aucune destination n'est donnée.
	switch {
	case *noUDP && len(udpTo) > 0:
		fail("-no-udp et -udp-to : il faut choisir")
	case *noUDP:
	case len(udpTo) == 0:
		cfg.Dests = []string{gateway.UDPDefault}
	default:
		cfg.Dests = udpTo
	}
	// Plus rien ne sortirait : les phrases vont alors sur stdout.
	if cfg.NMEAOut == "" && cfg.LogOut == "" && !*withTUI && len(cfg.Dests) == 0 {
		cfg.NMEAOut = "-"
	}
	if *withTUI {
		if cfg.NMEAOut == "-" || cfg.LogOut == "-" {
			fail("la TUI occupe stdout : -nmea/-log veulent un fichier")
		}
		if !gateway.TTY() {
			fail("-tui demande un terminal")
		}
	}
	if cfg.NMEAOut == "-" && cfg.LogOut == "-" {
		fail("-nmea et -log ne peuvent pas écrire tous deux sur stdout")
	}

	// Le suivi va sur stderr, sauf en TUI où il tient l'en-tête de l'écran.
	var obs gateway.Observer = stderrObserver{}
	var screen *gateway.TUI
	if *withTUI {
		dest := ""
		if len(cfg.Dests) > 0 {
			labels := make([]string, len(cfg.Dests))
			for i, d := range cfg.Dests {
				labels[i] = gateway.DestLabel(d)
			}
			dest = " · UDP → " + strings.Join(labels, ", ")
		}
		screen = gateway.NewTUI(dest)
		obs = screen
	}

	ctx, stop := signal.NotifyContext(context.Background(),
		os.Interrupt, syscall.SIGTERM)
	defer stop()

	done := make(chan struct{})
	if screen != nil {
		go screen.Run(done)
	}
	err := gateway.Run(ctx, cfg, obs)
	if screen != nil {
		close(done)
		<-screen.Stopped() // le temps de rendre le terminal
	}
	if err != nil {
		fail("%v", err)
	}
}

// stderrObserver : le suivi d'une session sans écran. Les valeurs, elles, sont
// déjà parties dans les sorties que la configuration décrit.
type stderrObserver struct{}

func (stderrObserver) Note(ts time.Time, text string, _ bool) {
	fmt.Fprintf(os.Stderr, "%s  # %s\n", ts.Format("15:04:05.000"), text)
}

func (stderrObserver) Update(gateway.Update) {}

// Link n'apprend rien de plus que les notes, qui disent déjà « connecté à … ».
func (stderrObserver) Link(gateway.Link) {}

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
