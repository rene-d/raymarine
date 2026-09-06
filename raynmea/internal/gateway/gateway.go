// Package gateway — la passerelle RayDB → NMEA 0183, sans interface.
//
// Tout ce que faisait `main()` est ici : ouvrir les sorties, lancer la
// découverte et la connexion, tenir le fil d'événements. Ce qui reste dehors,
// c'est la façon de le présenter — stderr et la TUI pour le programme en ligne
// de commande, la barre de menus pour l'app macOS (`cmd/raynmea-menu`).
//
// Le contrat est court : une `Config`, un `Observer`, et `Run` qui tourne
// jusqu'à l'annulation du contexte. Une option qui change, c'est un `Run` qu'on
// annule et qu'on relance — la reconnexion RayDB est immédiate, et rien n'a
// besoin de savoir se reconfigurer à chaud.
package gateway

import (
	"context"
	"fmt"
	"strings"
	"time"
)

// PathsDefault : l'abonnement par défaut, tout l'arbre de navigation.
func PathsDefault() []string { return []string{"data/#"} }

// UDPDefault est la destination de diffusion par défaut, telle qu'on l'écrit
// sur la ligne de commande (le port est ajouté par DestLabel/dialUDP).
const UDPDefault = nmeaUDPDefault

// UDPPort est le port NMEA 0183 conventionnel, sous-entendu par -udp-to.
const UDPPort = nmeaUDPPort

// MDNSDefault est la période d'interrogation mDNS par défaut.
const MDNSDefault = 10 * time.Second

// Config décrit une session. Les sorties se cumulent : diffusion UDP, phrases,
// journal des UPDATE, journal du suivi.
type Config struct {
	// IP du MFD. Vide : découverte mDNS permanente. Renseignée, la découverte
	// ne tourne pas du tout — une annonce ne doit pas défaire un choix explicite.
	IP    string
	Paths []string // chemins RayDB souscrits (défaut : PathsDefault)
	Dests []string // destinations UDP « hôte[:port] » ; vide : pas de diffusion

	MDNSEvery time.Duration // période d'interrogation mDNS (défaut : MDNSDefault)
	Verbose   bool          // détailler la découverte (notes « quiet »)

	// Sorties fichier : "" (aucune), "-" (stdout), sinon un chemin, ouvert en
	// ajout.
	NMEAOut string // les phrases NMEA
	LogOut  string // les UPDATE, un par ligne
	NoteOut string // le suivi (ce que l'Observer reçoit en Note)

	// TraceOut est l'enregistrement : suivi, valeurs reçues et phrases émises
	// dans **un seul** fichier, dans l'ordre où tout s'est produit. C'est de
	// quoi relire une séance sans avoir à recoller trois journaux.
	TraceOut string
	// TraceMax plafonne l'enregistrement, indépendamment de MaxLogBytes : un
	// enregistrement demandé exprès n'a pas les mêmes égards qu'un journal qui
	// tourne tout seul. 0 : il va jusqu'au bout.
	TraceMax int64

	// MaxLogBytes plafonne les sorties fichier : au-delà, le fichier est versé
	// dans « .1 » et repart à zéro. 0 (le défaut de la ligne de commande) :
	// aucune rotation, le fichier grossit tant qu'on le lui demande. L'app de la
	// barre de menus, elle, tourne des jours durant et le renseigne.
	MaxLogBytes int64
}

// PathBoatName est le chemin du nom du bateau. Il ne vit pas sous `data/…` mais
// dans les réglages, et n'arrive donc qu'à qui le souscrit **explicitement** :
// sur un MFD réel, `Settings/#` en ramènerait des milliers d'autres.
const PathBoatName = "Settings/Data/-/7/13/-/-/-/-"

// Link est l'état de la liaison avec le MFD : l'adresse qu'on suit, et si la
// session RayDB tient. Le texte des notes dit la même chose en prose ; ceci se
// lit sans l'analyser.
type Link struct {
	IP        string // adresse suivie (découverte ou imposée) ; vide : on cherche
	Connected bool
}

// Update est un UPDATE rendu à l'Observer : la valeur brute y est déjà réduite
// à ce qu'un affichage en fait — son texte et, si elle en a un, son nombre.
type Update struct {
	When  time.Time
	Path  string
	Text  string
	Num   float64
	IsNum bool
	// Sentences : le nombre de phrases NMEA que cet UPDATE a produites.
	Sentences int
}

// Observer voit passer la session. Les deux méthodes sont appelées depuis le
// fil de Run, l'une après l'autre et jamais en parallèle : une implémentation
// n'a de verrou à poser que pour ce qu'elle rend ailleurs (l'écran, le menu).
type Observer interface {
	// Note est le suivi : découverte, connexion, abonnements, erreurs. `quiet`
	// distingue le détail (hello, abonnements, mDNS verbeux) de ce qui mérite
	// l'en-tête d'un écran.
	Note(ts time.Time, text string, quiet bool)
	// Update est une valeur reçue, déjà traduite en phrases.
	Update(u Update)
	// Link est un changement d'état de la liaison : MFD trouvé, session ouverte,
	// session perdue.
	Link(l Link)
}

// DestLabel rend une destination telle qu'elle sera vraiment utilisée, port
// compris — de quoi l'annoncer avant même d'avoir ouvert le socket.
func DestLabel(dest string) string { return withDefaultPort(dest, nmeaUDPPort) }

// Run tient la passerelle jusqu'à l'annulation du contexte. Il rend une erreur
// si une sortie ne s'ouvre pas ; une fois partie, plus rien ne l'arrête que le
// contexte, les pannes de réseau étant du ressort de la reconnexion.
func Run(ctx context.Context, cfg Config, obs Observer) error {
	paths := cfg.Paths
	if len(paths) == 0 {
		paths = PathsDefault()
	}
	every := cfg.MDNSEvery
	if every <= 0 {
		every = MDNSDefault
	}

	nmeaSink, err := openSink(cfg.NMEAOut, cfg.MaxLogBytes)
	if err != nil {
		return err
	}
	defer nmeaSink.close()
	logSink, err := openSink(cfg.LogOut, cfg.MaxLogBytes)
	if err != nil {
		return err
	}
	defer logSink.close()
	noteSink, err := openSink(cfg.NoteOut, cfg.MaxLogBytes)
	if err != nil {
		return err
	}
	defer noteSink.close()
	traceSink, err := openSink(cfg.TraceOut, cfg.TraceMax)
	if err != nil {
		return err
	}
	defer traceSink.close()

	var udpSinks []*udpSink
	var labels []string
	for _, d := range cfg.Dests {
		u, err := dialUDP(d)
		if err != nil {
			return err
		}
		defer u.close()
		udpSinks = append(udpSinks, u)
		labels = append(labels, u.dest)
	}

	note := func(ts time.Time, text string, quiet bool) {
		line := fmt.Sprintf("%s  # %s", ts.Format("15:04:05.000"), text)
		noteSink.line(line)
		traceSink.line(line)
		obs.Note(ts, text, quiet)
	}

	// Les lignes ne portent que l'heure : un enregistrement qu'on relit des
	// semaines plus tard commence donc par dire quel jour et dans quelles
	// conditions il a été pris.
	if traceSink != nil {
		mfd := "MFD cherché en mDNS"
		if cfg.IP != "" {
			mfd = "MFD " + cfg.IP
		}
		diff := "sans diffusion"
		if len(labels) > 0 {
			diff = "UDP → " + strings.Join(labels, ", ")
		}
		traceSink.line(fmt.Sprintf("# raynmea %s — %s — %s — abonné à %s",
			time.Now().Format("2006-01-02 15:04:05"), mfd, diff,
			strings.Join(paths, " ")))
	}
	if len(labels) > 0 {
		note(time.Now(), "diffusion UDP → "+strings.Join(labels, ", "), false)
	}

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

	target := newTarget(cfg.IP)
	// Les notes de la découverte passent par le fil, comme celles de la
	// connexion ; le détail (Verbose) y va en « quiet » : il a sa place dans le
	// suivi, pas dans l'en-tête d'un écran.
	discovered := func(text string) { emit(event{when: time.Now(), note: text}) }
	debug := func(text string) {
		if cfg.Verbose {
			emit(event{when: time.Now(), note: text, quiet: true})
		}
	}
	// Une IP imposée l'est vraiment : la découverte ne tourne que si l'on doit
	// chercher le MFD, faute de quoi une annonce viendrait défaire le choix fait.
	if cfg.IP == "" {
		go newBrowser(target, every, discovered, debug).run(ctx)
	}

	cli := &client{target: target, paths: paths, emit: emit}
	go cli.run(ctx)

	bridge := newBridge()
	for {
		select {
		case <-ctx.Done():
			return nil
		case ev := <-events:
			if ev.link != nil {
				obs.Link(*ev.link)
				continue
			}
			if ev.path == "" {
				note(ev.when, ev.note, ev.quiet)
				continue
			}
			line := fmt.Sprintf("%s  UPDATE %-46s  %s",
				ev.when.Format("15:04:05.000"), ev.path, ev.val)
			logSink.line(line)
			traceSink.line(line)
			sentences := bridge.handle(ev.when, ev.path, ev.val)
			for _, s := range sentences {
				nmeaSink.line(s)
				traceSink.line(fmt.Sprintf("%s  NMEA   %s",
					ev.when.Format("15:04:05.000"), s))
				for _, u := range udpSinks {
					u.send(s, func(text string) { note(time.Now(), text, false) })
				}
			}
			u := Update{When: ev.when, Path: ev.path, Text: ev.val.String(),
				Sentences: len(sentences)}
			u.Num, u.IsNum = ev.val.Number()
			obs.Update(u)
		}
	}
}
