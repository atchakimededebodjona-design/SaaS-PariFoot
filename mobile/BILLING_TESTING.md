# Checklist QA manuelle — Google Play Billing (Phase 3)

Portée : `mobile/` (bridge Capacitor Java) + `frontend-design/vip.html` +
`frontend-design/google-play-billing.js` + backend (`POST /billing/google/verify`,
`GET /billing/entitlement`, déjà couverts par des tests automatisés —
voir `api/test_google_play_billing.py`).

Aucun test automatisé côté Android/JS cette phase (décision explicite —
aucun framework de test JS introduit). Cette checklist se coche à la main,
une fois un build de test installable disponible (piste de test interne
Play Console ou APK debug installé manuellement sur un appareil/émulateur —
aucune des deux n'a été mise en place cette phase).

Pré-requis avant de pouvoir cocher quoi que ce soit ici :
- Un produit `xfoot_premium` avec les base plans `monthly`/`yearly` doit
  exister dans une vraie Play Console (hors périmètre Phase 3).
- Un compte de service Google configuré côté backend (hors périmètre Phase 3).
- Un appareil/émulateur avec un compte Google de test (licence de test Play
  Console recommandée, pour ne pas être facturé réellement).

## 1. Web → Chariow inchangé
- [ ] Ouvrir `vip.html` dans un navigateur classique (pas l'app) : le bloc
      `#vip-plans-web` s'affiche, `#vip-plans-android` reste masqué.
- [ ] Le bouton "Débloquer l'accès VIP" mène toujours à `billing.html`.
- [ ] `billing.html` fonctionne exactement comme avant (checkout Chariow,
      activation par clé de licence) — aucun changement attendu.

## 2. Android → Google Play
- [ ] Ouvrir l'app Android : `#vip-plans-android` s'affiche,
      `#vip-plans-web` reste masqué.
- [ ] Aucune trace de Chariow visible nulle part dans l'app (pas de bouton,
      pas de lien, pas de WebView de paiement).

## 3. Utilisateur non connecté
- [ ] Depuis l'app Android sans session active, tenter un achat : redirigé
      vers `login.html`, aucun appel `launchBillingFlow` déclenché.

## 4. Produits affichés
- [ ] Les deux cartes (Mensuel/Annuel) affichent un prix — jamais un champ
      vide ni "…" bloqué indéfiniment.
- [ ] Les prix affichés correspondent à ceux configurés dans Play Console
      (formattedPrice de Google, jamais 1000/10000 FCFA codés en dur).

## 5. Monthly
- [ ] Bouton "Choisir" sur Mensuel lance bien le flux d'achat Google Play
      pour le base plan `monthly`.

## 6. Yearly
- [ ] Bouton "Choisir" sur Annuel lance bien le flux d'achat Google Play
      pour le base plan `yearly`.

## 7. Achat
- [ ] Un achat réussi (compte de test) ferme le flux Google Play et déclenche
      l'appel `POST /billing/google/verify` côté app (vérifiable via les logs
      serveur ou logcat).

## 8. purchaseToken
- [ ] Le `purchaseToken` reçu du plugin natif est bien celui envoyé au
      backend (comparer logcat ↔ logs serveur).

## 9. Verify
- [ ] La réponse de `/billing/google/verify` est bien reçue et traitée
      (message "Abonnement VIP activé" affiché, pas de blocage silencieux).

## 10. Premium
- [ ] Après un achat validé, `GET /billing/entitlement` (rafraîchi
      automatiquement) affiche `premium: true` et la carte de statut VIP
      passe à l'état actif.

## 11. Restauration
- [ ] Le bouton "Restaurer mes achats" fonctionne pour un compte ayant déjà
      un abonnement Google Play actif mais pas encore rattaché côté app
      (ex. après une réinstallation, voir #17).
- [ ] La restauration silencieuse à l'ouverture de l'écran VIP fonctionne
      sans action de l'utilisateur.

## 12. Achat pending
- [ ] Simuler un moyen de paiement différé (carte de test Google
      appropriée) : le message "Paiement en attente de confirmation."
      s'affiche, **aucun appel** `/billing/google/verify` n'est fait tant
      que l'achat reste PENDING (vérifiable via l'absence d'appel dans les
      logs serveur).
- [ ] Une fois la confirmation Google reçue (peut nécessiter une nouvelle
      ouverture de l'app ou un `restorePurchases()`), le Premium s'active.

## 13. Annulation
- [ ] Fermer le flux d'achat Google Play sans payer (bouton retour) :
      aucune erreur affichée à l'utilisateur, retour silencieux à l'écran VIP.

## 14. Erreur réseau
- [ ] Couper la connexion réseau de l'appareil juste après un achat Google
      réussi (avant l'appel `/verify`) : un message d'erreur clair s'affiche,
      l'achat reste visible dans Google Play (récupérable par une
      restauration ultérieure une fois le réseau rétabli).

## 15. Erreur Google
- [ ] Déclencher un code d'erreur Billing Library (ex. `ITEM_ALREADY_OWNED`
      en retentant un achat déjà possédé) : message d'erreur affiché, pas de
      plantage de l'app.

## 16. Token appartenant à un autre compte
- [ ] Acheter avec le Compte Google A connecté au Compte Xfoot 1.
- [ ] Se déconnecter, se reconnecter au Compte Xfoot 2 (même appareil, même
      Compte Google A) et tenter une restauration : le message d'erreur 409
      ("déjà lié à un autre compte") s'affiche, **aucune bascule
      automatique** du Premium vers le Compte Xfoot 2.

## 17. Réinstallation
- [ ] Désinstaller puis réinstaller l'app (même compte Google, même compte
      Xfoot) : à l'ouverture de l'écran VIP, la restauration silencieuse
      retrouve l'abonnement actif sans nouvel achat.

## 18. Plusieurs appareils
- [ ] Se connecter au même compte Xfoot sur un second appareil/émulateur
      (compte Google différent ou identique) : `GET /billing/entitlement`
      reflète le même statut Premium sur les deux appareils sans action
      supplémentaire (l'entitlement est côté serveur, par compte Xfoot, pas
      par appareil).

## 19. Changement de plan
- [ ] Depuis Mensuel actif, s'abonner à Annuel (upsell Google Play standard,
      ou nouvel achat si aucun chemin d'upsell direct n'est exposé côté UI
      cette phase) : le nouvel achat est vérifié, `plan` passe à `yearly`
      côté `GET /billing/entitlement`.

## 20. Renouvellement
- [ ] (Test long ou carte de test à renouvellement accéléré Play Console)
      Un renouvellement automatique doit être détecté au prochain
      `restorePurchases()`/`GET /billing/entitlement` sans action de
      l'utilisateur — la resynchronisation réelle passe par le backend
      (RTDN en configuration structurelle uniquement cette phase, voir
      Phase 2 — un renouvellement ne sera donc reflété qu'au prochain appel
      explicite de `/billing/google/verify` ou `/billing/entitlement` tant
      que Pub/Sub n'est pas réellement configuré).

---

**Non couvert par cette checklist** (hors périmètre Phase 3, nécessite une
configuration Google Cloud/Play Console réelle) : RTDN en conditions
réelles, acknowledgement en conditions réelles avec un vrai compte de
service, remboursement/révocation réels.
