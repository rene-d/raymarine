// nmea.go — pont RayDB → NMEA 0183, portage de la classe `Bridge` de
// `raydb_client.py` (table des correspondances dans
// « docs/2. protocole-raydb-23333.md » §NMEA).
//
// Unités RayDB (cohérentes avec NMEA 2000 / SeaTalkNG) : angles en **radians**,
// vitesses en **m/s**, profondeurs et altitudes en **mètres**. Si le MFD envoie
// en réalité des nœuds, -knots.
//
// Chaque phrase a **un** chemin déclencheur, pour éviter les doublons ; les
// autres chemins ne font qu'alimenter un cache lu à l'émission :
//
//	déclencheur                phrases     chemins en cache
//	---------------------------------------------------------------------
//	data/position              RMC         sog, cog, bearing/variation
//	                           GGA         position/altitude
//	                           GLL         —
//	data/position/accuracy     GST         —
//	data/sog                   VTG         cog, bearing/variation
//	data/heading/true          HDT         —
//	data/heading/magnetic      HDM, HDG    bearing/variation
//	data/wind/speed/apparent   MWV (R),    wind/direction/apparent
//	                           VWR
//	data/wind/speed/true       MWV (T),    wind/direction/true,
//	                           VWT, MWD    heading/true, variation
//	data/depth                 DPT, DBT    depth/offset
//	                           DBS         depth/offset
//	data/stw                   VHW         heading/{true,magnetic}
//	data/rot                   ROT         —
//	data/rudder                RSA         —
//	data/roll                  XDR         pitch
//	data/tide/drift            VDR         tide/set
//
// Référentiels vérifiés sur les captures : `data/wind/direction/{true,apparent}`
// sont des angles **relatifs à l'étrave**, pas des directions référencées au
// nord (d'où l'ajout du cap pour MWD) ; `data/rot` est en rad/s, converti en
// degrés/minute pour ROT.
package main

import (
	"fmt"
	"math"
	"strconv"
	"strings"
	"time"
)

const (
	msToKn   = 1.943844 // m/s → nœuds
	msToKmh  = 3.6      // m/s → km/h
	mToFt    = 3.28084  // m → pieds
	mToFath  = 0.546807 // m → brasses
	radToDeg = 180 / math.Pi

	nmeaUDPPort = 10110 // port conventionnel NMEA 0183 sur UDP
	// Destination par défaut de -udp : la boucle locale, où écoutent les traceurs
	// qui tournent sur la même machine. Le broadcast reste à un -udp-to près
	// (« -udp-to 255.255.255.255 »), SO_BROADCAST étant armé de toute façon.
	nmeaUDPDefault = "127.0.0.1"
)

type bridge struct {
	knots bool               // true : les vitesses RayDB sont déjà en nœuds
	gp    string             // talker des phrases de positionnement
	ii    string             // talker des phrases instruments
	cache map[string]float64 // chemin → dernière valeur numérique
}

func newBridge(knots bool) *bridge {
	return &bridge{knots: knots, gp: "GP", ii: "II", cache: map[string]float64{}}
}

// handle rend les phrases NMEA déclenchées par cet événement RayDB.
func (b *bridge) handle(ts time.Time, path string, v Value) []string {
	if v.Kind == kindStr {
		if path == "data/position" {
			return b.position(ts, v.Str)
		}
		return nil
	}
	num, ok := v.Number()
	if !ok {
		return nil // booléen, ou NaN : le MFD ne publie pas de valeur
	}
	b.cache[path] = num

	switch path {
	case "data/position/accuracy":
		// GST — RayDB ne donne qu'une précision globale (en mètres) : on la
		// reporte en RMS et en erreurs lat/lon, et on laisse vides l'ellipse et
		// l'erreur d'altitude. Approximation, pas une mesure.
		return []string{sentence(b.gp, fmt.Sprintf("GST,%s,%.2f,,,,%.2f,%.2f,",
			hms(ts), num, num, num))}
	case "data/sog":
		return b.vtg()
	case "data/heading/true":
		return []string{sentence(b.ii, fmt.Sprintf("HDT,%.1f,T", deg360(num)))}
	case "data/heading/magnetic":
		variation, ew := b.variation()
		return []string{
			sentence(b.ii, fmt.Sprintf("HDM,%.1f,M", deg360(num))),
			sentence(b.ii, fmt.Sprintf("HDG,%.1f,,,%s,%s", deg360(num), variation, ew)),
		}
	case "data/wind/speed/apparent":
		return append(b.mwv(num, "data/wind/direction/apparent", "R"),
			b.vw(num, "data/wind/direction/apparent", "VWR")...)
	case "data/wind/speed/true":
		out := append(b.mwv(num, "data/wind/direction/true", "T"),
			b.vw(num, "data/wind/direction/true", "VWT")...)
		return append(out, b.mwd(num)...)
	case "data/depth":
		return b.depth(num)
	case "data/stw":
		return b.vhw(num)
	case "data/rot":
		// ROT est en degrés/minute dans NMEA ; RayDB pousse des rad/s.
		return []string{sentence(b.ii, fmt.Sprintf("ROT,%.1f,A", num*radToDeg*60))}
	case "data/rudder":
		return []string{sentence(b.ii, fmt.Sprintf("RSA,%.1f,A,,V", num*radToDeg))}
	case "data/roll":
		return b.xdr(num)
	case "data/tide/drift":
		return b.vdr(num)
	}
	return nil
}

// ------------------------------------------------------- accès cache ---------

func (b *bridge) get(path string) (float64, bool) {
	v, ok := b.cache[path]
	return v, ok && !math.IsNaN(v)
}

func (b *bridge) kn(v float64) float64 {
	if b.knots {
		return v
	}
	return v * msToKn
}

func (b *bridge) ms(v float64) float64 {
	if b.knots {
		return v / msToKn
	}
	return v
}

// variation rend (valeur absolue en degrés, "E"|"W") ou ("", "").
func (b *bridge) variation() (string, string) {
	v, ok := b.get("data/bearing/variation")
	if !ok {
		return "", ""
	}
	d := v * radToDeg
	if d >= 0 {
		return fmt.Sprintf("%.1f", d), "E"
	}
	return fmt.Sprintf("%.1f", -d), "W"
}

// ------------------------------------------------------- phrases -------------

func (b *bridge) position(ts time.Time, val string) []string {
	lat, ns, lon, ew, ok := latlonNMEA(val)
	if !ok {
		return nil
	}
	t := hms(ts)
	variation, vew := b.variation()
	sog := ""
	if v, ok := b.get("data/sog"); ok {
		sog = fmt.Sprintf("%.1f", b.kn(v))
	}
	cog := ""
	if v, ok := b.get("data/cog"); ok {
		cog = fmt.Sprintf("%.1f", deg360(v))
	}
	alt := ""
	if v, ok := b.get("data/position/altitude"); ok {
		alt = fmt.Sprintf("%.1f", v)
	}
	return []string{
		sentence(b.gp, fmt.Sprintf("RMC,%s,A,%s,%s,%s,%s,%s,%s,%s,%s,%s,A",
			t, lat, ns, lon, ew, sog, cog, dmy(ts), variation, vew)),
		sentence(b.gp, fmt.Sprintf("GGA,%s,%s,%s,%s,%s,1,,,%s,M,,M,,",
			t, lat, ns, lon, ew, alt)),
		sentence(b.gp, fmt.Sprintf("GLL,%s,%s,%s,%s,%s,A,A", lat, ns, lon, ew, t)),
	}
}

func (b *bridge) vtg() []string {
	sog, ok := b.get("data/sog")
	if !ok {
		return nil
	}
	cogT, cogM := "", ""
	cog, hasCog := b.get("data/cog")
	if hasCog {
		cogT = fmt.Sprintf("%.1f", deg360(cog))
	}
	if v, ok := b.get("data/bearing/variation"); ok && hasCog {
		cogM = fmt.Sprintf("%.1f", deg360(cog-v))
	}
	kn := b.kn(sog)
	return []string{sentence(b.gp, fmt.Sprintf("VTG,%s,T,%s,M,%.1f,N,%.1f,K,A",
		cogT, cogM, kn, kn*msToKmh/msToKn))}
}

func (b *bridge) mwv(speed float64, anglePath, ref string) []string {
	angle, ok := b.get(anglePath)
	if !ok {
		return nil
	}
	return []string{sentence(b.ii, fmt.Sprintf("MWV,%.1f,%s,%.1f,N,A",
		deg360(angle), ref, b.kn(speed)))}
}

// vw rend VWR / VWT — même trame que MWV, en angle 0..180 bâbord/tribord.
func (b *bridge) vw(speed float64, anglePath, kind string) []string {
	angle, ok := b.get(anglePath)
	if !ok {
		return nil
	}
	a, side := angleLR(angle)
	kn, ms := b.kn(speed), b.ms(speed)
	return []string{sentence(b.ii, fmt.Sprintf("%s,%.1f,%s,%.1f,N,%.1f,M,%.1f,K",
		kind, a, side, kn, ms, ms*msToKmh))}
}

// mwd rend MWD — direction du vent vrai référencée au **nord**, d'où l'ajout du
// cap : data/wind/direction/true est un angle relatif à l'étrave.
func (b *bridge) mwd(speed float64) []string {
	angle, hasAngle := b.get("data/wind/direction/true")
	hdt, hasHdt := b.get("data/heading/true")
	if !hasAngle || !hasHdt {
		return nil
	}
	dirM := ""
	if v, ok := b.get("data/bearing/variation"); ok {
		dirM = fmt.Sprintf("%.1f", deg360(angle+hdt-v))
	}
	kn, ms := b.kn(speed), b.ms(speed)
	return []string{sentence(b.ii, fmt.Sprintf("MWD,%.1f,T,%s,M,%.1f,N,%.1f,M",
		deg360(angle+hdt), dirM, kn, ms))}
}

func (b *bridge) depth(depth float64) []string {
	off, hasOff := b.get("data/depth/offset")
	// DPT/DBT sont mesurées sous la sonde ; DBS sous la surface, d'où l'ajout de
	// l'offset quand il est positif (sonde → ligne de flottaison).
	surf := depth
	offStr := ""
	if hasOff {
		offStr = fmt.Sprintf("%.1f", off)
		if off > 0 {
			surf = depth + off
		}
	}
	return []string{
		sentence(b.ii, fmt.Sprintf("DPT,%.1f,%s,", depth, offStr)),
		sentence(b.ii, fmt.Sprintf("DBT,%.1f,f,%.1f,M,%.1f,F",
			depth*mToFt, depth, depth*mToFath)),
		sentence(b.ii, fmt.Sprintf("DBS,%.1f,f,%.1f,M,%.1f,F",
			surf*mToFt, surf, surf*mToFath)),
	}
}

func (b *bridge) vhw(stw float64) []string {
	hdt, hdm := "", ""
	if v, ok := b.get("data/heading/true"); ok {
		hdt = fmt.Sprintf("%.1f", deg360(v))
	}
	if v, ok := b.get("data/heading/magnetic"); ok {
		hdm = fmt.Sprintf("%.1f", deg360(v))
	}
	kn := b.kn(stw)
	return []string{sentence(b.ii, fmt.Sprintf("VHW,%s,T,%s,M,%.1f,N,%.1f,K",
		hdt, hdm, kn, kn*msToKmh/msToKn))}
}

func (b *bridge) xdr(roll float64) []string {
	body := "XDR"
	if pitch, ok := b.get("data/pitch"); ok {
		body += fmt.Sprintf(",A,%.1f,D,PTCH", pitch*radToDeg)
	}
	body += fmt.Sprintf(",A,%.1f,D,ROLL", roll*radToDeg)
	return []string{sentence(b.ii, body)}
}

func (b *bridge) vdr(drift float64) []string {
	set := ""
	if v, ok := b.get("data/tide/set"); ok {
		set = fmt.Sprintf("%.1f", deg360(v))
	}
	return []string{sentence(b.ii, fmt.Sprintf("VDR,%s,T,,M,%.1f,N", set, b.kn(drift)))}
}

// ------------------------------------------------------- mise en forme -------

// sentence assemble « $<talker><body>*CS » avec la somme de contrôle NMEA.
func sentence(talker, body string) string {
	payload := talker + body
	cs := byte(0)
	for i := 0; i < len(payload); i++ {
		cs ^= payload[i]
	}
	return fmt.Sprintf("$%s*%02X", payload, cs)
}

// deg360 rend un angle en radians converti en degrés dans [0, 360).
func deg360(rad float64) float64 {
	d := math.Mod(rad*radToDeg, 360)
	if d < 0 {
		d += 360
	}
	return d
}

// angleLR rend un angle/étrave en (0..180, 'L'|'R'), pour VWR / VWT.
func angleLR(rad float64) (float64, string) {
	d := deg360(rad)
	if d > 180 {
		return 360 - d, "L"
	}
	return d, "R"
}

// latlonNMEA convertit « 50.3646,-4.13203 » en ("5021.8760","N","00407.9218","W").
func latlonNMEA(s string) (string, string, string, string, bool) {
	slat, slon, found := strings.Cut(s, ",")
	if !found {
		return "", "", "", "", false
	}
	lat, err1 := strconv.ParseFloat(strings.TrimSpace(slat), 64)
	lon, err2 := strconv.ParseFloat(strings.TrimSpace(slon), 64)
	if err1 != nil || err2 != nil || math.IsNaN(lat) || math.IsNaN(lon) {
		return "", "", "", "", false
	}
	ns, ew := "N", "E"
	if lat < 0 {
		ns = "S"
	}
	if lon < 0 {
		ew = "W"
	}
	lat, lon = math.Abs(lat), math.Abs(lon)
	return fmt.Sprintf("%02d%07.4f", int(lat), math.Mod(lat, 1)*60), ns,
		fmt.Sprintf("%03d%07.4f", int(lon), math.Mod(lon, 1)*60), ew, true
}

// hms rend l'horodatage UTC « hhmmss.ss ».
func hms(ts time.Time) string {
	t := ts.UTC()
	return fmt.Sprintf("%02d%02d%02d.%02d", t.Hour(), t.Minute(), t.Second(),
		t.Nanosecond()/10_000_000)
}

// dmy rend la date UTC « ddmmyy ».
func dmy(ts time.Time) string {
	return ts.UTC().Format("020106")
}
