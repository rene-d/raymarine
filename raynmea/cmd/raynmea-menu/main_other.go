//go:build !darwin

// La barre de menus est celle de macOS : ailleurs, cette commande ne fait que
// le dire. Elle existe pour que `go build ./...` et `go vet ./...` passent sur
// toutes les plateformes — le Raspberry Pi du bord compile ce dépôt entier.
package main

import (
	"fmt"
	"os"
)

func main() {
	fmt.Fprintln(os.Stderr, "raynmea-menu : la barre de menus est propre à macOS ;"+
		" ailleurs, c'est `raynmea` qu'il faut lancer.")
	os.Exit(2)
}
