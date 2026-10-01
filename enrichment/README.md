# fn-kb-enrich — enrichissement semantique a l'indexation

Cette Function est appelee **par Azure AI Search**, pendant l'indexation, via un
`WebApiSkill`. Elle ne fait jamais partie du chemin d'une requete utilisateur :
si elle tombe, les recherches continuent, seul l'enrichissement des nouveaux
documents manque.

## Les deux routes

| Route | Entree | Sortie | Qui l'appelle |
|---|---|---|---|
| `POST /api/enrich` | `text`, `title` | `entities`, `entity_types`, `aliases`, `triples`, `audience` | skillset des fiches du referentiel |
| `POST /api/enrich-call` | `content`, `title`, `clientId` | `entities`, `call_problem`, `call_resolution`, `call_outcome` | skillset audio / video |

Le contrat est celui des Custom Skills : `{"values":[{"recordId","data"}]}` en
entree, `{"values":[{"recordId","data","errors","warnings"}]}` en sortie.

## Les quatre regles qui tiennent le tout

**1. Un alias n'est retenu que s'il apparait litteralement dans le texte.**
C'est `_verify_aliases()`, et c'est du code, pas une consigne au modele. Une
entite dont ni le nom ni aucun alias ne figure dans la source est rejetee en
entier : c'est une invention, pas une extraction. C'est cette verification qui
permet d'alimenter le synonym map de production **sans revue humaine** — la
sortie generative est ramenee au statut d'extraction verifiee.

**2. Le vocabulaire vient des fiches, jamais des appels.** `/api/enrich-call`
lit les entites deja presentes dans l'index du client (facette sur le champ
`entities`) et ne peut etiqueter un appel qu'avec celles-la — filtre une
premiere fois par le prompt, une seconde fois mecaniquement par le code. Les
transcriptions comportent des erreurs de reconnaissance vocale ; les laisser
creer du vocabulaire remplirait le referentiel de fantomes. Consequence directe
sur l'ordre d'indexation : **les documents texte d'abord, l'audio et la video
ensuite** (voir le script de reconstruction).

**3. Un appel est une preuve, pas une doctrine.** On en extrait un probleme, une
resolution et une issue — jamais de triplets de relation, qui injecteraient des
anecdotes dans le graphe comme si c'etaient des regles etablies.

**4. Un echec d'enrichissement est un `warning`, jamais une `error`.** Un 429 ne
doit pas faire tomber un run d'indexation : le document s'indexe, seul son
enrichissement manque, et il sera repris au run suivant.

## Determinisme

`temperature=0`, `seed=42`, deploiement de modele epingle, et cache par
empreinte SHA-256 de **(type, prompt, contenu)** dans Table Storage. Deux
indexations du meme document donnent le meme resultat ; changer le prompt
invalide le cache de lui-meme, sans purge manuelle.

## Deploiement

L'infrastructure vient de `infra/modules/enrich-function.bicep` : la Function
est hebergee sur le **plan App Service B1 existant**, celui qui fait deja
tourner la Web App, avec une identite managee systeme et un RBAC minimal.

Un plan Flex Consumption dedie a ete tente d'abord -- c'est le bon modele sur
le papier pour une charge en rafale. Il n'a jamais demarre : `Running` cote
ARM, `InternalServerError` du runtime sur toutes les routes, aucune trace ni
dans Application Insights ni dans FunctionAppLogs. Le B1 est un modele
d'hebergement deja prouve dans cette souscription. Le prix : pendant une
reindexation, l'enrichissement et l'interface partagent le CPU du plan -- sans
effet tant que les reindexations se font hors demonstration, et le vrai frein
reste de toute facon le quota TPM. L'entete du module garde cet historique.

Le code se pousse comme le reste :

```powershell
cd C:\V9\knowledgeengine-rag-platform\enrichment
func azure functionapp publish fn-knowledgeengine3-v9 --python
```

ou, sans Core Tools, par paquet zip (`az functionapp deployment source config-zip`).

### Reglages a connaitre

- `AOAI_DEPLOYMENT` (defaut `gpt-4o-enrich`) — **epingle volontairement**.
  Ce deploiement n'a pas demande de quota supplementaire : le quota gpt-4o de
  la souscription (50 kTPM, entierement consomme) a ete **decoupe en 30 pour la
  generation et 20 pour l'enrichissement**. Ce qu'on achete, c'est l'isolation —
  une reindexation complete sature son propre compartiment et ne peut plus faire
  tomber le chemin de reponse en 429. Ce qu'on paie, c'est 20 kTPM de moins en
  pointe sur les reponses. Si le quota est releve un jour, remonter
  `generationCapacity` a 50 dans `infra/modules/foundry.bicep`.
  Changer de modele change les sorties, donc devrait invalider le cache ; il
  est dans la cle de cache par le prompt, pas par le nom du modele. Si tu
  changes de modele, purge la table `enrichCache`.
- La **cle de fonction** que AI Search met dans l'URI du skill n'est pas dans
  Bicep : c'est un secret de plan de donnees. `search/deploy.ps1` la lit avec
  `az functionapp keys list` au moment du deploiement et l'injecte dans le
  skillset sans jamais l'afficher.
- `alwaysOn` est actif : l'indexeur appelle la Function en rafale apres de
  longues periodes d'inactivite, et sans ce reglage chaque run commencerait par
  un demarrage a froid sur ses premiers documents.

## Ce que la Function ne fait pas

Elle n'ecrit jamais dans l'index — c'est l'indexeur qui ecrit, avec sa propre
identite. Elle n'a sur Search qu'un droit de **lecture** (`Search Index Data
Reader`), pour la seule facette du vocabulaire.
