// mdns.go — découverte **permanente** du MFD par mDNS/Bonjour.
//
// Le MFD annonce `_raydb._tcp.local.` ; l'enregistrement porte directement son IP
// joignable (cf. « docs/2. protocole-raydb-23333.md »). Le **port** annoncé, lui,
// ne vaut rien : le MFD réel publie 49111 alors que RayDB écoute sur 23333 — on
// ne lit donc que l'adresse.
//
// La résolution est celle de `github.com/hashicorp/mdns`, qui pose un vrai
// IP_MULTICAST_IF par interface (via `x/net/ipv4`). Sa `Query` étant bornée par
// un délai et refermant ses sockets, la découverte permanente est ici une boucle
// de requêtes : on interroge toutes les `period`, sur chaque interface porteuse
// d'une IPv4, jusqu'à l'arrêt. Un MFD qui apparaît ou qui change d'adresse est
// donc vu au tour suivant, et non à l'instant de son annonce spontanée — sur un
// bateau, où le WiFi du MFD va et vient et où son bail DHCP peut changer, dix
// secondes de latence ne coûtent rien, et le changement d'adresse force la
// reconnexion (cf. `client.go`).
package gateway

import (
	"context"
	"fmt"
	"io"
	"log"
	"net"
	"strings"
	"sync"
	"time"

	"github.com/hashicorp/mdns"
)

const (
	mdnsService = "_raydb._tcp"
	mdnsDomain  = "local"
	// Suffixe des noms d'instance du service, à quoi on les reconnaît.
	mdnsSuffix = "._raydb._tcp.local."

	// Durée d'une requête. `mdns.Query` attend toujours la fin de ce délai, même
	// après avoir trouvé : c'est le temps laissé aux réponses d'arriver, pas un
	// délai d'attente maximal.
	mdnsQueryTimeout = 2 * time.Second
)

// ------------------------------------------------------------ cible ----------

// target porte l'adresse du MFD, telle que la découverte la connaît à l'instant.
// Une valeur plutôt qu'une file : le client relit toujours la dernière adresse
// connue, et une annonce ne doit ni s'accumuler ni se perdre.
type target struct {
	mu     sync.Mutex
	ip     string
	notify chan struct{} // capacité 1, coalescé
}

func newTarget(ip string) *target {
	return &target{ip: ip, notify: make(chan struct{}, 1)}
}

func (t *target) get() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.ip
}

// set retient une adresse et signale, si elle change.
func (t *target) set(ip string) bool {
	t.mu.Lock()
	if ip == "" || ip == t.ip {
		t.mu.Unlock()
		return false
	}
	t.ip = ip
	t.mu.Unlock()
	select {
	case t.notify <- struct{}{}:
	default: // un signal en attente dit déjà « relis la cible »
	}
	return true
}

func (t *target) changed() <-chan struct{} { return t.notify }

// ------------------------------------------------------------ navigateur -----

type browser struct {
	target  *target
	note    func(string)
	debug   func(string)
	period  time.Duration
	mu      sync.Mutex
	chosen  string          // instance retenue (nom mDNS complet, déséchappé)
	ignored map[string]bool // instances écartées, pour ne le dire qu'une fois
}

func newBrowser(t *target, period time.Duration, note, debug func(string)) *browser {
	return &browser{target: t, note: note, debug: debug, period: period,
		ignored: map[string]bool{}}
}

// run interroge jusqu'à l'annulation du contexte.
func (b *browser) run(ctx context.Context) {
	// Les envois de `mdns.Query` ne bloquent pas : une entrée arrivée alors que
	// la file est pleine est perdue, d'où un tampon et un consommateur dédié.
	entries := make(chan *mdns.ServiceEntry, 32)
	go func() {
		for {
			select {
			case e := <-entries:
				b.adopt(e)
			case <-ctx.Done():
				return
			}
		}
	}()

	tick := time.NewTicker(b.period)
	defer tick.Stop()
	for {
		b.round(ctx, entries)
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
	}
}

// round interroge toutes les interfaces d'un coup. En parallèle, parce que
// chaque requête consomme son délai en entier : en série, un tour durerait le
// nombre d'interfaces multiplié par `mdnsQueryTimeout`.
func (b *browser) round(ctx context.Context, entries chan<- *mdns.ServiceEntry) {
	// La bibliothèque journalise d'elle-même sur stderr (échecs de requête
	// d'instance, réponses vides) : on la fait taire, le suivi de raynmea passe
	// par `note` et `debug`.
	quiet := log.New(io.Discard, "", 0)
	var wg sync.WaitGroup
	for _, ifi := range ipv4Interfaces() {
		wg.Add(1)
		go func(ifi net.Interface) {
			defer wg.Done()
			err := mdns.QueryContext(ctx, &mdns.QueryParam{
				Service:   mdnsService,
				Domain:    mdnsDomain,
				Timeout:   mdnsQueryTimeout,
				Interface: &ifi,
				Entries:   entries,
				// RayDB est en IPv4 ; interroger en v6 ne ferait que doubler les
				// sockets et les erreurs sur les interfaces sans adresse v6.
				DisableIPv6: true,
				Logger:      quiet,
			})
			if err != nil && ctx.Err() == nil {
				b.debug(fmt.Sprintf("mDNS %s : %v", ifi.Name, err))
			}
		}(ifi)
	}
	wg.Wait()
}

// adopt examine une entrée annoncée et en tire, s'il s'agit du MFD, son adresse.
func (b *browser) adopt(e *mdns.ServiceEntry) {
	if e == nil {
		return
	}
	// `hashicorp/mdns` ne vérifie pas que la réponse correspond au service
	// demandé (TODO explicite dans son client.go) : son socket multicast ramasse
	// tout ce qui passe, et une annonce complète d'un autre service en ressort
	// comme une entrée. Le filtre est donc ici.
	inst := unescapeName(e.Name)
	if !strings.HasSuffix(strings.ToLower(inst), mdnsSuffix) {
		return
	}
	if e.AddrV4 == nil {
		b.debug(fmt.Sprintf("mDNS : %s annoncé sans adresse IPv4", instanceLabel(inst)))
		return
	}
	if !b.keep(inst) {
		return
	}
	if b.target.set(e.AddrV4.String()) {
		b.note(fmt.Sprintf("MFD découvert (mDNS) : %s (%s)",
			e.AddrV4, instanceLabel(inst)))
	}
}

// keep dit si cette instance est celle qu'on suit. La première annoncée est
// retenue, et on s'y tient : sur un réseau à plusieurs MFD, changer de cible à
// chaque annonce ferait osciller la connexion entre eux.
func (b *browser) keep(inst string) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.chosen == "" {
		b.chosen = inst
	}
	if strings.EqualFold(b.chosen, inst) {
		return true
	}
	if !b.ignored[inst] {
		b.ignored[inst] = true
		b.note(fmt.Sprintf("autre MFD annoncé (%s), ignoré", instanceLabel(inst)))
	}
	return false
}

// instanceLabel réduit « RayDBServer on E70363 1234567 v3_11._raydb._tcp.local. »
// à sa partie utile.
func instanceLabel(inst string) string {
	return strings.TrimSuffix(strings.TrimSuffix(inst, "."), "._raydb._tcp.local")
}

// unescapeName rend un nom DNS lisible : la bibliothèque le livre échappé à la
// façon des fichiers de zone (« RayDBServer\ on\ E70363 », et « \DDD » pour un
// octet non imprimable), or le nom d'instance du MFD porte des espaces.
func unescapeName(name string) string {
	if !strings.ContainsRune(name, '\\') {
		return name
	}
	var b strings.Builder
	b.Grow(len(name))
	for i := 0; i < len(name); i++ {
		if name[i] != '\\' || i+1 >= len(name) {
			b.WriteByte(name[i])
			continue
		}
		i++
		if i+2 < len(name) && isDigit(name[i]) && isDigit(name[i+1]) && isDigit(name[i+2]) {
			b.WriteByte((name[i]-'0')*100 + (name[i+1]-'0')*10 + name[i+2] - '0')
			i += 2
			continue
		}
		b.WriteByte(name[i])
	}
	return b.String()
}

func isDigit(c byte) bool { return c >= '0' && c <= '9' }

// ipv4Interfaces liste les interfaces actives, capables de multicast et
// **porteuses d'une IPv4**, la boucle locale comprise — c'est par elle que passe
// un simulateur (`mfdsim`) tournant sur la même machine.
//
// Le filtre sur l'adresse n'est pas cosmétique : sans lui, chaque requête sur
// une interface sans IPv4 (`utun*`, `awdl0`, `anpi*` d'un Mac — une vingtaine)
// échoue en « can't assign requested address », à chaque tour.
func ipv4Interfaces() []net.Interface {
	all, err := net.Interfaces()
	if err != nil {
		return nil
	}
	var out []net.Interface
	for _, ifi := range all {
		if ifi.Flags&net.FlagUp == 0 || ifi.Flags&net.FlagMulticast == 0 {
			continue
		}
		if hasIPv4(ifi) {
			out = append(out, ifi)
		}
	}
	return out
}

func hasIPv4(ifi net.Interface) bool {
	addrs, err := ifi.Addrs()
	if err != nil {
		return false
	}
	for _, a := range addrs {
		if n, ok := a.(*net.IPNet); ok && n.IP.To4() != nil {
			return true
		}
	}
	return false
}
