// raydb.go — protocole RayDB (TCP 23333) : construction des requêtes, lecture
// des trames, décodage du bloc valeur.
//
// Portage de `raydb_decode.py` / `raydb_client.py`, spec dans
// « docs/2. protocole-raydb-23333.md ». Une trame vaut, en little-endian :
//
//	[u32 len][u32 msg_type=1][u8 op][u32 path_len][u32 pad][path][bloc valeur]
//
// où `len` couvre tout ce qui suit le champ len lui-même. Le bloc valeur est
// [3 octets réservés][u32 type][valeur][4 octets de queue], avec :
//
//	0 bool(1)  1 i8(1)  2 i16(2)  3 i32(4)  4 i64(8)  7 u32(4)
//	9 f32(4)  10 f64(8)  11 chaîne [u64 len][octets]
//	13 liste : [u64 n] puis n × ([u32 type][valeur])
//	14 table : [u64 n] puis n × ([u64 klen][clé][u32 type][valeur])
//
// Une valeur suit immédiatement son type ; seules les chaînes portent une
// longueur. Voir § 5.1 de la spec.
package gateway

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"fmt"
	"io"
	"math"
	"strconv"
	"strings"
)

const (
	raydbPort = 23333

	msgType     = 1
	opSubscribe = 3
	opUpdate    = 4
	opHello     = 7

	// Nom sous lequel le client s'annonce — celui du vrai client Raymarine.
	// Le MFD n'en fait rien, mais on reste à l'octet près sur les captures.
	clientName = "RayDBRemoteClient"

	// Un en-tête complet fait 17 octets ; aucune trame légitime n'approche 1 Mo,
	// d'où le garde-fou (une longueur aberrante veut dire flux désynchronisé).
	frameHeaderLen = 17
	maxFrameLen    = 1 << 20
)

// Types du bloc valeur (§5.1 de la spec). L'énumération se lit comme une suite
// d'entiers de largeur croissante, puis les composites. 0x05, 0x06, 0x08 et
// 0x0C n'ont jamais été observés ; leur place est déduite de leurs voisins.
const (
	typeBool  = 0x00
	typeI8    = 0x01
	typeI16   = 0x02
	typeI32   = 0x03
	typeI64   = 0x04
	typeU8    = 0x05
	typeU16   = 0x06
	typeU32   = 0x07
	typeU64   = 0x08
	typeF32   = 0x09
	typeF64   = 0x0A
	typeStr   = 0x0B
	typeList  = 0x0D // [u64 n] puis n × ([u32 type][valeur])
	typeNamed = 0x0E // table : [u64 n] puis n × ([u64 klen][clé][u32 type][valeur])
)

// ------------------------------------------------------ requêtes montantes ---

// buildFrame assemble une trame RayDB complète.
func buildFrame(op byte, path string, tail []byte) []byte {
	p := []byte(path)
	body := make([]byte, 0, 13+len(p)+len(tail))
	body = binary.LittleEndian.AppendUint32(body, msgType)
	body = append(body, op)
	body = binary.LittleEndian.AppendUint32(body, uint32(len(p)))
	body = binary.LittleEndian.AppendUint32(body, 0) // pad / flags
	body = append(body, p...)
	body = append(body, tail...)

	out := binary.LittleEndian.AppendUint32(make([]byte, 0, 4+len(body)), uint32(len(body)))
	return append(out, body...)
}

// requestTail rend le bloc valeur « vide » des requêtes :
// [3 réservés][u32 type=0x07][u64 0]. Les 3 octets réservés sont opaques —
// repris tels quels des captures, où ils diffèrent selon l'opcode.
func requestTail(reserved ...byte) []byte {
	tail := append([]byte{}, reserved...)
	tail = binary.LittleEndian.AppendUint32(tail, typeU32)
	return binary.LittleEndian.AppendUint64(tail, 0)
}

// buildHello : le client s'annonce (mêmes octets que le client Raymarine).
func buildHello(name string) []byte {
	return buildFrame(opHello, name, requestTail(0x00, 0x01, 0x01))
}

// buildSubscribe : abonnement à un chemin ou à un sous-arbre (« data/# »).
func buildSubscribe(path string) []byte {
	return buildFrame(opSubscribe, path, requestTail(0x01, 0x00, 0x00))
}

// ------------------------------------------------------------ lecture flux ---

// readFrame lit une trame et rend (op, chemin, bloc valeur). Le découpage se
// fait sur le champ de longueur : une trame peut être scindée entre plusieurs
// segments TCP, et plusieurs trames tenir dans un seul.
func readFrame(r *bufio.Reader) (op byte, path string, block []byte, err error) {
	var head [4]byte
	if _, err = io.ReadFull(r, head[:]); err != nil {
		return 0, "", nil, err
	}
	n := binary.LittleEndian.Uint32(head[:])
	if n < frameHeaderLen-4 || n > maxFrameLen {
		return 0, "", nil, fmt.Errorf("longueur de trame aberrante : %d", n)
	}
	body := make([]byte, n)
	if _, err = io.ReadFull(r, body); err != nil {
		return 0, "", nil, err
	}
	op = body[4]
	plen := int(binary.LittleEndian.Uint32(body[5:]))
	if plen < 0 || 13+plen > len(body) {
		return 0, "", nil, fmt.Errorf("chemin de %d octets dans une trame de %d", plen, n)
	}
	return op, latin1(body[13 : 13+plen]), body[13+plen:], nil
}

// --------------------------------------------------------- bloc valeur -------

type valueKind uint8

const (
	kindNone valueKind = iota
	kindBool
	kindInt // entiers autres que u32 : i8, i16, i32, i64, u8, u16, u64
	kindU32
	kindF32
	kindF64
	kindStr
	kindList
	kindMap
)

// Field est une entrée nommée d'une table (type 0x0e). Les entrées sont gardées
// en tranche et non en map : le flux les donne dans un ordre, que l'affichage
// doit rendre stable.
type Field struct {
	Name  string
	Value Value
}

// Value est une valeur RayDB décodée, dans son type d'origine. Le float32 est
// gardé sur 32 bits : converti en double, il s'étale en chiffres que le MFD n'a
// jamais envoyés (0.03009999915957451 pour un 0,0301 émis).
type Value struct {
	Kind   valueKind
	Name   string // nom réencodé d'une table à entrée unique, s'il apporte quelque chose
	Bool   bool
	Int    int64
	U32    uint32
	F32    float32
	F64    float64
	Str    string
	Items  []Value // kindList
	Fields []Field // kindMap
}

// Number rend la valeur numérique de v, et si elle en a une. Le MFD publie NaN
// pour « pas de mesure » (data/cog/stable en donne) : ce n'est pas une valeur,
// ok vaut alors false — comme le `None` du client Python.
func (v Value) Number() (float64, bool) {
	var f float64
	switch v.Kind {
	case kindInt:
		return float64(v.Int), true
	case kindU32:
		return float64(v.U32), true
	case kindF32:
		f = float64(v.F32)
	case kindF64:
		f = v.F64
	default:
		return 0, false
	}
	return f, !math.IsNaN(f) && !math.IsInf(f, 0)
}

// String rend la valeur telle qu'on l'affiche. Les flottants sortent dans leur
// écriture décimale la plus courte qui les redonne bit pour bit — à la largeur
// où ils ont été reçus, 32 ou 64 bits.
func (v Value) String() string {
	var s string
	switch v.Kind {
	case kindBool:
		s = "false"
		if v.Bool {
			s = "true"
		}
	case kindInt:
		s = strconv.FormatInt(v.Int, 10)
	case kindU32:
		s = strconv.FormatUint(uint64(v.U32), 10)
	case kindF32:
		s = strconv.FormatFloat(float64(v.F32), 'g', -1, 32)
	case kindF64:
		s = strconv.FormatFloat(v.F64, 'g', -1, 64)
	case kindStr:
		s = v.Str
	case kindList:
		parts := make([]string, len(v.Items))
		for i, item := range v.Items {
			parts[i] = item.String()
		}
		s = "[" + strings.Join(parts, ", ") + "]"
	case kindMap:
		parts := make([]string, len(v.Fields))
		for i, fld := range v.Fields {
			parts[i] = fld.Name + ": " + fld.Value.String()
		}
		s = "{" + strings.Join(parts, ", ") + "}"
	default:
		return ""
	}
	if v.Name != "" {
		return v.Name + " = " + s
	}
	return s
}

// decodeValue décode le bloc valeur d'un UPDATE. `leaf` est le dernier segment
// du chemin : une table à entrée unique (type 0x0e, la forme des `diag/…`)
// réencode le nom du champ, qui double le plus souvent ce segment — on ne garde
// le nom que s'il en diffère, auquel cas il porte une information que le chemin
// n'a pas. Au-delà d'une entrée, la table est rendue telle quelle : n'en garder
// que la première perdait les autres en silence.
func decodeValue(b []byte, leaf string) (Value, bool) {
	if len(b) < 8 || b[0] != 0 || b[1] != 0 || b[2] != 0 {
		return Value{}, false
	}
	vtype := binary.LittleEndian.Uint32(b[3:])
	if vtype == typeNamed && len(b) >= 15 && binary.LittleEndian.Uint64(b[7:]) == 1 {
		// [u64 count=1 @7][u64 klen @15][nom @23][u32 type][valeur]
		if len(b) < 23 {
			return Value{}, false
		}
		klen := binary.LittleEndian.Uint64(b[15:])
		if klen > uint64(len(b)-23) {
			return Value{}, false
		}
		o := 23 + int(klen)
		if len(b) < o+4 {
			return Value{}, false
		}
		v, _, ok := decodeAt(b, o+4, binary.LittleEndian.Uint32(b[o:]))
		if !ok {
			return Value{}, false
		}
		if name := latin1(b[23:o]); name != leaf {
			v.Name = name
		}
		return v, true
	}
	v, _, ok := decodeAt(b, 7, vtype)
	return v, ok
}

// Largeur des types à taille fixe, en octets.
var scalarWidth = map[uint32]int{
	typeBool: 1, typeI8: 1, typeU8: 1, typeI16: 2, typeU16: 2,
	typeI32: 4, typeU32: 4, typeF32: 4, typeI64: 8, typeU64: 8, typeF64: 8,
}

// decodeAt lit la valeur de type `vtype` à l'offset `o` et rend l'offset de fin.
// La valeur suit immédiatement son type : seules les chaînes portent une
// longueur. Listes et tables s'imbriquent, d'où la récursion.
func decodeAt(b []byte, o int, vtype uint32) (Value, int, bool) {
	if w, fixed := scalarWidth[vtype]; fixed {
		if o < 0 || len(b) < o+w {
			return Value{}, 0, false
		}
		end := o + w
		switch vtype {
		case typeBool:
			return Value{Kind: kindBool, Bool: b[o] != 0}, end, true
		case typeI8:
			return Value{Kind: kindInt, Int: int64(int8(b[o]))}, end, true
		case typeU8:
			return Value{Kind: kindInt, Int: int64(b[o])}, end, true
		case typeI16:
			return Value{Kind: kindInt, Int: int64(int16(binary.LittleEndian.Uint16(b[o:])))}, end, true
		case typeU16:
			return Value{Kind: kindInt, Int: int64(binary.LittleEndian.Uint16(b[o:]))}, end, true
		case typeI32:
			return Value{Kind: kindInt, Int: int64(int32(binary.LittleEndian.Uint32(b[o:])))}, end, true
		case typeU32:
			return Value{Kind: kindU32, U32: binary.LittleEndian.Uint32(b[o:])}, end, true
		case typeI64, typeU64:
			return Value{Kind: kindInt, Int: int64(binary.LittleEndian.Uint64(b[o:]))}, end, true
		case typeF32:
			return Value{Kind: kindF32, F32: math.Float32frombits(binary.LittleEndian.Uint32(b[o:]))}, end, true
		case typeF64:
			return Value{Kind: kindF64, F64: math.Float64frombits(binary.LittleEndian.Uint64(b[o:]))}, end, true
		}
	}

	switch vtype {
	case typeStr:
		if o < 0 || len(b) < o+8 {
			return Value{}, 0, false
		}
		n := binary.LittleEndian.Uint64(b[o:])
		if n > uint64(len(b)-o-8) {
			return Value{}, 0, false
		}
		return Value{Kind: kindStr, Str: latin1(b[o+8 : o+8+int(n)])}, o + 8 + int(n), true

	case typeList, typeNamed:
		if o < 0 || len(b) < o+8 {
			return Value{}, 0, false
		}
		n := binary.LittleEndian.Uint64(b[o:])
		if n > uint64(len(b)) { // compteur aberrant : bloc tronqué ou mal cadré
			return Value{}, 0, false
		}
		o += 8
		v := Value{Kind: kindList}
		if vtype == typeNamed {
			v = Value{Kind: kindMap}
		}
		for i := uint64(0); i < n; i++ {
			name := ""
			if vtype == typeNamed {
				if len(b) < o+8 {
					return Value{}, 0, false
				}
				klen := binary.LittleEndian.Uint64(b[o:])
				o += 8
				if klen > uint64(len(b)-o) {
					return Value{}, 0, false
				}
				name = latin1(b[o : o+int(klen)])
				o += int(klen)
			}
			if len(b) < o+4 {
				return Value{}, 0, false
			}
			inner, end, ok := decodeAt(b, o+4, binary.LittleEndian.Uint32(b[o:]))
			if !ok {
				return Value{}, 0, false
			}
			o = end
			if vtype == typeNamed {
				v.Fields = append(v.Fields, Field{Name: name, Value: inner})
			} else {
				v.Items = append(v.Items, inner)
			}
		}
		return v, o, true
	}
	return Value{}, 0, false
}

// latin1 rend la chaîne portée par ces octets, coupée au premier NUL — au-delà,
// c'est un tampon réutilisé non nettoyé. Le protocole est en latin1 : chaque
// octet est un point de code, là où Go supposerait de l'UTF-8.
func latin1(b []byte) string {
	if i := bytes.IndexByte(b, 0); i >= 0 {
		b = b[:i]
	}
	for _, c := range b {
		if c > 0x7f {
			r := make([]rune, len(b))
			for i, c := range b {
				r[i] = rune(c)
			}
			return string(r)
		}
	}
	return string(b)
}
