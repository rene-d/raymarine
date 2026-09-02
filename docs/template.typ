// =============================================================================
//  Gabarit du rapport Raymarine.
//  Le contenu vit dans les Markdown 0..7 de ce dossier : ce fichier ne porte
//  que la mise en page. Rendu Markdown → Typst par @preview/cmarker
//  (téléchargé et mis en cache au premier `typst compile`).
// =============================================================================

#import "@preview/cmarker:0.1.6"

// Encadré : rendu des blockquotes Markdown (`> …`).
#let note(body) = block(
  width: 100%, fill: rgb("#fff8e6"),
  stroke: (left: 3pt + rgb("#f0b429")),
  inset: 9pt, radius: 2pt, body,
)

// Un chapitre = un fichier Markdown voisin de raymarine-protocoles.typ.
// Son titre `#` devient un titre de niveau 1 (nouvelle page + filet).
#let chapitre(path) = cmarker.render(
  read(path),
  h1-level: 1,
  blockquote: note,
  // Les `---` du Markdown séparent les sections ; les titres et les sauts de
  // page suffisent à l'écrit. Pour les afficher : line.with(length: 100%).
  scope: (rule: () => none),
)

#let rapport(
  titre: "",
  sous-titre: "",
  mention: "",
  auteur: "rene-d",
  entete: "",
  doc,
) = {
  set document(title: titre + " — " + sous-titre, author: auteur)
  set page(
    paper: "a4",
    margin: (x: 2.2cm, top: 2.6cm, bottom: 2.2cm),
    numbering: "1",
    header: context {
      if counter(page).get().first() > 1 {
        set text(size: 8pt, fill: luma(130))
        entete
        line(length: 100%, stroke: 0.4pt + luma(210))
      }
    },
  )
  set text(lang: "fr", size: 10pt, hyphenate: true)
  set par(justify: true, leading: 0.62em)
  set heading(numbering: none)

  show link: set text(fill: rgb("#1d4ed8"))
  show raw.where(block: false): box.with(
    fill: luma(238), inset: (x: 3pt), outset: (y: 3pt), radius: 2pt,
  )
  show raw.where(block: true): block.with(
    fill: luma(246), inset: 8pt, radius: 3pt, width: 100%, stroke: 0.5pt + luma(220),
  )

  set table(stroke: 0.5pt + luma(210), inset: (x: 6pt, y: 4pt))
  show table.cell.where(y: 0): set text(weight: "bold")

  // Les renvois d'un document à l'autre citent le fichier source ; à l'écrit,
  // le PDF est d'un seul tenant : on retire l'extension.
  show regex("\.md\b"): ""

  // Cases à cocher GFM (`- [x]` / `- [ ]`), que cmarker laisse en texte brut.
  show regex("\[x\]"): "✅"
  show regex("\[ \]"): "☐"

  // Les URL nues des tableaux d'équipements : cliquables dans le PDF.
  // (aucune URL dans les blocs de code des sources, la règle est sans risque)
  // Pour des tableaux plus compacts :  it => link(it.text)[lien]
  show regex("https?://[^\s|)\]]+"): it => link(it.text, it.text)

  // Titres de partie (un par fichier Markdown) : nouvelle page + filet.
  show heading.where(level: 1): it => {
    pagebreak(weak: true)
    it
    v(-0.15em)
    line(length: 100%, stroke: 0.7pt + luma(140))
    v(0.4em)
  }
  show heading.where(level: 1): set text(size: 18pt)
  show heading.where(level: 2): set text(size: 13pt)

  // ------------------------------------------------------------ page de titre
  v(4.5cm)
  align(center)[
    #text(size: 28pt, weight: "bold")[#titre]
    #v(0.3em)
    #text(size: 15pt)[#sous-titre]
    #v(0.5em)
    #text(size: 11pt, fill: luma(90))[#mention]
    #v(2.2cm)
    #text(size: 10pt)[
      Compilation des notes techniques du dépôt \
      #datetime.today().display("[day]/[month]/[year]")
    ]
  ]
  pagebreak()

  outline(title: "Sommaire", depth: 2, indent: auto)

  doc
}
