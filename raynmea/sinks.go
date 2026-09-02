// sinks.go — les sorties : phrases NMEA et journal des UPDATE (stdout ou
// fichier), et diffusion UDP des phrases.
package main

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
	f *os.File
}

// openSink ouvre la sortie décrite par `spec` : "" (aucune), "-" (stdout), ou un
// chemin de fichier — ouvert en **ajout**, pour ne pas perdre la session
// précédente.
func openSink(spec string) (*sink, error) {
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
	return &sink{f: f}, nil
}

func (s *sink) line(text string) {
	if s == nil {
		return
	}
	// Une sortie qui casse (tuyau fermé, disque plein) ne doit pas arrêter la
	// passerelle : les autres sorties, elles, marchent toujours.
	_, _ = s.f.WriteString(text + "\n")
}

func (s *sink) close() {
	if s == nil || s.f == os.Stdout {
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
