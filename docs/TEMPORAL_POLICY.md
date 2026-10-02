# Politique temporelle de Weather Desk

État d’implémentation du lot #16 : la référence partagée déclenche la sélection des sources IFS et satellite; chaque couche conserve et exporte sa validité effective.

## Résolution

Toutes les heures sont interprétées avec leur fuseau, puis normalisées en UTC.

- **IFS** : partir des quatre derniers cycles de six heures renvoyés par le fournisseur; considérer les validités aux pas de trois heures, de 0 à 72 h. Choisir la validité la plus proche dans un écart maximal de 90 minutes. En cas d’égalité, préférer la validité future, puis le run le plus récent. La requête GRIB confirme ensuite la disponibilité effective; un échec est présenté comme indisponibilité, jamais comme une validité réussie.
- **Satellite** : choisir l’image la plus proche parmi les heures publiées par le catalogue EUMETSAT, à 30 minutes maximum. En cas d’égalité, préférer l’image antérieure. Une heure absente ou hors tolérance reste signalée comme indisponible.

Le formulaire et le workspace MCP appellent le même résolveur IFS. Un changement de référence par l’interface est persisté dans le workspace; une modification MCP est reprise par le poller de l’interface.

## Chargement et export

Pendant une requête, la dernière couche reçue peut rester affichée; sa validité réelle reste visible. Chaque nouvelle requête remplace la précédente et son résultat ne peut pas s’appliquer après une sélection plus récente.

Les exports JSON, GeoJSON, Markdown, ZIP et PNG sont refusés pendant un chargement ou si le modèle demandé n’est pas celui qui est affiché. Le manifeste distingue l’heure de référence demandée de la validité et du run réellement servis par chaque source. La date d’émission de l’export reste séparée.

Un refus explicite est utilisé si une source ne répond pas ou si son heure est absente. L’export volontairement partiel et son étiquetage restent à concevoir; ils ne sont jamais implicites.

## Vérification restante

La sélection temps/source et les gardes d’export ont des tests unitaires, y compris cadences différentes, fuseaux, changement de jour, absence, ancienneté, réponse IFS désordonnée et changement MCP/UI. Une recette d’export complet pendant un chargement sur l’image de déploiement reste nécessaire avant de fermer #16.
