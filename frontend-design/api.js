// api.js — couche d'accès à l'API xFoot, partagée par login.html/index.html/billing.html.
//
// API en production (Railway, projet "luminous-adventure", domaine custom
// api.xfoot.site — voir HOSTINGER_DEPLOY.md).
const PRODUCTION_API_URL = "https://api.xfoot.site";

// Adresses privées (dev local sur ce PC, ou accès depuis un téléphone sur le
// même réseau) : localhost, boucle locale, et les 3 plages RFC1918.
const _LOCAL_HOSTNAME_RE = /^(localhost|127\.0\.0\.1|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})$/;

// En dev (page servie depuis ce PC ou son IP LAN), l'API tourne sur le port
// 8000 de la MÊME machine que celle qui sert cette page — dérivé de
// window.location.hostname, jamais codé en dur sur "localhost", pour
// marcher aussi bien en local qu'en accès mobile LAN.
//
// Une fois la page hébergée sur un vrai domaine (Hostinger), l'API n'est
// PLUS sur le même hôte (elle reste sur Railway) : impossible de la
// dériver de window.location, il faut PRODUCTION_API_URL ci-dessus.
//
// App native (Capacitor) : la WebView sert les fichiers locaux depuis
// https://localhost, qui matche _LOCAL_HOSTNAME_RE — sans ce garde-fou,
// l'app tenterait d'appeler https://localhost:8000 (rien n'y écoute) au
// lieu de l'API de production. window.Capacitor n'existe QUE dans une app
// empaquetée (jamais dans un navigateur classique, même en dev local).
const _isNativeApp = typeof window.Capacitor !== "undefined"
    && typeof window.Capacitor.isNativePlatform === "function"
    && window.Capacitor.isNativePlatform();

const API_BASE_URL = (!_isNativeApp && _LOCAL_HOSTNAME_RE.test(window.location.hostname))
    ? `${window.location.protocol}//${window.location.hostname}:8000`
    : PRODUCTION_API_URL;

const TOKEN_KEY = "xfoot_token";

function getToken() {
    return window.localStorage.getItem(TOKEN_KEY);
}

function setToken(token) {
    window.localStorage.setItem(TOKEN_KEY, token);
}

function clearToken() {
    window.localStorage.removeItem(TOKEN_KEY);
}

// Redirige vers login.html si aucun token n'est stocké — à appeler tout en
// haut des pages qui exigent une session (index.html, billing.html).
function requireAuth() {
    if (!getToken()) {
        window.location.href = "login.html";
    }
}

// Wrapper fetch() : ajoute l'en-tête Authorization si un token est présent,
// et redirige vers login.html sur 401 (token absent/expiré côté serveur)
// plutôt que de laisser chaque appelant gérer ce cas séparément.
async function apiFetch(path, options = {}) {
    const token = getToken();
    const headers = { ...(options.headers || {}) };
    if (token) {
        headers["Authorization"] = `Bearer ${token}`;
    }

    const response = await fetch(`${API_BASE_URL}${path}`, { ...options, headers });

    if (response.status === 401) {
        clearToken();
        window.location.href = "login.html";
        // Ne résout jamais : la redirection est déjà en cours, un appelant
        // qui continuerait après un 401 n'aurait de toute façon rien de
        // valide à afficher.
        return new Promise(() => {});
    }

    return response;
}

// Récupère l'utilisateur connecté avec son statut admin
let _cachedUser = null;
async function getCurrentUser() {
    if (!getToken()) return null;
    if (_cachedUser) return _cachedUser;
    try {
        const res = await apiFetch("/auth/me");
        if (res && res.ok) {
            _cachedUser = await res.json();
            return _cachedUser;
        }
    } catch (e) {
        console.error("Erreur getCurrentUser:", e);
    }
    return null;
}

// ---------------------------------------------------------------------------
// Programme de promotion / parrainage (Phase 14) — capture et attribution du
// lien https://www.xfoot.site/{slug}. Même mécanisme de persistance que le
// token (localStorage sur l'origine du frontend, PAS un cookie — Chariow/
// l'API tournent sur un domaine différent, xfoot_token est déjà stocké ainsi
// pour la même raison) — voir app/referral/router.py pour le contrat serveur.
// ---------------------------------------------------------------------------

const REFERRAL_SLUG_KEY = "xfoot_referral_slug";
const REFERRAL_CAPTURED_AT_KEY = "xfoot_referral_captured_at";
const VISITOR_ID_KEY = "xfoot_visitor_id";

// UUID anonyme persistant — AUCUNE donnée personnelle (pas d'email, pas d'IP,
// pas de user-agent) — sert uniquement à compter des visiteurs distincts
// côté serveur (voir ReferralVisit, app/models/promoter.py).
function getOrCreateVisitorId() {
    let id = window.localStorage.getItem(VISITOR_ID_KEY);
    if (!id) {
        id = (window.crypto && window.crypto.randomUUID) ? window.crypto.randomUUID()
            : `v-${Date.now()}-${Math.random().toString(36).slice(2)}`;
        window.localStorage.setItem(VISITOR_ID_KEY, id);
    }
    return id;
}

// Format de slug IDENTIQUE à app/referral/slug.py::_SLUG_RE côté backend et à
// la RewriteRule de frontend-design/.htaccess (1-40 caractères, [a-z0-9]
// uniquement, tirets internes, aucun point ni slash) — jamais une seconde
// définition du format : un candidat qui ne matche pas est simplement écarté
// ici, AVANT même d'appeler le backend, qui reste seul juge de l'existence et
// du statut réel d'un promoteur (voir POST /referral/resolve ci-dessous).
const _SLUG_CANDIDATE_RE = /^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$/;

// Phase 15.7.6 : la réécriture .htaccess qui sert /{slug} est INTERNE (jamais
// de redirection HTTP 301/302, volontairement — voir le commentaire de
// frontend-design/.htaccess) : le navigateur ne voit donc JAMAIS ?ref={slug}
// dans sa propre barre d'adresse, seulement location.pathname inchangé.
// Repli UNIQUEMENT si aucun ?ref= n'est présent (priorité 1 toujours au query
// param, pour ne jamais changer le comportement historique de
// /login.html?ref={slug}) — un seul segment, jamais un fichier réel
// (login.html/billing.html/etc. contiennent un '.', exclu par le regex
// ci-dessus, donc jamais interprétés comme un slug).
function _slugCandidateFromPathname() {
    const segment = window.location.pathname.replace(/^\/+/, "");
    if (!segment || segment.includes("/") || !_SLUG_CANDIDATE_RE.test(segment)) return null;
    return segment;
}

// À appeler au chargement de CHAQUE page publique (index.html en premier
// lieu, la landing réelle du site) : si l'URL porte ?ref={slug}, ou sinon si
// le pathname lui-même ressemble à un slug (cas de /{slug} réécrit en
// interne par .htaccess, voir frontend-design/.htaccess et
// _slugCandidateFromPathname ci-dessus), valide le slug côté serveur puis le
// mémorise (LAST VALID REFERRER — un nouveau ?ref= valide remplace toujours
// le précédent). Ne bloque jamais le rendu de la page (§31 : "le parcours
// doit être fluide", best-effort, jamais d'erreur visible si l'appel échoue).
async function captureReferralFromUrl() {
    try {
        const params = new URLSearchParams(window.location.search);
        const fromQuery = (params.get("ref") || "").trim().toLowerCase();
        const slug = fromQuery || _slugCandidateFromPathname();
        if (!slug) return;

        const res = await fetch(`${API_BASE_URL}/referral/resolve/${encodeURIComponent(slug)}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ visitor_id: getOrCreateVisitorId() }),
        });
        if (!res.ok) return;
        const body = await res.json();
        if (body.valid) {
            window.localStorage.setItem(REFERRAL_SLUG_KEY, slug);
            window.localStorage.setItem(REFERRAL_CAPTURED_AT_KEY, new Date().toISOString());
        }
        // slug invalide/inactif : comportement normal du site, rien à afficher (§32).
    } catch (e) {
        console.error("captureReferralFromUrl:", e);
    }
}

// À appeler juste après une inscription+connexion réussies (voir login.html)
// — le SEUL moment où une attribution devient une ligne serveur (§8/§9).
// Best-effort : ne doit jamais faire échouer le parcours d'inscription.
async function attributeReferralIfPresent() {
    const slug = window.localStorage.getItem(REFERRAL_SLUG_KEY);
    if (!slug) return;
    const capturedAt = window.localStorage.getItem(REFERRAL_CAPTURED_AT_KEY);
    try {
        await apiFetch("/referral/attribute", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ slug, captured_at: capturedAt, visitor_id: getOrCreateVisitorId() }),
        });
    } catch (e) {
        console.error("attributeReferralIfPresent:", e);
    } finally {
        // Un seul essai possible par nature (ALREADY_ATTRIBUTED derrière, §9) — jamais retenté après coup.
        window.localStorage.removeItem(REFERRAL_SLUG_KEY);
        window.localStorage.removeItem(REFERRAL_CAPTURED_AT_KEY);
    }
}

// Affiche/masque les liens de nav réservés (Promoteur/Admin) selon le rôle
// réel renvoyé par /auth/me (is_promoter/is_admin, jamais une hypothèse
// côté client) — à appeler une fois le DOM prêt sur chaque page qui inclut
// ces liens (voir index.html et les autres pages, bloc [data-role-nav]).
async function applyRoleBasedNav() {
    const user = await getCurrentUser();
    document.querySelectorAll('[data-role-nav="promoter"]').forEach(el => {
        el.classList.toggle("hidden", !(user && user.is_promoter));
    });
    document.querySelectorAll('[data-role-nav="admin"]').forEach(el => {
        el.classList.toggle("hidden", !(user && user.is_admin));
    });
}

