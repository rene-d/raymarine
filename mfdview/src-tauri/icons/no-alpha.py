#!/usr/bin/env python3
"""Réécrit des PNG sans canal alpha (macOS, via Quartz).

Apple refuse une icône d'application qui porte un canal alpha — même
entièrement opaque, même sur une icône qui ne montre aucune transparence : la
validation regarde le canal, pas les pixels. Or `cargo tauri icon` en laisse un
sur les icônes iOS qu'il produit, `--ios-color` remplissant les zones
transparentes sans supprimer le canal pour autant.

D'où ce complément, appelé par `just icons` sur le jeu d'icônes iOS. Le dessin
est simplement redessiné dans un contexte sans alpha, ce qui est sans perte :
les icônes en sortent identiques à l'œil, et acceptables par App Store Connect.

    python3 no-alpha.py fichier.png [fichier.png…]
"""

import sys

import Quartz


def no_alpha(path: str) -> bool:
    """Réécrit `path` sans canal alpha. Rend False si l'écriture a échoué."""
    url = Quartz.CFURLCreateFromFileSystemRepresentation(
        None, path.encode(), len(path), False)
    source = Quartz.CGImageSourceCreateWithURL(url, None)
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    width, height = Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)

    # `kCGImageAlphaNoneSkipLast` : quatre octets par pixel, mais le quatrième
    # n'est pas un alpha — c'est ce qui fait un PNG à trois canaux en sortie.
    context = Quartz.CGBitmapContextCreate(
        None, width, height, 8, 0,
        Quartz.CGColorSpaceCreateDeviceRGB(),
        Quartz.kCGImageAlphaNoneSkipLast)
    Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, width, height), image)

    destination = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(
        destination, Quartz.CGBitmapContextCreateImage(context), None)
    return bool(Quartz.CGImageDestinationFinalize(destination))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    failed = [p for p in sys.argv[1:] if not no_alpha(p)]
    for path in failed:
        print(f"no-alpha : {path} non réécrit", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
