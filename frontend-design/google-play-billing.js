// google-play-billing.js — glue JS entre le bridge natif Capacitor
// (mobile/android/app/src/main/java/site/xfoot/app/GooglePlayBillingPlugin.java)
// et le backend Xfoot (POST /billing/google/verify, GET /billing/entitlement).
//
// Chargé APRÈS api.js (utilise apiFetch/getToken déjà définis là-bas).
//
// RÈGLE CENTRALE : un événement natif "purchase réussi" n'est JAMAIS traité
// comme "Premium actif" tant que le backend n'a pas confirmé via
// POST /billing/google/verify — voir _handleUpdatedPurchase ci-dessous.
// Le prix affiché est TOUJOURS celui retourné par Google Play
// (formattedPrice), jamais une valeur codée en dur ici.

const XFOOT_GOOGLE_PLAY_PRODUCT_ID = "xfoot_premium";

const GooglePlayBilling = (() => {
    let _plugin = null;
    let _initialized = false;
    let _purchaseUpdateCallback = null;

    // Purchase.PurchaseState — valeurs brutes de la Billing Library,
    // relayées telles quelles par GooglePlayBillingPlugin.java.
    const PURCHASE_STATE_PURCHASED = 1;
    const PURCHASE_STATE_PENDING = 2;

    function isAvailable() {
        return typeof window.Capacitor !== "undefined"
            && typeof window.Capacitor.isNativePlatform === "function"
            && window.Capacitor.isNativePlatform()
            && window.Capacitor.getPlatform && window.Capacitor.getPlatform() === "android";
    }

    function _getPlugin() {
        if (_plugin) return _plugin;
        if (!window.Capacitor || !window.Capacitor.Plugins || !window.Capacitor.Plugins.GooglePlayBilling) {
            throw new Error("Plugin GooglePlayBilling indisponible (hors app Android empaquetée).");
        }
        _plugin = window.Capacitor.Plugins.GooglePlayBilling;
        return _plugin;
    }

    // callback(status) où status = {
    //   state: 'purchased' | 'pending' | 'canceled' | 'error' | 'restored',
    //   premium, premium_until, active_sources,  // seulement pour 'purchased'/'restored', renvoyés par le backend
    //   message,                                  // pour 'error'
    // }
    // Un seul callback à la fois — suffisant pour l'écran VIP, qui est la
    // seule page à utiliser ce module cette phase.
    function onPurchaseUpdate(callback) {
        _purchaseUpdateCallback = callback;
    }

    function _emit(status) {
        if (_purchaseUpdateCallback) _purchaseUpdateCallback(status);
    }

    async function init() {
        if (_initialized) return;
        const plugin = _getPlugin();
        await plugin.addListener("purchasesUpdated", (data) => {
            _handleUpdatedPurchase(data).catch((err) => {
                console.error("GooglePlayBilling: erreur de traitement purchasesUpdated", err);
                _emit({ state: "error", message: err.message || "Erreur inattendue." });
            });
        });
        const result = await plugin.initialize();
        if (!result.connected) {
            throw new Error(`Connexion à Google Play impossible (code ${result.responseCode}).`);
        }
        _initialized = true;
    }

    // Retourne les offres disponibles pour xfoot_premium, avec les prix
    // TELS QUE RETOURNÉS PAR GOOGLE PLAY (localisés) — jamais 1000/10000 FCFA
    // codés en dur : la page appelante doit afficher offer.pricingPhases[0].formattedPrice.
    async function getProducts() {
        const plugin = _getPlugin();
        const { products } = await plugin.getProducts({ productIds: [XFOOT_GOOGLE_PLAY_PRODUCT_ID] });
        const product = products.find((p) => p.productId === XFOOT_GOOGLE_PLAY_PRODUCT_ID);
        if (!product) return { monthly: null, yearly: null };
        const byBasePlan = {};
        for (const offer of product.subscriptionOfferDetails || []) {
            byBasePlan[offer.basePlanId] = offer;
        }
        return { monthly: byBasePlan.monthly || null, yearly: byBasePlan.yearly || null };
    }

    // basePlanId : "monthly" | "yearly" (tel que défini dans Play Console —
    // voir api/app/core/google_play_config.py, GOOGLE_PLAY_BASE_PLAN_IDS).
    async function purchase(basePlanId) {
        const token = getToken();
        if (!token) {
            // Jamais d'achat anonyme (voir plan Phase 3, section "Compte Xfoot").
            window.location.href = "login.html";
            return;
        }

        let userId;
        try {
            const meResponse = await apiFetch("/auth/me");
            const me = await meResponse.json();
            userId = me.id;
        } catch (e) {
            window.location.href = "login.html";
            return;
        }

        const plugin = _getPlugin();
        const result = await plugin.purchase({
            productId: XFOOT_GOOGLE_PLAY_PRODUCT_ID,
            basePlanId,
            obfuscatedAccountId: String(userId),
        });
        if (!result.launched) {
            _emit({ state: "error", message: `Impossible de lancer l'achat (code ${result.responseCode}).` });
        }
        // Le résultat réel (succès/annulation/pending) arrive de façon
        // asynchrone via l'événement "purchasesUpdated" -> _handleUpdatedPurchase.
    }

    // À appeler à l'ouverture de l'écran VIP (restauration silencieuse) et
    // sur un bouton explicite "Restaurer mes achats".
    async function restorePurchases() {
        const plugin = _getPlugin();
        const { purchases } = await plugin.queryPurchases();
        if (!purchases || purchases.length === 0) {
            _emit({ state: "none" });
            return;
        }
        for (const purchase of purchases) {
            await _processPurchase(purchase, /* isRestore */ true);
        }
    }

    async function _handleUpdatedPurchase(data) {
        const purchases = data.purchases || [];
        if (purchases.length === 0) {
            if (data.responseCode !== 0) {
                // BillingResponseCode.USER_CANCELED (1) et autres échecs natifs —
                // jamais interprété comme une erreur serveur, juste relayé.
                _emit({ state: data.responseCode === 1 ? "canceled" : "error", message: `Code Google Play : ${data.responseCode}` });
            }
            return;
        }
        for (const purchase of purchases) {
            await _processPurchase(purchase, /* isRestore */ false);
        }
    }

    async function _processPurchase(purchase, isRestore) {
        if (purchase.purchaseState === PURCHASE_STATE_PENDING) {
            // NE JAMAIS appeler /billing/google/verify pour un achat encore
            // PENDING (paiement non confirmé) — voir règle explicite Phase 3.
            _emit({ state: "pending", message: "Paiement en attente de confirmation." });
            return;
        }
        if (purchase.purchaseState !== PURCHASE_STATE_PURCHASED) {
            return; // UNSPECIFIED — rien à faire.
        }

        try {
            const response = await apiFetch("/billing/google/verify", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    product_id: XFOOT_GOOGLE_PLAY_PRODUCT_ID,
                    purchase_token: purchase.purchaseToken,
                }),
            });
            const body = await response.json().catch(() => ({}));

            if (response.status === 409) {
                _emit({ state: "error", message: "Cet achat Google Play est déjà lié à un autre compte Xfoot." });
                return;
            }
            if (!response.ok) {
                _emit({ state: "error", message: body.detail || `Erreur de validation serveur (${response.status}).` });
                return;
            }

            // La source de vérité reste le backend, jamais l'événement natif
            // "PURCHASED" lui-même — voir règle centrale en tête de fichier.
            _emit({
                state: isRestore ? "restored" : "purchased",
                premium: body.premium,
                plan: body.plan,
                expiry_time: body.expiry_time,
            });
        } catch (err) {
            _emit({ state: "error", message: "Impossible de contacter le serveur pour valider l'achat." });
        }
    }

    // Endpoint générique multi-provider (voir GET /billing/entitlement,
    // Phase 3) — à utiliser pour tout affichage de statut Premium côté
    // Android plutôt que GET /billing/subscription (resté Chariow-only).
    async function getEntitlement() {
        const response = await apiFetch("/billing/entitlement");
        if (!response.ok) throw new Error(`Erreur (${response.status})`);
        return response.json();
    }

    return { isAvailable, init, getProducts, purchase, restorePurchases, onPurchaseUpdate, getEntitlement };
})();
