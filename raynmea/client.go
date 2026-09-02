// client.go — la connexion RayDB : HELLO, abonnements, lecture des UPDATE, et
// reconnexion sans fin.
//
// Trois choses cassent une session, et toutes trois se soldent par une
// reconnexion : le MFD ferme, le MFD se tait (deadline de lecture), ou la
// découverte annonce une autre adresse pour le MFD qu'on suit — c'est ce
// dernier cas qui rend le bail DHCP inoffensif.
package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

const (
	dialTimeout = 5 * time.Second
	retryDelay  = 2 * time.Second
	// Le MFD pousse en continu ; un silence de cette durée n'est pas du calme,
	// c'est une connexion morte que rien ne signalera autrement (WiFi coupé,
	// MFD éteint : le TCP peut rester « établi » de longues minutes).
	readTimeout = 30 * time.Second
)

// event est un élément du fil, dans l'ordre où il s'est produit : un UPDATE
// (`path` non vide) ou une note de suivi. Une seule file pour les deux, comme
// dans `raydb_client.py` : « connecté à … » se lit ainsi avant les valeurs que
// la connexion vient de rendre possibles.
type event struct {
	when time.Time
	path string
	val  Value
	note string
	// quiet : note qui ne vaut que dans le fil, et ne doit pas prendre l'en-tête
	// de la TUI — l'état de la connexion y est plus utile qu'un chemin souscrit
	// une fois pour toutes.
	quiet bool
}

type client struct {
	target *target
	paths  []string
	emit   func(event)
}

func (c *client) note(format string, args ...any) {
	c.emit(event{when: time.Now(), note: fmt.Sprintf(format, args...)})
}

// quiet note une requête montante : elle appartient au fil, pas à l'en-tête.
func (c *client) quiet(format string, args ...any) {
	c.emit(event{when: time.Now(), note: fmt.Sprintf(format, args...), quiet: true})
}

// run enchaîne les sessions jusqu'à l'annulation du contexte.
func (c *client) run(ctx context.Context) {
	announced := false
	for ctx.Err() == nil {
		ip := c.target.get()
		if ip == "" {
			if !announced {
				c.note("attente d'une annonce mDNS du MFD…")
				announced = true
			}
			select {
			case <-ctx.Done():
				return
			case <-c.target.changed():
			}
			continue
		}
		announced = false
		if err := c.session(ctx, ip); err != nil && ctx.Err() == nil {
			c.note("%s : %v — reconnexion…", ip, err)
		}
		if ctx.Err() != nil {
			return
		}
		if c.target.get() != ip {
			continue // nouvelle adresse : on y va sans attendre
		}
		select {
		case <-ctx.Done():
			return
		case <-c.target.changed():
		case <-time.After(retryDelay):
		}
	}
}

// session tient une connexion du HELLO à sa perte.
func (c *client) session(ctx context.Context, ip string) error {
	addr := net.JoinHostPort(ip, strconv.Itoa(raydbPort))
	c.note("connexion à %s…", addr)
	conn, err := (&net.Dialer{Timeout: dialTimeout}).DialContext(ctx, "tcp", addr)
	if err != nil {
		return err
	}
	defer conn.Close()
	if tcp, ok := conn.(*net.TCPConn); ok {
		tcp.SetNoDelay(true)
	}
	c.note("connecté à %s", addr)

	// La connexion se ferme aussi de l'extérieur : à l'arrêt, et quand la
	// découverte donne une autre adresse au MFD — fermer le socket est la façon
	// la plus courte de sortir d'un `readFrame` bloqué. `switched` distingue
	// alors cette fermeture voulue de la perte du MFD : sans lui, la lecture se
	// plaint d'un socket qu'on vient de fermer soi-même.
	var switched atomic.Bool
	done := make(chan struct{})
	defer close(done)
	go func() {
		for {
			select {
			case <-ctx.Done():
				conn.Close()
				return
			case <-done:
				return
			case <-c.target.changed():
				if next := c.target.get(); next != ip {
					c.note("le MFD est passé en %s", next)
					switched.Store(true)
					conn.Close()
					return
				}
			}
		}
	}()

	if _, err := conn.Write(buildHello(clientName)); err != nil {
		return err
	}
	c.quiet("hello %s", clientName)
	for _, p := range c.paths {
		if _, err := conn.Write(buildSubscribe(p)); err != nil {
			return err
		}
		c.quiet("subscribe %s", p)
	}

	r := bufio.NewReaderSize(conn, 64<<10)
	for {
		if err := conn.SetReadDeadline(time.Now().Add(readTimeout)); err != nil {
			return err
		}
		op, path, block, err := readFrame(r)
		if err != nil {
			if switched.Load() || ctx.Err() != nil {
				return nil // fermeture voulue : c'est déjà dit
			}
			return readError(err)
		}
		if op != opUpdate {
			continue // ACK de keepalive et autres : rien à rendre
		}
		// Le dernier segment du chemin est seul juge du nom d'un
		// enregistrement nommé (cf. `decodeValue`).
		leaf := path
		if i := strings.LastIndexByte(path, '/'); i >= 0 {
			leaf = path[i+1:]
		}
		if v, ok := decodeValue(block, leaf); ok {
			c.emit(event{when: time.Now(), path: path, val: v})
		}
	}
}

// readError traduit les deux fins de lecture qui ne sont pas des erreurs
// réseau : la fermeture propre du MFD, et son silence.
func readError(err error) error {
	switch {
	case errors.Is(err, io.EOF), errors.Is(err, io.ErrUnexpectedEOF):
		return errors.New("connexion fermée par le MFD")
	case errors.Is(err, os.ErrDeadlineExceeded):
		return fmt.Errorf("aucune donnée depuis %s", readTimeout)
	}
	return err
}
