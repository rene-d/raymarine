# Compilation du rapport PDF, à partir des Markdown 0..7 de ce dossier.

doc := "raymarine-protocoles"

# liste les recettes
default:
    @just --list

# compile le PDF
build:
    typst compile {{ doc }}.typ

# recompile à chaque modification des Markdown ou du gabarit
watch:
    typst watch {{ doc }}.typ

# compile puis ouvre le PDF
open: build
    open {{ doc }}.pdf

# aperçu PNG d'une page, pour vérifier une mise en page (ex. : just page 16)
page n:
    typst compile --pages {{ n }} {{ doc }}.typ /tmp/{{ doc }}-{{ n }}.png --ppi 110
    open /tmp/{{ doc }}-{{ n }}.png

# supprime le PDF produit
clean:
    rm -f {{ doc }}.pdf
