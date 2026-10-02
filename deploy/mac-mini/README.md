# Déploiement Mac mini

Lancer `scripts/deploy_mac_mini.sh` depuis ce dépôt. Le script synchronise les sources vers `~/weather-desk`, vérifie que le moteur Docker OrbStack est actif, construit l'image ARM native depuis le lockfile `uv`, puis démarre Panel dans un conteneur Compose. Les données SQLite et le cache utilisent un volume persistant nommé. Le port 5006 est publié uniquement sur `127.0.0.1`; l'application tourne sans privilèges et son système de fichiers racine est en lecture seule.

Le tunnel Cloudflare reste un agent `launchd` natif sur macOS. Il transmet vers `127.0.0.1:5006`; ainsi aucun port du conteneur n'est directement exposé au LAN ou à Internet. Le fichier de configuration et les identifiants Panel restent sous `~/.config/weather-desk`, accessibles au démarrage au conteneur et jamais intégrés à l'image.

Le tunnel a sa propre configuration dans `~/.cloudflared/weather-desk.yml`; il ne modifie pas les routes des tunnels existants. Après le premier déploiement, créer dans la zone DNS Cloudflare `galerne-routing.com` un CNAME proxifié nommé `weather-desk`, pointant vers `<UUID-du-tunnel>.cfargotunnel.com` (l'UUID est affiché par le script). Le certificat `cloudflared` déjà installé sur le Mac mini est rattaché à une autre zone et ne peut pas créer cet enregistrement dans `galerne-routing.com`.

Le tunnel route `/mcp` vers le conteneur MCP sur le port loopback 8765. Le serveur exige un bearer token dédié, stocké dans `~/.config/weather-desk/mcp-token`; ne le transmettre qu'au client MCP à configurer. Dans Cloudflare Zero Trust, créer une application Access auto-hébergée pour `weather-desk.galerne-routing.com/mcp*`, puis la restreindre aux clients attendus avec une règle service token. Configurer les en-têtes Cloudflare Access du client et `Authorization: Bearer <mcp-token>` : les deux contrôles doivent réussir. La connexion Panel ne protège pas cette route.

L'assistant par prompt appelle côté serveur un endpoint OpenAI-compatible Chat Completions. Configurer `WEATHER_DESK_LLM_BASE_URL`, `WEATHER_DESK_LLM_MODEL` et, si nécessaire, `WEATHER_DESK_LLM_API_KEY` dans `~/.config/weather-desk/llm.env` (droits 600). Pour un modèle Ollama sur macOS, utiliser `http://host.docker.internal:11434/v1`, son nom exact, et `WEATHER_DESK_LLM_REASONING_EFFORT=none` pour désactiver la réflexion prolongée si le modèle le permet. L’API Ollama doit être accessible au conteneur OrbStack ; ne pas publier son port vers Internet. Un prompt produit uniquement une proposition validée; l'interface affiche les commandes et la révision visée, puis n'applique qu'après clic sur **Confirmer et appliquer**. Une modification concurrente invalide la proposition.

Le compte `maurin.loic.ac@gmail.com` et son mot de passe aléatoire sont conservés dans `~/.config/weather-desk/access.txt` avec les permissions `600`. Le cookie secret et le fichier JSON des utilisateurs sont dans le même répertoire, également protégés. Le déploiement réaffiche les identifiants existants sans les régénérer.

```sh
ssh macmini 'cat ~/.config/weather-desk/access.txt'
ssh macmini 'cat ~/.config/weather-desk/mcp-token'
ssh macmini 'cd ~/weather-desk && docker compose --env-file ~/.config/weather-desk/compose.env -f deploy/mac-mini/compose.yml ps'
ssh macmini 'cd ~/weather-desk && docker compose --env-file ~/.config/weather-desk/compose.env -f deploy/mac-mini/compose.yml logs -f weather-desk'
ssh macmini 'tail -f ~/Library/Logs/weather-desk-tunnel.log'
```

The OrbStack app must launch in the Mac mini's logged-in user session. Enable its macOS Login Item and verify that the container returns to `healthy` after an OrbStack restart and a full Mac reboot. The Cloudflare connector remains separately supervised by `launchd`.

## Exploitation

- Keep macOS, OrbStack, and the container image current, but deploy image updates deliberately: run tests, build the new image, inspect health, and retain the previous Git revision for rollback. Do not use an unpinned `latest` application image.
- Back up the `weather-desk-data` volume (SQLite workspace and provider cache) and `~/.config/weather-desk` (authentication and tunnel-related settings) to storage outside the Mac mini. OrbStack can export the named volume with `orb docker volume export weather-desk-data weather-desk-data.tar.zst`. The cache can be rebuilt; validate that a workspace backup can be restored.
- Monitor the public `/liveness` endpoint from outside the Mac mini and alert on sustained failure; `restart: unless-stopped` only restarts the process and does not notify an operator.
- Use a UPS and schedule macOS/OrbStack restarts with a person able to unlock the Mac. OrbStack is a per-user macOS app: a container restart policy cannot bring the Docker engine back if OrbStack itself is not running.
- Keep the Docker API socket and port 5006 off the LAN. The Compose port binding is loopback-only, and the Cloudflare tunnel is the sole inbound path.
