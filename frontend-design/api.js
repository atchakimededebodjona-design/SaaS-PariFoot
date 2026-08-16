// api.js — couche d'accès à l'API xFoot, partagée par login.html/index.html/billing.html.
//
// API_BASE_URL dérivée de window.location.hostname (jamais codée en dur sur
// "localhost") : la même page fonctionne qu'elle soit ouverte depuis ce PC
// (http://localhost:5500) ou depuis un téléphone sur le réseau local
// (http://<IP-LAN>:5500) — l'API tourne toujours sur le port 8000 de la
// même machine que celle qui sert cette page.
const API_BASE_URL = `${window.location.protocol}//${window.location.hostname}:8000`;

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
