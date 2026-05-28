# Update Model Profiles

Procedure pour changer les modeles, profils ou efforts par defaut de Claude
Code et Codex dans AgentChattr.

Ce fichier vit a la racine du repo AgentChattr parce que `docs/` est ignore
par `.gitignore`.

## Principe Important

`config.toml` ne suffit pas toujours.

Le wrapper choisit un profil dans cet ordre :

1. `data_dir/settings.json` -> `agent_profiles.<agent>` si present et valide.
2. Sinon le profil marque `default = true` dans `config.toml`.
3. Sinon le premier profil de la liste.

Donc un ancien `settings.json` peut masquer le nouveau default meme apres un
restart complet.

Pour Claude fast mode, le wrapper choisit l'etat dans cet ordre :

1. `data_dir/settings.json` -> `agent_fast_modes.claude` si present.
2. Sinon `fast_mode_default = true/false` dans `[agents.claude]`.
3. Sinon `false`.

La commande AgentChattr `/fast` modifie `agent_fast_modes.claude`.

Pour l'installation Context OS lancee via `agentchattr`, le data dir vivant est :

```text
/Users/theocs/Library/Application Support/AgentChattr/context-os/data
```

Le repo local `./data` peut etre vide et ne pas refleter l'instance reelle.

## Preflight Git

Avant de modifier les profils, verifier que l'on travaille dans le bon fork et
sur la bonne branche :

```bash
git status -sb
git remote -v
git branch --show-current
git rev-parse --abbrev-ref --symbolic-full-name @{u}
```

Pour le fork Context OS de Theo, l'etat attendu au 2026-05-28 est :

```text
branch locale: theo/context-os-agentchattr
upstream: theo/theo/context-os-agentchattr
remote theo: https://github.com/theocs-dev/agentchattr.git
remote origin: https://github.com/bcurts/agentchattr.git (push desactive)
```

Ne pas supposer que l'on est sur `main`. Si l'objectif est de modifier `main` du
fork, le faire explicitement avant les edits et le noter dans le compte rendu.

## Fichiers A Mettre A Jour

### 1. Profils source

Modifier [config.toml](</Users/theocs/IPCRA/1. PROJETS/DEV/agentchattr/config.toml>).

Pour Claude :

```toml
[agents.claude]
fast_mode_default = true

[agents.claude.profiles.max]
label = "Opus 4.7 Max"
model = "opus"
reasoning = "max"
args = ["--permission-mode", "auto"]
default = true
```

Fast mode est un setting Claude Code. D'apres la doc Anthropic, il peut etre
active avec `/fast` ou `"fastMode": true`, fonctionne sur Opus 4.7/4.6, garde
la qualite/capacite Opus et vise une latence plus basse avec un cout token plus
eleve. Pre-requis critique : les usage credits doivent etre actives. Si Claude
affiche `Fast mode requires usage credits`, le flag est bien passe au process,
mais fast mode n'est pas encore actif et il ne repondra pas plus vite.

Ne pas cacher `fastMode` dans `profiles.max.args` si l'UI doit pouvoir
l'activer/desactiver. Le wrapper ajoute `--settings {"fastMode":true/false}` a
partir de `agent_fast_modes.claude`.

Pour Codex :

```toml
[agents.codex.profiles.deep]
label = "Extra High"
model = "gpt-5.5"
reasoning = "xhigh"
default = true
```

`Extra High` est un label AgentChattr. La valeur transmise a Codex est
`xhigh`.

Enlever `default = true` des anciens profils.

Ne pas essayer de remplacer `claude` ou `codex` via `config.local.toml` :
[config_loader.py](</Users/theocs/IPCRA/1. PROJETS/DEV/agentchattr/config_loader.py>)
ajoute seulement les agents locaux qui n'existent pas deja dans `config.toml`.
Les agents integres restent donc definis par `config.toml`.

### 2. UI et commandes

Verifier les surfaces suivantes :

- [app.py](</Users/theocs/IPCRA/1. PROJETS/DEV/agentchattr/app.py>) :
  message d'usage `/model`, resolution des aliases (`xhigh` -> profil `deep`),
  commande `/fast`, payload UI `fast_mode`.
- [static/chat.js](</Users/theocs/IPCRA/1. PROJETS/DEV/agentchattr/static/chat.js>) :
  menu slash command, affichage pill `Fast`/`Standard`, checkbox settings.
- [static/index.html](</Users/theocs/IPCRA/1. PROJETS/DEV/agentchattr/static/index.html>) :
  checkbox settings Claude fast, bump du cache-buster `chat.js?v=N`, sinon le
  navigateur peut garder l'ancien texte JS.

### 3. Tests

Ajouter ou adapter les tests suivants :

- [tests/test_usage_telemetry.py](</Users/theocs/IPCRA/1. PROJETS/DEV/agentchattr/tests/test_usage_telemetry.py>) :
  verifier les args de lancement construits depuis `config.toml`, dont
  `fastMode:true` par defaut et `fastMode:false` quand le settings le force.
- [tests/test_model_commands.py](</Users/theocs/IPCRA/1. PROJETS/DEV/agentchattr/tests/test_model_commands.py>) :
  verifier `/model claude max`, `@codex /model xhigh` et `/fast on/off`.

Commande :

```bash
.venv/bin/python -m unittest discover -s tests
```

## Mettre A Jour Le Settings Vivant

Si une instance AgentChattr a deja ete utilisee, verifier :

```bash
sed -n '1,160p' "$HOME/Library/Application Support/AgentChattr/context-os/data/settings.json"
```

Chercher :

```json
"agent_profiles": {
  "claude": "xhigh",
  "codex": "balanced"
}
```

Corriger vers :

```json
"agent_profiles": {
  "claude": "max",
  "codex": "deep"
},
"agent_fast_modes": {
  "claude": true
}
```

Commande macOS :

```bash
plutil -replace agent_profiles.claude -string max "$HOME/Library/Application Support/AgentChattr/context-os/data/settings.json"
plutil -replace agent_profiles.codex -string deep "$HOME/Library/Application Support/AgentChattr/context-os/data/settings.json"
plutil -replace agent_fast_modes.claude -bool YES "$HOME/Library/Application Support/AgentChattr/context-os/data/settings.json"
```

Alternative depuis l'UI/chat :

```text
/model claude max
/model codex xhigh
/fast on
```

ou :

```text
@codex /model xhigh
```

Attention : les commandes `/model` et `/fast` sauvegardent les choix, mais les
wrappers deja lances doivent etre redemarres pour que les flags terminal
changent.

## Redemarrage

Il y a deux couches a redemarrer :

- le serveur/LaunchAgent, pour recharger `config.toml`, `app.py` et les assets
  statiques ;
- les wrappers/tmux Claude et Codex, pour relancer les CLIs avec les nouveaux
  flags.

Pour appliquer un changement code + profils de bout en bout, utiliser le hard
restart complet :

```bash
agentchattr restart all --cwd /Users/theocs/IPCRA
```

Alias legacy equivalent :

```bash
agentchattr kill all --cwd /Users/theocs/IPCRA
```

Pour redemarrer seulement les wrappers, sans redemarrer le serveur :

```bash
agentchattr restart agents --cwd /Users/theocs/IPCRA
```

Ou separement :

```bash
agentchattr restart claude --cwd /Users/theocs/IPCRA
agentchattr restart codex --cwd /Users/theocs/IPCRA
```

`agentchattr restart claude|codex|agents` ouvre les wrappers dans de nouvelles
fenetres Terminal.app. Pour lancer un agent dans le terminal courant, utiliser :

```bash
agentchattr launch claude --cwd /Users/theocs/IPCRA
```

`agentchattr restart server` redemarre le serveur/LaunchAgent. Il ne tue pas les
wrappers deja ouverts et n'ouvre pas le navigateur par defaut. Utiliser
`agentchattr restart server --open` ou `agentchattr open` si l'UI doit etre
ouverte. `agentchattr restart` sans cible reste un alias legacy de
`agentchattr restart server`. `agentchattr restart --agents` reste supporte pour
lancer Claude/Codex apres le redemarrage serveur, mais le chemin recommande est
`agentchattr restart all`.

## Verification

### 1. Process reels

```bash
ps -axww | rg 'wrapper\.py (claude|codex)|model_reasoning_effort|/Users/theocs/.local/bin/claude --mcp-config'
```

Attendu :

```text
claude ... --model opus --effort max ...
codex ... --model gpt-5.5 -c model_reasoning_effort="xhigh"
```

### 2. Tmux

```bash
tmux list-sessions
tmux capture-pane -t agentchattr-claude -p -S -30
tmux capture-pane -t agentchattr-codex -p -S -30
```

Selon les restarts, les sessions peuvent s'appeler `agentchattr-claude-2` ou
`agentchattr-codex-2`.

Attendu dans Claude :

```text
Opus 4.7 (1M context) with max effort
```

Attendu dans Codex :

```text
gpt-5.5 xhigh
```

Codex peut afficher temporairement `default` pendant le chargement. Attendre la
fin du startup MCP avant de conclure.

### 3. UI

Avec les wrappers enregistres, les pills et settings doivent montrer :

```text
Claude · Opus 4.7 Max · Fast
Codex · Extra High
```

Si `/fast off` a ete utilise, la pill Claude doit montrer `Standard` et la
checkbox `Claude speed / Fast` doit etre decochee.

`/model` doit lister :

```text
claude fast mode: ON
* claude max: ...
* codex deep: Extra High ...
```

`@codex /model` doit lister seulement les profils Codex.

## Verifier Les Defaults Des CLIs

Verifier les CLIs installees au moment de l'update :

```bash
claude --help | rg -- '--effort'
codex --help | rg -- '--config'
```

Pour Codex, verifier aussi la page modeles OpenAI si le modele change :
<https://developers.openai.com/api/docs/models>. Au 2026-05-28, `gpt-5.5`
liste `none low medium high xhigh` comme niveaux de reasoning.

Ces fichiers peuvent exister :

```text
~/.claude/settings.json
~/.codex/config.toml
```

Ils ne doivent pas etre confondus avec AgentChattr :

- Claude hors AgentChattr peut garder `effortLevel = "xhigh"`.
- Codex hors AgentChattr peut avoir son propre `model_reasoning_effort`.
- Dans AgentChattr, les flags du wrapper doivent gagner :
  - Claude : `--effort max`
  - Codex : `-c model_reasoning_effort="xhigh"`

Toujours verifier les process reels apres restart.

Pour Claude fast mode, verifier aussi que le process contient :

```text
--settings {"fastMode":true}
```

Si `/fast off` a ete utilise, verifier au contraire :

```text
--settings {"fastMode":false}
```

Puis verifier l'etat dans Claude Code avec `/fast` ou l'icone `↯`. Le header
Claude peut continuer a afficher seulement `Opus 4.7 ... with max effort`; il ne
suffit donc pas a prouver que fast mode est actif. Si `/fast` affiche une demande
d'activation des usage credits, il manque encore le pre-requis billing/org.

## Angles Morts Connus

- `settings.json` vivant masque les nouveaux defaults de `config.toml`.
- Le data dir du launcher Context OS n'est pas `./data`.
- `agentchattr restart server` ne remplace pas les wrappers/tmux deja ouverts.
- Le navigateur peut garder un ancien `chat.js` sans bump `v=N`.
- Les wrappers existants gardent leurs flags jusqu'au hard restart.
- `agentchattr kill ...` est un alias legacy ; documenter `restart ...` comme
  chemin principal.
- `agentchattr launch claude|codex` lance dans le terminal courant et bloque
  jusqu'a detachement/arret du wrapper.
- Le select profil UI est visible seulement quand un wrapper est enregistre.
- `config.local.toml` ne peut pas override `claude`/`codex`.
- Claude fast mode est plus cher et depend des usage credits / reglages org ; le
  flag `--settings {"fastMode":true}` ne suffit pas si les credits sont inactifs.
- Le label de profil ne doit pas etre la seule source UI du fast mode, sinon
  `/fast off` laisse une UI mensongere.
- `@claude, @codex /model ...` traite actuellement le premier agent mentionne.
