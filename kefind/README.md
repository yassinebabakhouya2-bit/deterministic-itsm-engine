# kefind — tranche 3 : choisir la bonne fiche

`kefind` est un moteur RAG déterministe en 5 temps : le LLM ne fait que
**comprendre** (temps 1) et **rédiger** (temps 5) ; le code décide tout le
reste. Il réutilise `kecore` (nettoyage, entités, décomposition des fiches,
appels LLM enregistrables) et s'expose comme un moteur `scoreboard`.

## V10, tranche 3 : trouver la fiche par les entités (`kefind.funnel`)

Depuis le 2026-10-06, la fiche se trouve **par le code, entités d'abord** ; le texte ne fait
plus que départager. Le LLM n'intervient qu'en amont, pour **interpréter** le ticket
(`kefind.interpret`, voir plus bas) ; il ne filtre ni ne décide.

```
carte du KB d'un client (un run kecore, figé et versionné)
  fiches décomposées (étapes vérifiées, entités) + profil (dictionnaire) + graphe (kefind.graph)

ticket
  │ 0. INTERPRÉTER (LLM, facultatif) termes de recherche anglais + français, vérifiés par le code
  │ 1. ENTITÉS     kecore.entities, AVEC LE MÊME DICTIONNAIRE que les fiches ;
  │                une entité absente de la carte est ignorée (et tracée)
  │ 2. FILTRE      niveaux, du plus informatif au moins informatif :
  │                réponses du technicien > numéro de fiche cité (KB0052, via le graphe)
  │                > code d'erreur / événement / n° KB ServiceNow > mise à jour > application
  │                > chemin, registre, commande, URL, menu, raccourci.
  │                Dans un niveau : AU MOINS UNE entité ; entre niveaux : TOUS.
  │                Rien ne passe -> le dernier niveau est retiré, puis le précédent (tracé).
  │                L'OS ne filtre jamais (cité en passant) ; il sert aux questions.
  │ 3. GRAPHE      doublon -> fiche canonique ; fiche remplacée -> celle qui la remplace
  │ 4. TEXTE       BM25F sur la structure kecore (titre x3 dont nom du document,
  │                symptôme/cause x2, étapes x1, corps x1), mots du ticket + termes
  │                de l'interprétation : classe, ne choisit pas
  │ 5. DÉCISION    fiche / question / abstain (FunnelConfig)
  ▼
Finding (kind, reason, fiche_id, fiches, question, options, trace)
```

**Quand une fiche est montrée directement.** Si une entité forte l'a désignée (réponse du
technicien, numéro de fiche, code d'erreur ou d'événement, mise à jour) ; ou si le ticket (ou son
interprétation) reprend au moins deux mots de son titre, nom du document compris, dont un au
moins informatif (présent dans au plus 25 % des fiches). Quand plusieurs fiches sont proches par
le texte et qu'une seule a son titre ainsi repris, c'est elle. Sinon la fiche est **proposée**
avec les suivantes (question « laquelle de ces fiches ? », trois au plus) : sans identifiant ni
titre, un vocabulaire proche ne suffit pas à affirmer. Plusieurs fiches proches et une entité qui les sépare : la question porte sur
cette entité (« l'application concernée est-elle outlook ou teams ? »), et la réponse revient
dans `answers` (`["app:teams"]`), qui filtre au premier niveau.

**Le graphe (`kefind.graph`).** Numéro de chaque fiche (id ou titre) ; doublons = textes
partageant au moins 80 % de leurs suites de 5 mots, ou même numéro et au moins 50 % ; même numéro
mais textes différents = *conflit de numéro*, jamais fusionné (client-s : KB0076 et KB0339 sont
chacun deux fiches distinctes) ; références et prérequis (numéro cité dans le texte, dans une
étape « prérequis ») ; remplacement (« remplace la fiche KB0052 », « replaced by KB0120 »).

**Interpréter (`kefind.interpret`).** Un ticket en français et une fiche en anglais ne partagent
aucun mot (« compte bloqué » contre « LOCKED ACCOUNT »). Le LLM reçoit le seul ticket et rend des
termes de recherche en anglais et en français, avec le correctif usuel quand il est connu
(« Teams écran blanc » donne « clear Teams cache ») et l'application. Le code vérifie chaque terme :
12 au plus, 6 mots au plus, aucun code d'erreur, numéro de fiche, chemin, commande, URL, menu ou
raccourci absent du ticket, aucune adresse ni téléphone. Les termes s'ajoutent aux mots du ticket
pour le texte ; ils ne filtrent jamais (une application proposée par le LLM n'est pas une
entité du ticket). La réponse est enregistrée sous l'empreinte de la requête : même ticket, mêmes
termes. Sans LLM, ou s'il échoue, la recherche se fait sur le ticket seul.

**Ce que la carte apporte en plus de kecore.** Un titre partagé par plusieurs fiches est un
intitulé de modèle, pas un titre (« General Information » ouvre 198 des 242 fiches client-s) :
le nom du document le remplace, pour l'affichage comme pour le texte. Les entités du nom du
document sont ajoutées à celles de la fiche (même code, même dictionnaire).

```python
from kefind.funnel import KBMap, find
kb_map = KBMap("clienta", fiches, dictionary=profile.dictionary)   # graphe construit si absent
finding = find(kb_map, "L'application reste bloquée, erreur 0x80070005")
finding.kind, finding.question        # "question", "... l'application concernée est-elle outlook ou teams ?"
find(kb_map, "...", answers=["app:teams"]).fiche_id                   # "KB0030002"
```

En Azure : `POST /api/kecore/find` (`kecore_func/kefind_service.py`, interprétation par défaut,
`"interpret": false` pour le code seul). Pour `scoreboard` : `kefind.funnel_engine:factory`
(`{"fiches_path": ..., "profile_path": ..., "funnel": {...}}`), ou `FunnelEngine(..., llm=...)`
pour mesurer avec l'interprétation.
Les seuils de `FunnelConfig` sont des valeurs de départ, à calibrer sur des tickets étiquetés
(tranche 4).

Le pipeline d'origine ci-dessous (recherche fusionnée, décision par score) reste en place pour
comparaison sur le scoreboard.

## Le pipeline d'origine (5 temps)

```
ticket texte
    │  (1) COMPRENDRE          kecore.text.clean_text + kecore.entities (code)
    │                          symptôme/application/contexte (LLM, JSON validé)
    ▼
TicketUnderstanding
    │  (2) CHERCHER            BM25 (kefind.search.Bm25Index) + "sens" (TF-IDF cosinus,
    │                          interface EmbeddingProvider injectable) fusionnés,
    │                          puis bonus d'entités (code d'erreur, appli, OS...)
    ▼
~20 SearchResult, triés
    │  (3) FILTRER PAR LE GRAPHE   kefind.graph_filter.filter_candidates — no-op
    │                              pour cette tranche (point d'extension, tranche 5)
    ▼
candidats filtrés
    │  (4) DÉCIDER PAR LE CODE     kefind.decide.decide, seuils calibrés
    │                              fiche / question / abstain — jamais de LLM
    ▼
Outcome
    │  (5) RÉDIGER PUIS VÉRIFIER   LLM "grand modèle" (séparé du temps 1),
    │                              ne voit que la fiche retenue ; citation vérifiée
    │                              mot pour mot (kecore.text.NormalizedText)
    ▼
Answer (résumé + étape vérifiée, jamais une étape inventée)
```

## Décider (temps 4) : les trois issues

Avec `Thresholds(min_score, gap)` :

* **fiche** — la première fiche a un score `top` et la deuxième a un écart
  `top.score - second.score >= gap` : assez nette pour trancher.
* **question** — les fiches dont le score est à moins de `gap` du meilleur
  (`close`) sont proches ; une question fermée est générée à partir de
  l'attribut qui les discrimine le mieux (application, code d'erreur,
  identifiant d'événement, OS — jamais le LLM), ou, à défaut d'un tel
  attribut, en nommant les titres des fiches candidates.
* **abstain** — soit aucun candidat, soit le meilleur score est sous
  `min_score` : rien d'assez sûr.

## Calibrer les seuils

`min_score` se calibre directement avec `scoreboard.metrics.calibrate()` sur
des tickets étiquetés : c'est un balayage de seuil d'abstention qui trouve,
de façon conservatrice (borne haute de l'IC à 95 %), le seuil le plus bas qui
garde le taux de fiches fausses sous un plafond donné. `kefind.calibration`
ne réimplémente rien, il fait tourner le moteur avec `scoreboard.runner` puis
lit le résultat avec `calibrate()` :

```python
from kefind.calibration import calibrate_engine

calibration = calibrate_engine(engine, tickets, max_wrong=0.05)
# calibration.recommended.threshold -> à mettre dans Thresholds(min_score=...)
```

En ligne de commande :

```
python -m kefind calibrate --tickets TICKETS.jsonl --client CLIENT --fiches FICHES.jsonl --max-wrong 0.05
```

`gap` n'est pas ce que `calibrate()` mesure (qui ne regarde que le seuil
d'abstention) : calibrez-le séparément, par exemple en comparant sur les
mêmes tickets le taux de questions utiles (une vraie ambiguïté) contre le
taux de questions pour rien (un cas qui aurait dû trancher) à plusieurs
valeurs de `gap`.

## Utiliser le moteur depuis scoreboard

```json
{
  "fiches_path": "clients-local/kecore/{client}/fiches.decomposed.jsonl",
  "thresholds": {"min_score": 0.18, "gap": 0.12},
  "alpha": 0.5,
  "understand_llm_config": "llm.json",
  "write_llm_config": "llm.json"
}
```

```
python -m scoreboard run TICKETS.jsonl --engine kefind.engine:factory --config kefind.json --out results/
```

Sans `understand_llm_config`/`write_llm_config`, les temps 1 et 5 retombent
sur des règles simples (symptôme = première ligne du ticket, application
trouvée par les entités, étape montrée = première étape de résolution) —
utile pour calibrer les temps 2 et 4 sans dépendre d'un LLM.

Pour fournir les fiches directement en mémoire (tests, démo) plutôt que
depuis un fichier par client, passez `"fiches": {"<client>": [...]}` avec
le format de `DecomposedFiche.to_dict()` (prioritaire sur `fiches_path`).

## Ligne de commande

```
python -m kefind decide --ticket "Outlook reste bloqué, erreur 0x80070005." \
    --client clienta --fiches kefind/examples/fiches/clienta.jsonl

python -m kefind calibrate --tickets kefind/examples/tickets.jsonl \
    --client clienta --fiches kefind/examples/fiches/clienta.jsonl
```

## Exemple (`kefind/examples/`)

* `fiches/clienta.jsonl`, `fiches/clientb.jsonl` : 5 et 2 fiches synthétiques
  décomposées par `kecore` (sans LLM, donc `status: citable`), avec un
  Outlook et un Teams qui partagent volontairement le même code d'erreur
  (`0x80070005`) pour exercer la décision "question", et un Outlook dupliqué
  entre les deux clients pour exercer l'isolation.
* `tickets.jsonl` : 7 tickets au format `scoreboard.dataset.Ticket`
  (`T-5` est l'ambigu Outlook/Teams ; `T-7` n'est couvert par aucune fiche).

Régénérés avec `kecore.decompose.Decomposer(profile=None, llm=None)` sur du
texte de fiche brut — voir les tests (`kefind/tests/helpers.py::decompose_fiche`)
pour la même recette.

## Interface d'embeddings (temps 2)

```python
class EmbeddingProvider(Protocol):
    def fit(self, corpus: Sequence[str]) -> None: ...
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...
```

`TfidfEmbeddingProvider` (par défaut) est un TF-IDF cosinus déterministe,
sans réseau. Un vrai fournisseur d'embeddings (API Azure OpenAI, modèle
local...) implémente la même interface — `fit` peut n'y rien faire — et se
passe à `FicheSearchIndex(..., embedder=...)` ou `KefindEngine(..., embedder=...)`
sans changer le reste du pipeline.

## Ce que chaque temps ne fait jamais

* Temps 1 : le LLM ne décide rien — les entités techniques viennent toujours
  des règles de `kecore.entities`, jamais du LLM.
* Temps 2 : jamais de LLM ; jamais une fiche d'un autre client (chaque
  `FicheSearchIndex` vérifie que toutes ses fiches appartiennent à son
  client) ; jamais une fiche `info_only`.
* Temps 4 : jamais de LLM — seuils et comparaison d'entités seulement.
* Temps 5 : le texte d'étape montré vient toujours d'une étape réelle de la
  `DecomposedFiche` retenue, jamais du texte brut du LLM, même quand sa
  citation est vérifiée — et jamais une étape inventée quand elle ne l'est
  pas (repli sur l'étape de résolution par défaut de la fiche).
