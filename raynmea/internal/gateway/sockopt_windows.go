//go:build windows

package gateway

import "syscall"

// controlBroadcast arme SO_BROADCAST sur le socket avant sa connexion (cf. la
// variante Unix ; seul le type du descripteur change).
func controlBroadcast(_, _ string, c syscall.RawConn) error {
	var serr error
	if err := c.Control(func(fd uintptr) {
		serr = syscall.SetsockoptInt(syscall.Handle(fd), syscall.SOL_SOCKET,
			syscall.SO_BROADCAST, 1)
	}); err != nil {
		return err
	}
	return serr
}
