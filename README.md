# Weather Desk

Poste personnel d'analyse météo inspiré de Synergie/SYNOPSIS : juxtaposer une image satellite, une analyse modèle, annoter une situation et produire un bulletin Markdown traçable.

## Démarrage

```bash
cd ~/weather-desk
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

Le MVP fonctionne sans clé API : les images peuvent être chargées depuis le disque ou via URL. Le branchement EUMETSAT/EUMETView et les sorties IFS seront ajoutés derrière ces mêmes entrées.

## Flux actuel

1. renseigner la zone et l'échéance ;
2. charger une image vapeur d'eau et une analyse IFS/AROME ;
3. dessiner un front ou une zone sur la carte ;
4. renseigner l'hypothèse, la confiance et les impacts ;
5. télécharger le bulletin Markdown.

Les annotations restent dans le bulletin téléchargé. La prochaine étape sera d'écrire automatiquement un manifest JSON et de publier les artefacts dans wxr-shared.

