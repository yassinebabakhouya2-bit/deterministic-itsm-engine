# KB0010001 - Outlook ne démarre plus

## Symptôme
Outlook reste bloqué sur l'écran « Chargement du profil ».

## Cause
Un complément COM défectueux ou un fichier .ost corrompu.

## Résolution
1. Fermez Outlook.
2. Lancez Outlook en mode sans échec avec la commande `outlook.exe /safe`.
3. Si Outlook démarre en mode sans échec, allez dans Fichier > Options > Compléments et désactivez les compléments COM.
4. Si le problème persiste, supprimez le fichier .ost dans %LOCALAPPDATA%\Microsoft\Outlook
   puis relancez Outlook.

Pour toute question, contactez le support au 05 22 12 34 56.
