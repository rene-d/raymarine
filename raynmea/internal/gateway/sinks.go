// sinks.go — les sorties : phrases NMEA et journal des UPDATE (stdout ou
// fichier), et diffusion UDP des phrases.
package gateway

import (
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"time"
)

// sink est une sortie ligne à ligne. L'écriture n'est pas tamponnée : ce qui est
// écrit est parti, qu'on suive au `tail -f` ou dans un tuyau.
type sink struct {
	f    *os.File
	path string // vide pour stdout : il n'y a alors rien à faire tourner
	max  int64  // plafond avant rotation ; 0 : aucune
	n    int64  // ce que le fichier pèse à l'instant
}

// openSink ouvre la sortie décrite par `spec` : "" (aucune), "-" (stdout), ou un
// chemin de fichier — ouvert en **ajout**, pour ne pas perdre la session
// précédente. `max` plafonne le fichier (0 : pas de rotation).
func openSink(spec string, max int64) (*sink, error) {
	switch spec {
	case "":
		return nil, nil
	case "-":
		return &sink{f: os.Stdout}, nil
	}
	f, err := os.OpenFile(spec, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	s := &sink{f: f, path: spec, max: max}
	// Le fichier ouvert en ajout pèse déjà quelque chose : la rotation doit
	// compter à partir de là, pas à partir de zéro.
	if st, err := f.Stat(); err == nil {
		s.n = st.Size()
	}
	return s, nil
}

func (s *sink) line(text string) {
	if s == nil || s.f == nil {
		return
	}
	// Une sortie qui casse (tuyau fermé, disque plein) ne doit pas arrêter la
	// passerelle : les autres sorties, elles, marchent toujours.
	n, _ := s.f.WriteString(text + "\n")
	s.n += int64(n)
	if s.max > 0 && s.n >= s.max {
		s.rotate()
	}
}

// rotate verse le fichier dans « .1 » et repart à zéro : deux fichiers au plus,
// c'est ce qu'il faut pour qu'une app qui tourne des semaines ne remplisse pas
// le disque, et assez pour retrouver ce qui vient de se passer. Un échec (disque
// plein, répertoire disparu) laisse simplement le fichier grossir : il n'y a
// rien de mieux à faire, et sûrement pas arrêter la passerelle.
func (s *sink) rotate() {
	if s.path == "" {
		return
	}
	s.f.Close()
	if err := os.Rename(s.path, s.path+".1"); err != nil {
		// Rien renommé : on rouvre et on repart du compteur à zéro, faute de
		// quoi chaque ligne suivante retenterait la rotation. Le fichier
		// dépassera le plafond d'autant, et on réessaiera au prochain.
		s.f, s.n = nil, 0
		if f, err := os.OpenFile(s.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644); err == nil {
			s.f = f
		}
		return
	}
	f, err := os.OpenFile(s.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		s.f = nil // plus de fichier : on se tait plutôt que d'écrire n'importe où
		return
	}
	s.f, s.n = f, 0
}

func (s *sink) close() {
	if s == nil || s.f == nil || s.f == os.Stdout {
		return
	}
	s.f.Close()
}

// ------------------------------------------------------------ diffusion UDP --

// udpSink diffuse les phrases vers une destination. Le socket est connecté :
// une destination injoignable se signale alors (ICMP port unreachable) au lieu
// de partir dans le vide.
type udpSink struct {
	conn    *net.UDPConn
	dest    string
	lastErr string
	failed  int       // erreurs depuis la dernière fois qu'on l'a dit
	said    time.Time // ... et quand on l'a dite
}

// Une destination sourde refuse un paquet sur deux (l'ICMP « port unreachable »
// ne remonte qu'à l'écriture suivante) : la dire à chaque fois noierait le
// suivi, ne pas la dire du tout cacherait une adresse fautive. On la répète donc
// au plus toutes les 30 s, avec le compte.
const udpErrEvery = 30 * time.Second

// dialUDP prépare la diffusion vers « HÔTE[:PORT] ». SO_BROADCAST est armé dans
// tous les cas : c'est la seule façon d'écrire vers 255.255.255.255 ou vers
// l'adresse de diffusion du réseau local, et il ne gêne pas les autres.
func dialUDP(dest string) (*udpSink, error) {
	addr := withDefaultPort(dest, nmeaUDPPort)
	d := net.Dialer{Control: controlBroadcast}
	conn, err := d.Dial("udp4", addr)
	if err != nil {
		return nil, err
	}
	return &udpSink{conn: conn.(*net.UDPConn), dest: addr}, nil
}

// send émet une phrase, terminée en CR-LF comme l'exige NMEA 0183.
func (u *udpSink) send(sentence string, note func(string)) {
	_, err := u.conn.Write([]byte(sentence + "\r\n"))
	if err == nil {
		return
	}
	u.failed++
	msg := err.Error()
	if msg == u.lastErr && time.Since(u.said) < udpErrEvery {
		return
	}
	count := ""
	if u.failed > 1 {
		count = fmt.Sprintf(" (%d phrases perdues)", u.failed)
	}
	note(fmt.Sprintf("UDP %s : %s%s", u.dest, msg, count))
	u.lastErr, u.failed, u.said = msg, 0, time.Now()
}

func (u *udpSink) close() { u.conn.Close() }

// withDefaultPort complète « hôte » en « hôte:port » si le port est absent.
func withDefaultPort(dest string, port int) string {
	if _, _, err := net.SplitHostPort(dest); err == nil {
		return dest
	}
	return net.JoinHostPort(strings.TrimSpace(dest), strconv.Itoa(port))
}
