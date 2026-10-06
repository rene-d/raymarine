//go:build windows

// mdns_windows.go — la requête mDNS par le service du système (dnsapi.dll).
//
// Windows 10 et 11 ont leur propre résolveur mDNS, qui tient le port 5353 :
// lui demander de chercher, plutôt que d'ouvrir nos propres sockets à côté du
// sien, c'est n'avoir ni port à partager ni pare-feu à convaincre. Deux appels :
//
//   - `DnsServiceBrowse` liste les instances de `_raydb._tcp.local` ;
//   - `DnsServiceResolve` donne, pour chacune, son adresse IPv4.
//
// Les deux sont asynchrones : Windows rappelle une fonction de son choix, sur un
// fil à lui. Les rappels sont donc deux fonctions créées une fois pour toutes
// (`syscall.NewCallback` n'en libère jamais), qui retrouvent la requête par le
// numéro passé en contexte et lui versent leur résultat, copié — la mémoire de
// Windows est rendue avant de quitter le rappel.
//
// Une requête dure le même temps que celle de hashicorp/mdns
// (mdnsQueryTimeout), puis s'annule : la boucle de `browser.run` la refait à
// chaque tour. `DnsServiceBrowse` date de Windows 10 1903 ; plus ancien, la
// fonction manque, et la découverte se rabat sur hashicorp/mdns.
package gateway

import (
	"context"
	"errors"
	"fmt"
	"net"
	"runtime"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"github.com/hashicorp/mdns"
	"golang.org/x/sys/windows"
)

func init() { nativeBrowse = browseWindows }

const (
	dnsQueryRequestVersion1 = 1
	dnsRequestPending       = 9506 // DNS_REQUEST_PENDING : la requête est partie
	dnsFreeRecordList       = 1    // DnsFreeRecordList, pour DnsRecordListFree
)

var (
	dnsapi                  = windows.NewLazySystemDLL("dnsapi.dll")
	procServiceBrowse       = dnsapi.NewProc("DnsServiceBrowse")
	procServiceBrowseCancel = dnsapi.NewProc("DnsServiceBrowseCancel")
	procServiceResolve      = dnsapi.NewProc("DnsServiceResolve")
	procServiceResolveCncl  = dnsapi.NewProc("DnsServiceResolveCancel")
	procServiceFreeInstance = dnsapi.NewProc("DnsServiceFreeInstance")
)

// Les structures de windns.h, champ pour champ.

// DNS_SERVICE_BROWSE_REQUEST, version 1 (rappel DNS_SERVICE_BROWSE_CALLBACK).
type serviceBrowseRequest struct {
	version        uint32
	interfaceIndex uint32 // 0 : toutes les interfaces
	queryName      *uint16
	callback       uintptr
	context        uintptr
}

// DNS_SERVICE_RESOLVE_REQUEST.
type serviceResolveRequest struct {
	version        uint32
	interfaceIndex uint32
	queryName      *uint16
	callback       uintptr
	context        uintptr
}

// DNS_SERVICE_CANCEL : Windows y range de quoi annuler la requête en cours.
type serviceCancel struct{ reserved uintptr }

// DNS_SERVICE_INSTANCE, jusqu'au champ dont on a besoin.
type serviceInstance struct {
	instanceName *uint16
	hostName     *uint16
	ip4Address   *[4]byte // IP4_ADDRESS, dans l'ordre du réseau
}

// ------------------------------------------------------------ les rappels ----

// nativeResult est ce qu'un rappel rend à la requête qui l'attend.
type nativeResult struct {
	status uint32
	names  []string // browse : les instances annoncées
	ip     net.IP   // resolve : l'IPv4 de l'instance, nil si elle n'en a pas
}

var (
	callbacksOnce sync.Once
	browseCB      uintptr
	resolveCB     uintptr

	pendingMu sync.Mutex
	pending   = map[uintptr]chan nativeResult{}
	pendingID uintptr
)

// register ouvre une requête : son numéro, passé en contexte à Windows, et la
// file où ses rappels déposent leurs résultats.
func register() (uintptr, chan nativeResult) {
	callbacksOnce.Do(func() {
		browseCB = syscall.NewCallback(onBrowse)
		resolveCB = syscall.NewCallback(onResolve)
	})
	pendingMu.Lock()
	defer pendingMu.Unlock()
	pendingID++
	ch := make(chan nativeResult, 16)
	pending[pendingID] = ch
	return pendingID, ch
}

// unregister ferme une requête. Un rappel tardif (l'annulation en produit un)
// ne trouvera plus personne, et se contentera de rendre sa mémoire à Windows.
func unregister(id uintptr) {
	pendingMu.Lock()
	delete(pending, id)
	pendingMu.Unlock()
}

// deliver remet un résultat à sa requête, sans jamais bloquer le fil de
// Windows : une file pleine perd le résultat, la requête suivante le reverra.
func deliver(id uintptr, r nativeResult) {
	pendingMu.Lock()
	ch := pending[id]
	pendingMu.Unlock()
	if ch == nil {
		return
	}
	select {
	case ch <- r:
	default:
	}
}

// onBrowse est le DNS_SERVICE_BROWSE_CALLBACK : il relève les PTR (« telle
// instance offre le service ») et rend la liste à Windows.
func onBrowse(status uint32, id uintptr, rec *windows.DNSRecord) uintptr {
	r := nativeResult{status: status}
	for p := rec; p != nil; p = p.Next {
		if p.Type != windows.DNS_TYPE_PTR {
			continue
		}
		ptr := (*windows.DNSPTRData)(unsafe.Pointer(&p.Data[0]))
		if ptr.Host != nil {
			r.names = append(r.names, windows.UTF16PtrToString(ptr.Host))
		}
	}
	if rec != nil {
		windows.DnsRecordListFree(rec, dnsFreeRecordList)
	}
	deliver(id, r)
	return 0
}

// onResolve est le DNS_SERVICE_RESOLVE_COMPLETE : il relève l'IPv4 de
// l'instance et rend l'instance à Windows.
func onResolve(status uint32, id uintptr, inst *serviceInstance) uintptr {
	r := nativeResult{status: status}
	if inst != nil {
		if inst.ip4Address != nil {
			a := *inst.ip4Address
			r.ip = net.IPv4(a[0], a[1], a[2], a[3])
		}
		procServiceFreeInstance.Call(uintptr(unsafe.Pointer(inst)))
	}
	deliver(id, r)
	return 0
}

// ------------------------------------------------------------- la requête ----

// browseWindows est le nativeBrowse de Windows : la liste des instances, puis
// l'adresse de chacune, versées dans `out` comme le ferait hashicorp/mdns.
func browseWindows(ctx context.Context, wait time.Duration,
	out chan<- *mdns.ServiceEntry, debug func(string)) error {
	if err := procServiceBrowse.Find(); err != nil {
		return errors.New("DnsServiceBrowse absente (Windows 10 1903 ou plus récent requis)")
	}
	names, err := browseOnce(ctx, wait, debug)
	if err != nil {
		return err
	}
	for _, name := range names {
		ip, answered, err := resolveOnce(ctx, name, wait, debug)
		if err != nil {
			return err
		}
		if !answered || ctx.Err() != nil {
			continue
		}
		// adopt attend un nom complet, point final compris, comme les livre
		// hashicorp/mdns ; Windows l'omet.
		e := &mdns.ServiceEntry{Name: name + ".", AddrV4: ip}
		select {
		case out <- e:
		case <-ctx.Done():
			return nil
		}
	}
	return nil
}

// browseOnce écoute les annonces pendant `wait`, et rend les instances vues.
func browseOnce(ctx context.Context, wait time.Duration, debug func(string)) ([]string, error) {
	id, results := register()
	defer unregister(id)

	name, err := windows.UTF16PtrFromString(mdnsService + "." + mdnsDomain)
	if err != nil {
		return nil, err
	}
	req := &serviceBrowseRequest{version: dnsQueryRequestVersion1,
		queryName: name, callback: browseCB, context: id}
	cancel := &serviceCancel{}
	st, _, _ := procServiceBrowse.Call(uintptr(unsafe.Pointer(req)),
		uintptr(unsafe.Pointer(cancel)))
	if st != dnsRequestPending {
		return nil, fmt.Errorf("DnsServiceBrowse : %v", syscall.Errno(st))
	}

	var names []string
	seen := map[string]bool{}
	timer := time.NewTimer(wait)
	defer timer.Stop()
collect:
	for {
		select {
		case r := <-results:
			if r.status != 0 {
				debug(fmt.Sprintf("mDNS de Windows : %v", syscall.Errno(r.status)))
				continue
			}
			for _, n := range r.names {
				if !seen[n] {
					seen[n] = true
					names = append(names, n)
				}
			}
		case <-timer.C:
			break collect
		case <-ctx.Done():
			break collect
		}
	}
	procServiceBrowseCancel.Call(uintptr(unsafe.Pointer(cancel)))
	// Windows a tenu ces trois-là jusqu'à l'annulation : ils doivent vivre
	// jusqu'ici.
	runtime.KeepAlive(req)
	runtime.KeepAlive(name)
	runtime.KeepAlive(cancel)
	return names, nil
}

// resolveOnce demande l'adresse d'une instance ; `answered` dit si elle a
// répondu — sans IPv4 peut-être, adopt le relèvera. Pas de réponse dans le
// délai n'est pas une erreur : l'instance s'est tue, l'API n'y est pour rien.
func resolveOnce(ctx context.Context, instance string, wait time.Duration,
	debug func(string)) (ip net.IP, answered bool, err error) {
	id, results := register()
	defer unregister(id)

	name, err := windows.UTF16PtrFromString(instance)
	if err != nil {
		return nil, false, err
	}
	req := &serviceResolveRequest{version: dnsQueryRequestVersion1,
		queryName: name, callback: resolveCB, context: id}
	cancel := &serviceCancel{}
	st, _, _ := procServiceResolve.Call(uintptr(unsafe.Pointer(req)),
		uintptr(unsafe.Pointer(cancel)))
	if st != dnsRequestPending {
		return nil, false, fmt.Errorf("DnsServiceResolve : %v", syscall.Errno(st))
	}
	defer func() {
		runtime.KeepAlive(req)
		runtime.KeepAlive(name)
		runtime.KeepAlive(cancel)
	}()

	timer := time.NewTimer(wait)
	defer timer.Stop()
	select {
	case r := <-results:
		// La résolution est terminée : rien à annuler.
		if r.status != 0 {
			debug(fmt.Sprintf("mDNS de Windows : %s : %v",
				instanceLabel(instance+"."), syscall.Errno(r.status)))
			return nil, false, nil
		}
		return r.ip, true, nil
	case <-timer.C:
		debug(fmt.Sprintf("mDNS de Windows : %s ne répond pas", instanceLabel(instance+".")))
	case <-ctx.Done():
	}
	procServiceResolveCncl.Call(uintptr(unsafe.Pointer(cancel)))
	return nil, false, nil
}
