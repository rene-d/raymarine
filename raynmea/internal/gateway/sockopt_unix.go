//go:build !windows

package gateway

import "syscall"

// controlBroadcast arme SO_BROADCAST sur le socket avant sa connexion : sans
// lui, écrire vers une adresse de diffusion échoue (EACCES).
func controlBroadcast(_, _ string, c syscall.RawConn) error {
	var serr error
	if err := c.Control(func(fd uintptr) {
		serr = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_BROADCAST, 1)
	}); err != nil {
		return err
	}
	return serr
}
