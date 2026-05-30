# Plan — Clear honnête (UI ↔ sessions agents)

**Statut :** plan validé, convergé entre `claude` et `codex`, arbitré par Théo — **pas un patch**.
**Date :** 2026-05-29
**Repo :** `1. PROJETS/DEV/agentchattr` (R2 fermé par défaut — voir §9).
**Source :** canal `#clear-feature` (AgentChattr), messages 409→436. Toutes les références `fichier:ligne` ci-dessous ont été vérifiées sur le filesystem pendant l'audit (état repo au 2026-05-29, R2 resté fermé donc aucune dérive de code).

## Suivi de livraison validé

| Lot | Statut | Note |
|---|---|---|
| PR0 — Bugfix télémétrie recovery Claude | Fait | Livré séparément, sans sémantique clear. |
| PR1 — Fondation clear | Fait | Couche de vérité livrée, sans injection terminale. |
| PR2 — Workflow UI honnête | Fait | Confirmation UI par agent, sans action terminale. |
| PR3 — Adaptateur Claude pilote (restart) | En cours | Implémentation + tests mock ; activation par défaut bloquée jusqu'à preuve live Claude. |

---

## 1. Le problème : trois états confondus

`/clear` mélange aujourd'hui trois choses distinctes que l'UI traite comme une seule :

1. **Chat / store UI vidé** — le transcript affiché est effacé.
2. **Métriques usage invalidées / remises à zéro** — le compteur de tokens.
3. **Contexte réel du process agent vidé** — la mémoire de la CLI.

Aujourd'hui, AgentChattr ne garantit que (1). Le handler `/clear` (`app.py:1464-1467`) fait **uniquement** `store.clear()` (`store.py:201-219`) + `broadcast_clear()` (`app.py:1303-1308`, event WS UI-only). Il n'injecte rien dans les terminaux et ne touche pas `usage_snapshots`.

**Conséquence :** après `/clear`, le transcript UI est vidé mais la CLI garde tout son contexte et ses tokens. Il n'y a **pas** de faux succès aujourd'hui (aucun clear terminal n'est tenté). Le faux succès serait **introduit** si on câblait `/clear → injection` sans confirmation. C'est précisément ce que ce plan empêche.

---

## 2. Objectif produit & non-objectifs

### Objectif
Transformer `/clear` en action **honnête** :
- toujours pouvoir vider le chat/store UI ;
- afficher ce qui est **vrai** pour chaque agent (capacité, pas présence) ;
- ne jamais afficher `confirmed` pour un contexte CLI sans **preuve définie par adaptateur** ;
- séparer clairement `not_supported`, `not_applicable`, `pending`, `confirmed`, `failed`.

### Non-objectifs (interdits explicites)
- **Pas** de bouton global « clear all terminals confirmed ».
- **Pas** de `confirmed` fondé seulement sur présence/heartbeat.
- **Pas** de `confirmed` fondé seulement sur du pane-scraping générique.
- **Pas** de stratégie API déduite implicitement de `transport=api` : elle doit être **déclarée**.
- **Pas** de changement de session/révision interprété comme un clear (voir §5, principe miroir).

---

## 3. Taxonomie de capacité (modèle convergé)

La règle produit centrale : **le bouton clear lit la capacité, jamais la présence.**

| Signal | Sens | Source |
|---|---|---|
| `registered` | instance connue du registry | `registry.py` |
| `available` | présence / `is_online` (heartbeat MCP récent) — **conservé tel quel** | `agents.py:23` |
| `busy` | `is_active` (écran changé < 8 s, `ACTIVITY_TIMEOUT`) | `agents.py:24`, `mcp_bridge.py:30,750` |
| `terminal_injectable` | le transport peut recevoir un payload terminal (tmux / console Windows) | **à ajouter** |
| `clear_supported` | clear réellement défini pour cette base/transport, avec preuve vérifiable | **à ajouter** |
| `clear_strategy` | `unsupported \| not_applicable \| app_state_reset \| cli_command \| session_restart \| …` | **à ajouter** |
| `clear_confirmation` | `none \| wrapper_machine \| session_generation \| heuristic_only` | **à ajouter** |
| `clear_state` | `{state, reason, requested_at, updated_at}`, state initial calculé depuis la stratégie | **à ajouter** |

**Conceptuellement** `present == available`, mais on **n'ajoute pas** de second champ status `present` (voir C1). `available` reste l'unique signal de présence ; docs et tests doivent affirmer explicitement qu'`available` ne doit **jamais** alimenter `clear_supported`.

---

## 4. Constats techniques vérifiés

- **UI → terminal existe déjà.** Une mention `@agent` écrit dans `data/<name>_queue.jsonl`, drainé par `_queue_watcher` (`wrapper.py:879-977`) qui injecte un prompt dans le TUI. Le drain **aplatit** tout le batch en un seul prompt (`wrapper.py:892-973`). Le prompt par défaut est littéralement « use mcp to read #channel… » (`wrapper.py:937`) — c'est exactement le mécanisme qui déclenche ces sessions. Mais le watcher injecte du texte **sans readback ni confirmation**.
- **terminal_injectable existe de fait, n'est pas modélisé.** Unix : `tmux send-keys` (`wrapper_unix.py`). Windows : `kernel32.WriteConsoleInputW` (`wrapper_windows.py:139,186`). Mais `Instance` (`registry.py:20-31`) ne porte aucun champ transport/wrapper-kind : le serveur ne sait pas si une instance est tmux / console / api.
- **Métriques pilotées par le wrapper.** `_usage_monitor` (`wrapper.py:689-720`) poll 5 s, lit le JSONL de session du CLI, poste `/api/usage_event` → `usage_snapshots[name]` → status UI (`app.py:1094-1106`). Dedup local via `last_sent` (`wrapper.py:717-719`). **Le serveur ne peut pas fabriquer un reset fiable** : soit le prochain poll est identique (`last_sent`) et fige le reset, soit il diffère et écrase le reset.
- **API = stateless.** `wrapper_api.py` (`276-347`) relit les N derniers messages chat et reconstruit le contexte à chaque trigger. Pas de CLI injectable, pas de contexte persistant.
- **Auth agent disponible.** `_resolve_authenticated_agent` (Bearer token wrapper) est déjà utilisé par `usage_event`/`deregister` ; seuls `register`/`deregister`/`heartbeat` sont pré-auth (`app.py:413`). Un `/api/clear_event` authentifié est cohérent avec ce pattern.
- **Le bug latent C3 (réordonne le plan) — confirmé filesystem :**
  - `_usage_monitor` reçoit `claude_session_id` évalué **une seule fois** à la création du thread : `_profile_claude_session_id(launch_args)` (`wrapper.py:1298`) → string **figée**.
  - `source_path` est **collant** : résolu une fois puis jamais reconsidéré (`wrapper.py:699-700`, `if source_path is None and claude_session_id`).
  - La recovery Claude mute `--session-id` **en place** dans les args (`wrapper_unix.py:70-76`), appelée sur thinking-block error / prompt interrompu (`wrapper_unix.py:223-225`).
  - **Donc** : dès qu'une recovery se déclenche, le monitor continue de tailer l'**ancien** fichier de session → usage stale-high pour toujours. C'est un bug **existant aujourd'hui**, indépendant de tout clear ; le clear-via-restart ne ferait qu'ajouter un 2ᵉ déclencheur au même bug.

---

## 5. Arbitrages (débats résolus)

### Objections
- **C1 — Pas de `present` redondant.** `available` (`is_online`) est déjà la présence ; les pills UI keyent sur `available/working/offline` (`static/chat.js:1877-1883`). Un alias `present` parallèle = drift. → garder `available`/`busy`, **ajouter seulement** `terminal_injectable`/`clear_supported`/`clear_state` (+ `clear_strategy`/`clear_confirmation`). La taxonomie conceptuelle reste (`present == available`), mais sans second champ.
- **C2 — `busy=false` n'est PAS une preuve de sûreté.** `busy` = « écran changé < 8 s » : faux aussi quand l'agent est bloqué sur un prompt de permission, un tool call long, ou en train de penser sans rendu. Pour une action terminale destructive, `busy` ne peut que **bloquer ou avertir**, jamais **autoriser silencieusement**. `busy=false` = « pas manifestement occupé », pas « safe ». → confirmation explicite utilisateur + état `requested/pending` ; le wrapper peut encore refuser si `busy=true` ou état local douteux.
- **C3 — `session_restart` casse le monitor d'usage sans repointage.** (Détail §4.) Élevé en **critère d'acceptation dur** et **découplé en PR0** (bugfix télémétrie autonome), car c'est un bug existant et pas seulement un prérequis du clear.
- **C4 — `/clear` injecté ne rebaseline pas de façon fiable.** Pour l'injection (vs restart) il n'y a **pas** de nouveau fichier de session ; le même JSONL continue. Que ses lignes d'usage post-`/clear` retombent vraiment est **non vérifié** (compaction vs reset, interne Claude Code). → `/clear` injecté plafonne à `pending`/`heuristic`, **jamais** `confirmed` par défaut. `session_restart` est le seul candidat défendable pour `confirmed` (preuve machine : nouvel id + nouveau fichier = usage bas), mais il est **destructif** → jamais de lancement silencieux.

### Questions
- **Q1 — File d'actions séparée.** La mention queue est drainée en batch et **aplatie** en un prompt (`wrapper.py:892-973`) : une action interleavée risque d'être avalée/réordonnée. → file d'actions dédiée `data/<name>_actions.jsonl`. **Exigence dure : une action clear ne doit JAMAIS passer par `inject_fn(prompt)`** ; dispatch sur le type **avant** l'aplatissement.
- **Q2 — Champ generation/session.** Champ **optionnel et opaque** (`generation`, `session_id`/`session_ref`) dans le contrat `clear_event`/`usage_event` **dès PR1** (forward-compatible, contrat stable) ; **logique complète en PR3**. Schéma commun optionnel, mais runtime générationnel **provider-specific** : Claude en a besoin tout de suite (`--session-id`) ; Codex a une corrélation rollout différente (`started_at`/fichier rollout) ; les API agents peuvent n'avoir aucune génération.
- **Q3 — API `not_applicable` : runtime + override.** Déclaré au runtime par `wrapper_api` + override config possible. **Contrainte dure : déclaration explicite, jamais déduite par le serveur de `transport=api`.**
- **Q4 — `session_restart` > `/clear` injecté pour `confirmed`.** `session_restart` réutilise `_refresh_claude_session_id` → confirmation **machine-vérifiable**. Coût : destructif (tue process + TUI + scrollback, peut détruire le travail en cours). → gated not-busy + confirmation explicite, jamais silencieux. `/clear` injecté = futur « soft clear » optionnel, plafond `pending`/`heuristic`.

### Principe miroir (génération ≠ clear)
Une nouvelle génération/révision de session **ne signifie jamais un clear** par elle-même : la recovery thinking-block crée déjà une nouvelle session **sans aucune intention de clear** (`wrapper_unix.py:223-225`). Donc `generation` est une **métadonnée d'autorité, pas une preuve suffisante**. `confirmed` exige **tout** : clear **demandé** (file d'actions) + stratégie déclarée + wrapper authentifié + standard de preuve par adaptateur. La génération **corrobore**, elle ne **déclenche** pas. C'est le miroir exact du bug latent : le même mécanisme prouve que changement-de-session ≠ clear.

---

## 6. Plan de livraison par PR

> Séquençage gaté : on peut **s'arrêter après n'importe quelle PR** sans avoir introduit de faux succès terminal.

### PR0 — Bugfix télémétrie recovery Claude (clear-agnostic) — Fait
**But :** corriger le bug latent C3, **mergeable seul** même si toute la feature clear est abandonnée.

**Périmètre strict — interdits :** aucun `clear_event`, aucun changement UI, aucun changement du schéma wire `usage_event`, aucun wording `clear/confirmed/pending`.

**Changements :**
- Nouvel état runtime **neutre** `ClaudeSessionState` (nom Claude-only assumé : le bug et le mécanisme sont liés à `--session-id` Claude ; on généralisera après preuve), avec `session_id` + `session_revision`, lock, `set_session_id()`.
- Initialisé dans `wrapper.py` au même endroit que le lancement du monitor / `run_agent` (`wrapper.py:1295`, `1321`) — même scope, l'état se passe aux deux.
- **Writers en PR0 : initialisation + recovery uniquement.** Le futur clear réutilisera le même setter plus tard (seam propre, sans refonte).
- **Reader :** `_usage_monitor` détecte `session_revision` changée → reset `source_path=None`, `last_sent=''`, `reported_unavailable=False` (les 3 locals exacts, `wrapper.py:691-705`) → re-résout le fichier du nouveau `session_id`.
- `wrapper_unix.run_agent` reçoit l'état et appelle `set_session_id()` lors de la recovery (`wrapper_unix.py:223`).
- `wrapper_windows.run_agent` garde la **parité de signature** : accept-and-ignore (pas de recovery `--session-id` côté Windows). Cf. AGENTS.md « keep terminal wrapper behavior in sync » — ne pas casser Windows par mismatch.

**Alternative écartée (tracée) :** relire `launch_args` live à chaque poll et re-dériver `_profile_claude_session_id`. Rejetée : trop implicite, même classe de fragilité (état partagé par mutation de liste en place). L'objet explicite est le bon niveau de robustesse.

**Test ciblé :** ancien fichier à usage élevé → recovery change le session id → nouveau fichier à usage bas ; le monitor **ne reposte jamais** l'ancien usage ; il peut devenir temporairement `unavailable` (« session jsonl not found », `wrapper.py:703-705`) plutôt que stale — **comportement désirable, à garder.**

**Definition of done :** la recovery Claude repointe correctement le monitor ; l'ancien usage n'est jamais reposté ; zéro sémantique clear introduite.

---

### PR1 — Fondation clear (couche de vérité, sans injection terminale) — Fait
**But :** rendre le serveur **incapable** de confondre présence et capacité. Haute valeur / faible risque.

**Changements code :**
- `registry.py` — étendre `Instance` (defaults conservateurs) : `transport` (`unknown|tmux|windows_console|api`), `terminal_injectable` (bool, défaut **false**), `clear_supported` (bool, défaut **false**), `clear_strategy`, `clear_confirmation`, `clear_state` (`{state, reason, requested_at, updated_at}`, state initial calculé depuis la stratégie).
- `app.py::register_agent` (`app.py:2447`) — accepter ces champs du wrapper, **valider les enums**, valeurs inconnues → defaults safe (pas de confiance aveugle).
- `wrapper.py::_register_instance` (`wrapper.py:726`) — déclarer `transport` (`tmux` hors Windows, `windows_console` sur Windows), `terminal_injectable=true`, **`clear_supported=false` au début**.
- `wrapper_api.py` — déclarer `transport=api`, `terminal_injectable=false`, `clear_strategy=not_applicable` avec reason `stateless_chat_replay`. **Pas** le default de tous les API agents futurs (Q3).
- `agents.py::get_status` / `app.py::_status_payload` — exposer `registered`, `available` (présence, conservé), `busy`, capabilities, `clear_state`, usage. **Pas** de champ `present` redondant (C1).
- **Contrat événement** — `clear_event`/`usage_event` incluent le champ **optionnel opaque** `generation`/`session_ref` dès maintenant (Q2) ; la logique vient en PR3.
- **Endpoint `POST /api/clear_event`** — authentifié agent via `_resolve_authenticated_agent` (Bearer wrapper, **sa propre instance uniquement**). Payload `{state, strategy, confirmation, reason, generation?}`. États acceptés : `not_applicable | pending | confirmed | failed | not_supported`. **Règle serveur : le serveur relaie et persiste, il ne fabrique jamais `confirmed`.**
- **Métriques honnêtes** — ne pas simplement `pop usage_snapshots` côté serveur. Le **wrapper** émet l'autorité usage après clear : `status=unavailable`, reason `'cleared; awaiting fresh telemetry'`, avec generation/session si dispo. Le wrapper re-baseline (reset `last_sent`, invalide `source_path`, ne republie pas l'ancien usage) — **s'appuie sur le mécanisme de PR0.**
- `static/chat.js` — garder `available` pour l'existant ; **toute UI clear lit depuis `clear_state`/capabilities, jamais `available`**. Le bouton principal peut continuer à vider le chat, mais le libellé/tooltip indique **« chat clear »**, aucune coche terminale.

**Tests :** register sans capabilities → defaults safe ; wrapper CLI déclare injectable mais clear non supporté ; wrapper API déclare `not_applicable` explicitement ; status payload contient les champs ; UI/status ne mappe jamais `available=true → clear_supported=true` ; `/api/clear_event` refuse non-auth et token stale ; un agent ne peut pas muter le `clear_state` d'un autre ; `confirmed` seulement quand le wrapper authentifié le poste ; usage cleared visible en `unavailable` avec reason claire ; un ancien usage ne clobber pas l'état cleared si une nouvelle génération est déclarée.

**Definition of done :** l'UI affiche qui est présent et qui supporte le clear, et un wrapper peut dire « mon état est cleared/not_applicable/failed » sans mensonge — **aucun terminal clear n'est tenté.**

---

### PR2 — Workflow UI honnête (toujours sans promesse globale) — Fait
**But :** rendre l'action utilisateur explicite.

**Changements :**
- Remplacer le confirm actuel `Clear Chat?` (`static/chat.js:1996/2019/2026/2045`, event `clear` traité à `chat.js:584`) par une confirmation qui distingue : **`Clear chat only`** toujours disponible + liste des agents (`not_applicable`, `not_supported`, `pending`, `supported`).
- Si **aucun** agent `clear_supported`, le bouton n'essaie rien de terminal.
- Si un agent est `busy`, bloquer ou demander confirmation explicite → `requested/pending`, **jamais** d'injection silencieuse (C2).
- Affichage de résultat **par agent**, pas un toast global.

**Tests :** l'UI ne propose pas de terminal clear pour un agent non supporté ; un agent API `not_applicable` n'est pas marqué `failed` ; un agent `busy` ne reçoit pas de demande terminale sans garde.

**Definition of done :** Théo peut utiliser `/clear` sans croire que les terminaux ont été vidés quand ce n'est pas le cas.

---

### PR3 — Adaptateur Claude pilote (restart) — En cours
**But :** tester **une seule** stratégie terminale avant toute généralisation. **Prérequis : PR0 mergé** (monitor générationnel en place).

**Note A1 :** reportée à PR3 pour rester couplée à l'adaptateur Claude pilote et à la preuve live de `session_restart`.

**Statut branche PR3 :** l'adaptateur `session_restart`, la file d'actions séparée, l'endpoint de demande explicite et les tests mock sont implémentés. `clear_supported=true` reste commenté dans `config.toml` tant que le test live Claude répétable n'a pas été capturé.

**Politique `--no-restart` :** le clear explicite redémarre quand même (l'utilisateur a confirmé l'action). La recovery Claude automatique respecte `--no-restart` : elle ne tue pas la session, n'injecte pas le trigger dans un état connu comme cassé, et signale au chat que le trigger a été ignoré et doit être renvoyé après recovery manuelle.

**Changements :**
- File d'actions séparée `data/<name>_actions.jsonl` (Q1) — **une action clear ne passe jamais par `inject_fn(prompt)`.**
- Le wrapper lit l'action `clear_context`.
- Stratégie `session_restart` : nouvelle session id / restart contrôlé, réutilisant le pattern `_refresh_claude_session_id` (`wrapper_unix.py:70`) — **conçu comme action explicite**, pas du recovery recyclé en douce.
- Garde : Claude idle selon C2 + **confirmation explicite utilisateur** ; le wrapper peut **refuser** (`busy=true` ou état local douteux).
- Le wrapper émet `pending`, puis `confirmed` **seulement** après preuve définie : nouvelle session active + source usage réinitialisée / fichier de session distinct (via le `set_session_id()` partagé de PR0). La génération **corrobore**, elle ne déclenche jamais seule (§5).
- Si preuve absente : rester `pending` ou `failed`, **jamais** `confirmed`.

**Tests :** unit sur le parser de la file d'actions ; test wrapper **sans lancer Claude réel** (action clear → stratégie mock → `clear_event` attendu) ; **test live manuel Claude obligatoire** avant de passer `clear_supported=true` dans la config/default.

**Definition of done :** Claude marqué `clear_supported=true` **seulement** après test live répétable et preuve documentée.

---

### PR N — Autres agents un par un (Codex / Gemini / Qwen / …)
**But :** ne **jamais** généraliser par analogie.

Pour chaque base : (1) documenter la commande/stratégie ; (2) définir le standard de preuve ; (3) adaptateur isolé ; (4) test mock + test live ; (5) seulement ensuite `clear_supported=true`.

Si la preuve est seulement visuelle/heuristique : **état max = `pending`/`heuristic`, jamais `confirmed`.**

---

## 7. Critères d'acceptation globaux

- Aucun code ne dérive `clear_supported` de `available`/présence.
- Tout `confirmed` vient d'un **wrapper authentifié** + **stratégie déclarée** + **standard de preuve par adaptateur**.
- Les API agents ne sont jamais traités comme des terminaux.
- Les métriques ne montrent jamais un usage ancien comme frais après clear/rebaseline.
- Le clear chat/store reste disponible même si aucun clear terminal n'est supporté.
- Les tests couvrent les defaults safe et les cas auth / token stale / cross-agent.
- **(PR0)** Après un changement de session en recovery, le monitor repointe et l'ancien usage n'est jamais reposté.

---

## 8. Risques résiduels

- **Pane-scraping = jamais une preuve parfaite** : toute détection par regex/capture est heuristique sauf confirmation machine. Aucun CLI n'émet de signal machine « contexte vidé ».
- **`busy` est une approximation d'activité**, pas une preuve de sûreté (C2).
- **`session_restart` est destructif** : tue la session tmux où Théo peut être attaché, perd le scrollback/TUI, peut détruire le travail en cours. Coût produit à assumer.
- **Rebaseline du `/clear` injecté incertain** : que le JSONL Claude retombe vraiment post-`/clear` n'est pas vérifié (C4) — raison de plus de préférer `session_restart` pour `confirmed`.
- **Codex/Gemini/Qwen non testés** : `clear_supported=false` tant que non validé live par adaptateur.

---

## 9. Note de permission (R2)

Le repo `1. PROJETS/DEV/agentchattr` est sous `1. PROJETS/` → **R2 workspace, fermé par défaut** (`hot.md` : « Périmètre éditable actif : aucun »). **Ce document est un plan, pas un patch.** Aucune modification de code tant que Théo n'ouvre pas explicitement R2 pour la session.

**Préflight Phase 0 (quand R2 sera ouvert), cf. AGENTS.md §Workflow :**
1. `git fetch --prune --all` puis `git switch -c <short-topic-branch> theo/main`.
2. `git status -sb` avant toute édition (worktree partagé, ne pas écraser les changements de Théo).
3. Lancer les tests baseline : `python -m pytest tests/test_usage_telemetry.py tests/test_model_commands.py tests/test_unicode_mentions.py` (vérifié : ces 3 fichiers existent).
4. Si baseline échoue, consigner **avant** modification.

**PR0 est prêt à coder dès l'ouverture de R2.**
