package desktop

import (
	"errors"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"github.com/rene-d/raymarine/raynmea/internal/gateway"
)

// memStore est un Store en mémoire : `saved` à nil, rien n'a jamais été écrit.
type memStore struct {
	saved *Options
	err   error
}

func (m *memStore) Load(o *Options) error {
	if m.err != nil {
		return m.err
	}
	if m.saved != nil {
		*o = *m.saved
	}
	return nil
}

func (m *memStore) Save(o Options) error { m.saved = &o; return nil }

func TestLoadOptions(t *testing.T) {
	// Jamais écrites, ou illisibles : les défauts.
	for _, s := range []*memStore{{}, {err: errors.New("illisible")}} {
		if got := LoadOptions(s); !reflect.DeepEqual(got, DefaultOptions()) {
			t.Errorf("LoadOptions(%+v) = %+v, veut les défauts", s, got)
		}
	}

	// Une v1 ignorait l'enregistrement : son plafond est armé.
	v1 := &memStore{saved: &Options{Version: 1, UDP: false, Dests: []string{"10.0.0.5"}}}
	got := LoadOptions(v1)
	if !got.RecordCap || got.Version != optionsVersion || got.UDP ||
		!reflect.DeepEqual(got.Dests, []string{"10.0.0.5"}) {
		t.Errorf("v1 rattrapée = %+v", got)
	}

	// Des options à jour sont rendues telles quelles — diffusion coupée comprise.
	cur := Options{Version: optionsVersion, IP: "192.168.42.1", UDP: false}
	if got := LoadOptions(&memStore{saved: &cur}); !reflect.DeepEqual(got, cur) {
		t.Errorf("LoadOptions = %+v, veut %+v", got, cur)
	}
}

func TestConfig(t *testing.T) {
	dir := t.TempDir()
	o := DefaultOptions()
	cfg := o.Config(dir)
	if !reflect.DeepEqual(cfg.Dests, []string{gateway.UDPDefault}) {
		t.Errorf("Dests = %v", cfg.Dests)
	}
	if cfg.NoteOut != filepath.Join(dir, "suivi.log") || cfg.MaxLogBytes != MaxLogBytes {
		t.Errorf("suivi : %q, %d", cfg.NoteOut, cfg.MaxLogBytes)
	}
	if cfg.TraceOut != "" {
		t.Errorf("enregistrement non armé, TraceOut = %q", cfg.TraceOut)
	}
	want := append(gateway.PathsDefault(), gateway.PathBoatName)
	if !reflect.DeepEqual(cfg.Paths, want) {
		t.Errorf("Paths = %v, veut %v", cfg.Paths, want)
	}

	// Diffusion coupée : plus de destination, même si la liste est gardée.
	o.UDP = false
	if cfg := o.Config(dir); len(cfg.Dests) != 0 {
		t.Errorf("diffusion coupée, Dests = %v", cfg.Dests)
	}

	// Enregistrement armé, plafonné puis non.
	o.ToggleRecord(time.Date(2026, 9, 6, 23, 55, 0, 0, time.Local))
	cfg = o.Config(dir)
	if cfg.TraceOut != filepath.Join(dir, "raynmea-20260906-235500.log") ||
		cfg.TraceMax != MaxLogBytes {
		t.Errorf("enregistrement : %q, %d", cfg.TraceOut, cfg.TraceMax)
	}
	o.RecordCap = false
	if cfg := o.Config(dir); cfg.TraceMax != 0 {
		t.Errorf("non plafonné, TraceMax = %d", cfg.TraceMax)
	}
	o.ToggleRecord(time.Now())
	if o.Record || o.RecordFile != "" {
		t.Errorf("désarmé : %+v", o)
	}
}

func TestApplySaves(t *testing.T) {
	s := &memStore{}
	e := NewEngine(t.TempDir(), s)
	e.Apply(func(o *Options) { o.IP = "192.168.42.1" })
	if s.saved == nil || s.saved.IP != "192.168.42.1" || e.Options().IP != "192.168.42.1" {
		t.Errorf("Apply : gardé %+v, en cours %+v", s.saved, e.Options())
	}
	select {
	case <-e.reload:
	default:
		t.Error("Apply n'a pas demandé de relance")
	}
}

func TestLists(t *testing.T) {
	l := []string{"a", "b"}
	if got := With(l, "b"); !reflect.DeepEqual(got, l) {
		t.Errorf("With doublon = %v", got)
	}
	if got := With(l, "c"); !reflect.DeepEqual(got, []string{"a", "b", "c"}) ||
		!reflect.DeepEqual(l, []string{"a", "b"}) {
		t.Errorf("With = %v (original %v)", got, l)
	}
	if got := Without(l, "a"); !reflect.DeepEqual(got, []string{"b"}) ||
		!reflect.DeepEqual(l, []string{"a", "b"}) {
		t.Errorf("Without = %v (original %v)", got, l)
	}
}

func TestSize(t *testing.T) {
	for n, want := range map[int64]string{
		512: "512 octets", 2048: "2 ko", 3 << 20: "3,0 Mo", 1572864: "1,5 Mo",
	} {
		if got := Size(n); got != want {
			t.Errorf("Size(%d) = %q, veut %q", n, got, want)
		}
	}
}
