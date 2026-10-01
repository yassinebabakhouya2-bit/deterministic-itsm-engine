# kefind — tranche 3 : choisir la bonne fiche

`kefind` est un moteur RAG déterministe en 5 temps : le LLM ne fait que
**comprendre** (temps 1) et **rédiger** (temps 5) ; le code décide tout le
reste. Il réutilise `kecore` (nettoyage, entités, décomposition des fiches,
appels LLM enregistrables) et s'expose comme un moteur `scoreboard`.

## Le pipeline

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
