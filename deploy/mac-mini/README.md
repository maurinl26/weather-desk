# Déploiement Mac mini

Lancer `scripts/deploy_mac_mini.sh` depuis ce dépôt. Le script synchronise les sources vers `~/weather-desk`, installe `uv` si besoin, synchronise les dépendances verrouillées, puis crée deux agents `launchd` indépendants : Panel et un tunnel Cloudflare dédié. Panel écoute uniquement sur `127.0.0.1:5006` et le sous-domaine utilise une authentification Basic intégrée à Panel.

Le tunnel a sa propre configuration dans `~/.cloudflared/weather-desk.yml`; il ne modifie pas les routes des tunnels existants. Après le premier déploiement, créer dans la zone DNS Cloudflare `galerne-routing.com` un CNAME proxifié nommé `weather-desk`, pointant vers `<UUID-du-tunnel>.cfargotunnel.com` (l'UUID est affiché par le script). Le certificat `cloudflared` déjà installé sur le Mac mini est rattaché à une autre zone et ne peut pas créer cet enregistrement dans `galerne-routing.com`.

Le compte `maurin.loic.ac@gmail.com` et son mot de passe aléatoire sont conservés dans `~/.config/weather-desk/access.txt` avec les permissions `600`. Le cookie secret et le fichier JSON des utilisateurs sont dans le même répertoire, également protégés. Le déploiement réaffiche les identifiants existants sans les régénérer.

```sh
ssh macmini 'cat ~/.config/weather-desk/access.txt'
ssh macmini 'launchctl kickstart -k gui/$(id -u)/pro.galerne.weather-desk'
ssh macmini 'tail -f ~/Library/Logs/weather-desk.log'
```
