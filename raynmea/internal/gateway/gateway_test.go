package gateway

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"math"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/hashicorp/mdns"
)

// buildUpdate reproduit ce qu'émet le MFD (cf. `mfdsim/mfdsim/raydb.py`) :
// [3 réservés][u32 type][payload][4 octets de queue].
func buildUpdate(path string, vtype uint32, payload []byte) []byte {
	block := append([]byte{0, 0, 0}, binary.LittleEndian.AppendUint32(nil, vtype)...)
	block = append(block, payload...)
	return buildFrame(opUpdate, path, append(block, 0, 0, 0, 0))
}

func f32(v float32) []byte {
	return binary.LittleEndian.AppendUint32(nil, math.Float32bits(v))
}

func TestFrameRoundTrip(t *testing.T) {
	// Deux trames dans un seul flux, et une valeur par type : c'est le découpage
	// sur le champ de longueur qui est en jeu autant que le décodage.
	var stream []byte
	stream = append(stream, buildUpdate("data/sog", typeF32, f32(3.6512))...)
	stream = append(stream, buildUpdate("data/position", typeStr,
		append(binary.LittleEndian.AppendUint64(nil, 19), "48.320600,-4.804300"...))...)
	r := bufio.NewReader(bytes.NewReader(stream))

	op, path, block, err := readFrame(r)
	if err != nil || op != opUpdate || path != "data/sog" {
		t.Fatalf("première trame : op=%d path=%q err=%v", op, path, err)
	}
	v, ok := decodeValue(block, "sog")
	if !ok || v.Kind != kindF32 || v.F32 != 3.6512 {
		t.Fatalf("data/sog : %+v (ok=%v)", v, ok)
	}
	// Le float32 doit se relire dans l'écriture qui l'a produit, sans les
	// chiffres qu'une conversion en double inventerait.
	if got := v.String(); got != "3.6512" {
		t.Errorf("écriture du float32 : %q", got)
	}

	_, path, block, err = readFrame(r)
	if err != nil || path != "data/position" {
		t.Fatalf("seconde trame : path=%q err=%v", path, err)
	}
	if v, ok := decodeValue(block, "position"); !ok || v.Str != "48.320600,-4.804300" {
		t.Fatalf("data/position : %+v (ok=%v)", v, ok)
	}
}

func TestRequestsAreByteExact(t *testing.T) {
	// Le MFD n'accepte l'abonnement que sur ces octets-là (captures E70363 /
	// E70481) : toute dérive ici casse la connexion sans rien dire. Les valeurs
	// attendues sont celles de `raydb_client.build_hello/build_subscribe`.
	for _, c := range []struct{ what, got, want string }{
		{"HELLO", hex.EncodeToString(buildHello(clientName)),
			"2d00000001000000071100000000000000" +
				"526179444252656d6f7465436c69656e74" + "0001010700000000000000000000" + "00"},
		{"SUBSCRIBE", hex.EncodeToString(buildSubscribe("data/#")),
			"22000000010000000306000000000000006461" +
				"74612f23" + "0100000700000000000000000000" + "00"},
	} {
		if c.got != c.want {
			t.Errorf("%s :\n  reçu %s\n  voulu %s", c.what, c.got, c.want)
		}
	}
}

func TestDecodeNamedRecord(t *testing.T) {
	// Type 0x0e des `diag/…` : [u64 count][u64 klen][nom][u32 type][payload].
	// Le nom double le dernier segment du chemin — il n'apprend alors rien.
	payload := binary.LittleEndian.AppendUint64(nil, 1)
	payload = binary.LittleEndian.AppendUint64(payload, uint64(len("can_address")))
	payload = append(payload, "can_address"...)
	payload = binary.LittleEndian.AppendUint32(payload, typeU32)
	payload = binary.LittleEndian.AppendUint32(payload, 22)
	block := append([]byte{0, 0, 0}, binary.LittleEndian.AppendUint32(nil, typeNamed)...)
	block = append(block, payload...)

	v, ok := decodeValue(block, "can_address")
	if !ok || v.Kind != kindU32 || v.U32 != 22 || v.Name != "" {
		t.Fatalf("nom redondant : %+v (ok=%v)", v, ok)
	}
	if v, _ := decodeValue(block, "autre"); v.String() != "can_address = 22" {
		t.Errorf("nom porteur d'information : %q", v.String())
	}
}

func TestDecodeValueRefusesGarbage(t *testing.T) {
	for _, block := range [][]byte{
		nil,
		{0, 0, 0, typeF32},                   // payload absent
		{1, 0, 0, typeF32, 0, 0, 0, 0, 0},    // octets réservés non nuls
		{0, 0, 0, 0x42, 0, 0, 0, 0, 0, 0, 0}, // type inconnu
		{0, 0, 0, typeStr, 0, 0, 0, 0xff, 0xff, 0xff, 0xff, 0, 0, 0, 0}, // longueur aberrante
	} {
		if v, ok := decodeValue(block, ""); ok {
			t.Errorf("bloc %x accepté : %+v", block, v)
		}
	}
}

func TestNaNIsNotAValue(t *testing.T) {
	// Le MFD publie NaN pour « pas de mesure » : rien ne doit sortir en NMEA.
	v, ok := decodeValue(append([]byte{0, 0, 0},
		append(binary.LittleEndian.AppendUint32(nil, typeF32),
			f32(float32(math.NaN()))...)...), "stable")
	if !ok {
		t.Fatal("bloc NaN refusé")
	}
	if _, has := v.Number(); has {
		t.Error("NaN pris pour une valeur")
	}
	if s := newBridge().handle(time.Now(), "data/cog", v); len(s) != 0 {
		t.Errorf("phrases émises sur un NaN : %v", s)
	}
}

func TestSentences(t *testing.T) {
	b := newBridge()
	ts := time.Date(2026, 7, 30, 12, 34, 56, 780_000_000, time.UTC)

	// Les chemins en cache n'émettent rien par eux-mêmes.
	for path, val := range map[string]float32{
		"data/sog": 3.6, "data/cog": 4.2, "data/bearing/variation": 0.0244,
		"data/position/altitude": 0.4,
	} {
		if path == "data/sog" {
			continue // seul déclencheur du lot
		}
		if s := b.handle(ts, path, Value{Kind: kindF32, F32: val}); len(s) != 0 {
			t.Errorf("%s a émis %v", path, s)
		}
	}
	b.cache["data/sog"] = 3.6

	got := b.handle(ts, "data/position",
		Value{Kind: kindStr, Str: "48.320600,-4.804300"})
	// Phrases relevées de `raydb_client.py` sur les mêmes valeurs — au centième
	// de seconde près, que Python tire d'un epoch flottant (123456.77) là où l'on
	// lit les nanosecondes de l'horodatage.
	want := []string{
		"$GPRMC,123456.78,A,4819.2360,N,00448.2580,W,7.0,240.6,300726,1.4,E,A*29",
		"$GPGGA,123456.78,4819.2360,N,00448.2580,W,1,,,0.4,M,,M,,*46",
		"$GPGLL,4819.2360,N,00448.2580,W,123456.78,A,A*77",
	}
	if len(got) != len(want) {
		t.Fatalf("phrases de data/position : %v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("phrase %d :\n  reçu %s\n  voulu %s", i, got[i], want[i])
		}
	}

	// Vent : l'angle est relatif à l'étrave, MWD y ajoute le cap.
	b.cache["data/wind/direction/true"] = 1.0472 // 60°
	b.cache["data/heading/true"] = 4.3458        // 249°
	got = b.handle(ts, "data/wind/speed/true", Value{Kind: kindF32, F32: 7.4})
	for i, want := range []string{
		"$IIMWV,60.0,T,14.4,N,A*3C",
		"$IIVWT,60.0,R,14.4,N,7.4,M,26.6,K*79",
		"$IIMWD,309.0,T,307.6,M,14.4,N,7.4,M*7E",
	} {
		if i >= len(got) || got[i] != want {
			t.Errorf("phrase vent %d :\n  reçu %v\n  voulu %s", i, got, want)
		}
	}
}

func TestChecksumAndAngles(t *testing.T) {
	if got := sentence("II", "HDT,249.6,T"); got != "$IIHDT,249.6,T*2B" {
		t.Errorf("somme de contrôle : %q", got)
	}
	if d := deg360(-0.1); math.Abs(d-354.2704) > 1e-3 {
		t.Errorf("deg360(-0.1) = %f", d)
	}
	if a, side := angleLR(-1.0472); math.Abs(a-60) > 1e-3 || side != "L" {
		t.Errorf("angleLR(-60°) = %f %s", a, side)
	}
	if _, _, _, _, ok := latlonNMEA("pas une position"); ok {
		t.Error("position illisible acceptée")
	}
}

// entry forge une annonce telle que `hashicorp/mdns` la livre : nom échappé à la
// façon des fichiers de zone, et le port inutile du MFD (49111).
func entry(name, ip string) *mdns.ServiceEntry {
	e := &mdns.ServiceEntry{Name: name, Port: 49111}
	if ip != "" {
		e.AddrV4 = net.ParseIP(ip)
	}
	return e
}

func TestAdoptEntry(t *testing.T) {
	const (
		mfd    = `RayDBServer\ on\ E70363\ 1234567\ 4_11_13._raydb._tcp.local.`
		autre  = `RayDBServer\ on\ E70481\ 7654321\ 4_11_13._raydb._tcp.local.`
		voisin = `Salon._airplay._tcp.local.`
	)
	var notes []string
	tgt := newTarget("")
	b := newBrowser(tgt, time.Second, func(s string) { notes = append(notes, s) },
		func(s string) { t.Log(s) })

	// La bibliothèque ne filtre pas par service : son socket multicast ramasse
	// tout, et c'est `adopt` qui écarte ce qui n'est pas RayDB.
	b.adopt(entry(voisin, "10.0.0.9"))
	// Une annonce sans adresse ne dit rien de joignable.
	b.adopt(entry(mfd, ""))
	if got := tgt.get(); got != "" {
		t.Fatalf("cible prise sur une annonce à écarter : %q", got)
	}

	b.adopt(entry(mfd, "192.168.42.1"))
	if got := tgt.get(); got != "192.168.42.1" {
		t.Fatalf("adresse découverte : %q", got)
	}
	// Le nom doit ressortir déséchappé dans la note.
	if len(notes) != 1 || !strings.Contains(notes[0], "RayDBServer on E70363 1234567") {
		t.Errorf("note de découverte : %q", notes)
	}

	// Un autre MFD ne vole pas la cible retenue…
	b.adopt(entry(autre, "192.168.42.9"))
	if got := tgt.get(); got != "192.168.42.1" {
		t.Errorf("cible détournée par un autre MFD : %q", got)
	}
	// … mais celui qu'on suit peut changer d'adresse (bail DHCP).
	b.adopt(entry(mfd, "192.168.42.42"))
	if got := tgt.get(); got != "192.168.42.42" {
		t.Errorf("changement d'adresse non suivi : %q", got)
	}
}

func TestUnescapeName(t *testing.T) {
	for in, want := range map[string]string{
		`RayDBServer\ on\ E70363._raydb._tcp.local.`: "RayDBServer on E70363._raydb._tcp.local.",
		`sans-echappement.local.`:                    "sans-echappement.local.",
		`octet\065\066.local.`:                       "octetAB.local.",
		`fin\`:                                       `fin\`,
	} {
		if got := unescapeName(in); got != want {
			t.Errorf("unescapeName(%q) = %q, voulu %q", in, got, want)
		}
	}
}

func TestWithDefaultPort(t *testing.T) {
	for in, want := range map[string]string{
		"192.168.1.42":       "192.168.1.42:10110",
		"192.168.1.42:10111": "192.168.1.42:10111",
		"127.0.0.1":          "127.0.0.1:10110",
		"255.255.255.255":    "255.255.255.255:10110",
	} {
		if got := withDefaultPort(in, nmeaUDPPort); got != want {
			t.Errorf("withDefaultPort(%q) = %q, voulu %q", in, got, want)
		}
	}
}

// hexBlock rend les octets d'un bloc valeur noté en hexadécimal.
func hexBlock(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatalf("hex : %v", err)
	}
	return b
}

func TestDecodeAllValueTypes(t *testing.T) {
	// Blocs relevés tels quels dans pcap/boat-c_*.pcap, un par type ajouté.
	// Ils vérifient l'alignement avec raydb_decode.py, dont les valeurs
	// attendues sont issues.
	for _, c := range []struct {
		what, block, want string
		kind              valueKind
	}{
		{"i32 (Diagnostics/CrashLogCount_*)",
			"000000030000005101000000000000", "337", kindInt},
		{"i64 (Settings/Data/-/10/2/…)",
			"00000004000000010000000000000000000000", "1", kindInt},
		{"liste (availableSysCarto)",
			"0000000d000000010000000000000007000000010000000000000000", "[1]", kindList},
		{"table à 5 entrées (Settings/Data/-/3/89/…)",
			"0000000e00000005000000000000000300000000000000484c" +
				"5303000000000000000300000000000000484d530300000000" +
				"00000003000000000000004c4c530300000000000000030000" +
				"00000000004c4d530300000000000000060000000000000056" +
				"656e646f72030000000100000000000000",
			"{HLS: 0, HMS: 0, LLS: 0, LMS: 0, Vendor: 1}", kindMap},
	} {
		v, ok := decodeValue(hexBlock(t, c.block), "")
		if !ok {
			t.Errorf("%s : bloc refusé", c.what)
			continue
		}
		if v.Kind != c.kind {
			t.Errorf("%s : kind=%d, voulu %d", c.what, v.Kind, c.kind)
		}
		if got := v.String(); got != c.want {
			t.Errorf("%s :\n  reçu %q\n  voulu %q", c.what, got, c.want)
		}
	}
}

func TestMultiEntryTableIsNotTruncated(t *testing.T) {
	// Le défaut corrigé : une table à plusieurs entrées ne doit pas se réduire
	// à la première. `Vendor` est la dernière des cinq.
	block := hexBlock(t, "0000000e00000005000000000000000300000000000000484c"+
		"5303000000000000000300000000000000484d530300000000"+
		"00000003000000000000004c4c530300000000000000030000"+
		"00000000004c4d530300000000000000060000000000000056"+
		"656e646f72030000000100000000000000")
	v, ok := decodeValue(block, "")
	if !ok || len(v.Fields) != 5 {
		t.Fatalf("%d entrées décodées, 5 attendues", len(v.Fields))
	}
	if v.Fields[4].Name != "Vendor" || v.Fields[4].Value.Int != 1 {
		t.Errorf("dernière entrée : %+v", v.Fields[4])
	}
}

func TestSinkRotates(t *testing.T) {
	// Le plafond sert une app qui tourne des semaines : au-delà, le fichier est
	// versé dans « .1 » et repart à zéro, et il n'en reste jamais que deux.
	path := filepath.Join(t.TempDir(), "suivi.log")
	s, err := openSink(path, 40)
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	for i := range 12 {
		s.line(fmt.Sprintf("ligne %02d", i)) // 9 octets chacune
	}

	cur, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(cur) >= 40 {
		t.Errorf("le fichier courant pèse %d octets, le plafond est 40", len(cur))
	}
	if !strings.Contains(string(cur), "ligne 11") {
		t.Errorf("la dernière ligne devrait être dans le fichier courant : %q", cur)
	}
	old, err := os.ReadFile(path + ".1")
	if err != nil {
		t.Fatalf("pas de fichier tourné : %v", err)
	}
	// Deux fichiers, pas plus : la deuxième rotation a recouvert la première,
	// et « .1 » porte donc les lignes qui précèdent immédiatement.
	if !strings.Contains(string(old), "ligne 09") {
		t.Errorf("le fichier tourné devrait porter les lignes d'avant : %q", old)
	}
	if entries, _ := filepath.Glob(path + "*"); len(entries) != 2 {
		t.Errorf("%d fichiers, 2 attendus : %v", len(entries), entries)
	}
}

func TestSinkWithoutMaxNeverRotates(t *testing.T) {
	// Le défaut de la ligne de commande : `-log capture.log` garde tout.
	path := filepath.Join(t.TempDir(), "capture.log")
	s, err := openSink(path, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	for i := range 200 {
		s.line(fmt.Sprintf("ligne %03d", i))
	}
	if _, err := os.Stat(path + ".1"); !os.IsNotExist(err) {
		t.Error("sans plafond, rien ne doit tourner")
	}
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if n := strings.Count(string(b), "\n"); n != 200 {
		t.Errorf("%d lignes gardées, 200 attendues", n)
	}
}
