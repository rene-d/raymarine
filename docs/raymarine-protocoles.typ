// =============================================================================
//  Raymarine — Rétro-ingénierie du réseau et des protocoles MFD
//  Agrégat PDF des notes Markdown 0..7 de ce dossier, qui restent la source
//  unique : ce fichier n'énumère que les chapitres, la mise en page est dans
//  template.typ.
//  Compilation :  typst compile docs/raymarine-protocoles.typ   (ou : just build)
// =============================================================================

#import "template.typ": chapitre, rapport

#show: rapport.with(
  titre: "Raymarine",
  sous-titre: "Rétro-ingénierie du réseau et des protocoles MFD",
  mention: "Axiom · LightHouse · RayConnect",
  entete: "Raymarine — rétro-ingénierie des protocoles MFD",
)

#chapitre("1. protocole-udp5800.md")
#chapitre("2. protocole-raydb-23333.md")
#chapitre("3. protocole-rrce-50000.md")
#chapitre("4. ssh-mfd-analyse.md")
#chapitre("5. protocole-messages-8182.md")
