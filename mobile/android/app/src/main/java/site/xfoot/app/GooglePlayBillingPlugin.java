package site.xfoot.app;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import com.android.billingclient.api.AccountIdentifiers;
import com.android.billingclient.api.BillingClient;
import com.android.billingclient.api.BillingClientStateListener;
import com.android.billingclient.api.BillingFlowParams;
import com.android.billingclient.api.BillingResult;
import com.android.billingclient.api.PendingPurchasesParams;
import com.android.billingclient.api.ProductDetails;
import com.android.billingclient.api.Purchase;
import com.android.billingclient.api.PurchasesUpdatedListener;
import com.android.billingclient.api.QueryProductDetailsParams;
import com.android.billingclient.api.QueryPurchasesParams;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Bridge Capacitor minimal vers Google Play Billing Library 9.1.0 (Java,
 * API callback classique — pas de KTX/coroutines, voir commentaire dans
 * mobile/android/app/build.gradle).
 *
 * Ce plugin ne fait QUE relayer les données brutes de la Billing Library
 * vers le JavaScript (frontend-design/google-play-billing.js). Il ne
 * calcule JAMAIS "Premium", ne décide JAMAIS qu'un achat est valide, ne
 * stocke rien comme source de vérité, ne contient aucun secret, n'appelle
 * ni la base Xfoot ni Chariow — le backend (POST /billing/google/verify,
 * GET /billing/entitlement) reste l'unique source de vérité, comme
 * documenté dans le plan d'architecture Phase 3.
 *
 * Événement émis vers JS : "purchasesUpdated" — {responseCode, purchases: [...]}
 * à chaque retour de la Billing Library (achat, restauration, ou erreur),
 * jamais interprété ici : c'est au JS/backend de décider quoi en faire.
 */
@CapacitorPlugin(name = "GooglePlayBilling")
public class GooglePlayBillingPlugin extends Plugin implements PurchasesUpdatedListener {

    private BillingClient billingClient;

    // Cache des ProductDetails de la dernière requête getProducts() — la
    // Billing Library exige de réutiliser l'objet ProductDetails obtenu par
    // queryProductDetailsAsync (pas seulement son productId) pour lancer un
    // achat, voir purchase() ci-dessous. Purement un besoin technique de la
    // librairie, aucune logique métier.
    private final Map<String, ProductDetails> productDetailsCache = new HashMap<>();

    @Override
    public void load() {
        billingClient = BillingClient.newBuilder(getContext())
                .setListener(this)
                .enablePendingPurchases(PendingPurchasesParams.newBuilder().build())
                .build();
    }

    @PluginMethod
    public void initialize(PluginCall call) {
        if (billingClient.isReady()) {
            JSObject ret = new JSObject();
            ret.put("connected", true);
            call.resolve(ret);
            return;
        }
        billingClient.startConnection(new BillingClientStateListener() {
            @Override
            public void onBillingSetupFinished(BillingResult billingResult) {
                JSObject ret = new JSObject();
                ret.put("connected", billingResult.getResponseCode() == BillingClient.BillingResponseCode.OK);
                ret.put("responseCode", billingResult.getResponseCode());
                call.resolve(ret);
            }

            @Override
            public void onBillingServiceDisconnected() {
                // Reconnexion simple (une tentative) — le JS peut rappeler
                // initialize() explicitement si celle-ci échoue aussi ;
                // aucune boucle infinie ici, un plugin minimal n'a pas à
                // gérer de stratégie de retry élaborée.
                notifyListeners("billingServiceDisconnected", new JSObject());
                billingClient.startConnection(this);
            }
        });
    }

    @PluginMethod
    public void getProducts(PluginCall call) {
        JSArray productIdsArg = call.getArray("productIds");
        List<QueryProductDetailsParams.Product> products = new ArrayList<>();
        try {
            List<Object> ids = productIdsArg != null ? productIdsArg.toList() : null;
            if (ids == null || ids.isEmpty()) {
                call.reject("productIds requis (ex. [\"xfoot_premium\"]) — ce plugin ne connaît aucun productId Xfoot en dur.");
                return;
            }
            for (Object id : ids) {
                products.add(
                    QueryProductDetailsParams.Product.newBuilder()
                        .setProductId(String.valueOf(id))
                        .setProductType(BillingClient.ProductType.SUBS)
                        .build()
                );
            }
        } catch (Exception e) {
            call.reject("productIds invalide : " + e.getMessage());
            return;
        }

        QueryProductDetailsParams params = QueryProductDetailsParams.newBuilder()
                .setProductList(products)
                .build();

        billingClient.queryProductDetailsAsync(params, (billingResult, productDetailsResult) -> {
            if (billingResult.getResponseCode() != BillingClient.BillingResponseCode.OK) {
                call.reject("queryProductDetailsAsync a échoué", String.valueOf(billingResult.getResponseCode()));
                return;
            }
            List<ProductDetails> productDetailsList = productDetailsResult.getProductDetailsList();
            JSArray out = new JSArray();
            for (ProductDetails pd : productDetailsList) {
                productDetailsCache.put(pd.getProductId(), pd);
                out.put(productDetailsToJs(pd));
            }
            JSObject ret = new JSObject();
            ret.put("products", out);
            call.resolve(ret);
        });
    }

    @PluginMethod
    public void purchase(PluginCall call) {
        String productId = call.getString("productId");
        String basePlanId = call.getString("basePlanId");
        String obfuscatedAccountId = call.getString("obfuscatedAccountId");

        if (productId == null || basePlanId == null || obfuscatedAccountId == null) {
            call.reject("productId, basePlanId et obfuscatedAccountId sont requis.");
            return;
        }

        ProductDetails productDetails = productDetailsCache.get(productId);
        if (productDetails == null) {
            call.reject("Appeler getProducts() avant purchase() — ProductDetails inconnu pour " + productId);
            return;
        }

        String offerToken = null;
        List<ProductDetails.SubscriptionOfferDetails> offers = productDetails.getSubscriptionOfferDetails();
        if (offers != null) {
            for (ProductDetails.SubscriptionOfferDetails offer : offers) {
                if (basePlanId.equals(offer.getBasePlanId())) {
                    offerToken = offer.getOfferToken();
                    break;
                }
            }
        }
        if (offerToken == null) {
            call.reject("basePlanId introuvable dans les offres disponibles : " + basePlanId);
            return;
        }

        BillingFlowParams.ProductDetailsParams productDetailsParams =
            BillingFlowParams.ProductDetailsParams.newBuilder()
                .setProductDetails(productDetails)
                .setOfferToken(offerToken)
                .build();

        BillingFlowParams billingFlowParams = BillingFlowParams.newBuilder()
                .setProductDetailsParamsList(List.of(productDetailsParams))
                // Rattache l'achat au compte Xfoot connecté au moment de
                // l'achat (défense en profondeur en complément de la
                // vérification serveur côté POST /billing/google/verify) —
                // JAMAIS un achat anonyme, voir google-play-billing.js.
                .setObfuscatedAccountId(obfuscatedAccountId)
                .build();

        BillingResult result = billingClient.launchBillingFlow(getActivity(), billingFlowParams);
        JSObject ret = new JSObject();
        ret.put("launched", result.getResponseCode() == BillingClient.BillingResponseCode.OK);
        ret.put("responseCode", result.getResponseCode());
        call.resolve(ret);
        // Le résultat réel de l'achat (succès/annulation/erreur) arrive de
        // façon asynchrone via onPurchasesUpdated ci-dessous, jamais ici.
    }

    @PluginMethod
    public void queryPurchases(PluginCall call) {
        QueryPurchasesParams params = QueryPurchasesParams.newBuilder()
                .setProductType(BillingClient.ProductType.SUBS)
                .build();
        billingClient.queryPurchasesAsync(params, (billingResult, purchases) -> {
            if (billingResult.getResponseCode() != BillingClient.BillingResponseCode.OK) {
                call.reject("queryPurchasesAsync a échoué", String.valueOf(billingResult.getResponseCode()));
                return;
            }
            JSObject ret = new JSObject();
            ret.put("purchases", purchasesToJs(purchases));
            call.resolve(ret);
        });
    }

    @Override
    public void onPurchasesUpdated(BillingResult billingResult, List<Purchase> purchases) {
        JSObject data = new JSObject();
        data.put("responseCode", billingResult.getResponseCode());
        data.put("purchases", purchases != null ? purchasesToJs(purchases) : new JSArray());
        notifyListeners("purchasesUpdated", data);
    }

    @Override
    protected void handleOnDestroy() {
        if (billingClient != null) {
            billingClient.endConnection();
        }
        super.handleOnDestroy();
    }

    // -------------------------------------------------------------------
    // Conversion pure Billing Library -> JSObject, aucune interprétation
    // métier (pas de "isPremium", pas de validation) — juste les champs
    // bruts dont google-play-billing.js/le backend ont besoin.
    // -------------------------------------------------------------------

    private JSObject productDetailsToJs(ProductDetails pd) {
        JSObject obj = new JSObject();
        obj.put("productId", pd.getProductId());
        obj.put("productType", pd.getProductType());
        obj.put("title", pd.getTitle());

        JSArray offersJs = new JSArray();
        List<ProductDetails.SubscriptionOfferDetails> offers = pd.getSubscriptionOfferDetails();
        if (offers != null) {
            for (ProductDetails.SubscriptionOfferDetails offer : offers) {
                JSObject offerObj = new JSObject();
                offerObj.put("basePlanId", offer.getBasePlanId());
                offerObj.put("offerId", offer.getOfferId());
                offerObj.put("offerToken", offer.getOfferToken());

                JSArray phasesJs = new JSArray();
                for (ProductDetails.PricingPhase phase : offer.getPricingPhases().getPricingPhaseList()) {
                    JSObject phaseObj = new JSObject();
                    // Prix FORMATÉ ET LOCALISÉ par Google Play — jamais un
                    // prix codé en dur côté app (voir google-play-billing.js).
                    phaseObj.put("formattedPrice", phase.getFormattedPrice());
                    phaseObj.put("priceCurrencyCode", phase.getPriceCurrencyCode());
                    phaseObj.put("billingPeriod", phase.getBillingPeriod());
                    phasesJs.put(phaseObj);
                }
                offerObj.put("pricingPhases", phasesJs);
                offersJs.put(offerObj);
            }
        }
        obj.put("subscriptionOfferDetails", offersJs);
        return obj;
    }

    private JSArray purchasesToJs(List<Purchase> purchases) {
        JSArray out = new JSArray();
        for (Purchase purchase : purchases) {
            JSObject obj = new JSObject();
            obj.put("purchaseToken", purchase.getPurchaseToken());
            obj.put("orderId", purchase.getOrderId());
            obj.put("purchaseState", purchase.getPurchaseState()); // 0=UNSPECIFIED,1=PURCHASED,2=PENDING
            obj.put("isAcknowledged", purchase.isAcknowledged());
            obj.put("autoRenewing", purchase.isAutoRenewing());

            JSArray productsJs = new JSArray();
            for (String productId : purchase.getProducts()) {
                productsJs.put(productId);
            }
            obj.put("products", productsJs);

            AccountIdentifiers accountIdentifiers = purchase.getAccountIdentifiers();
            if (accountIdentifiers != null) {
                obj.put("obfuscatedAccountId", accountIdentifiers.getObfuscatedAccountId());
            }
            out.put(obj);
        }
        return out;
    }
}
