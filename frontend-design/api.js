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
const API_BASE_URL = _LOCAL_HOSTNAME_RE.test(window.location.hostname)
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
