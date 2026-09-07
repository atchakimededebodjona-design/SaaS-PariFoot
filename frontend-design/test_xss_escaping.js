// test_xss_escaping.js — Phase 16.2 : non-régression de escapeHtml() (frontend-design/api.js),
// utilisée pour neutraliser deux points confirmés vulnérables avant ce correctif :
//   - external_reference (saisie libre par un admin, admin-withdrawals.html + promoter.html) ;
//   - name (saisie libre à l'inscription, jusqu'à 100 caractères, aucune restriction de format
//     côté backend — voir api/app/models/user.py::UserCreate — réaffiché à un ADMIN via
//     admin-subscribers.html et à un PROMOTEUR via promoter.html::r.client).
// Les deux étaient injectés en innerHTML sans échappement : un utilisateur/admin malveillant
// pouvait y stocker du HTML/JS exécuté dans la session d'un tiers (vol de token JWT en
// localStorage, action en son nom via l'API).
//
// Charge le VRAI fichier api.js (lecture seule, jamais dupliqué/réécrit ici), même technique
// que test_referral_capture.js.
//
// Usage : node frontend-design/test_xss_escaping.js

const fs = require("fs");
const path = require("path");

const API_JS_PATH = path.join(__dirname, "api.js");
const SOURCE = fs.readFileSync(API_JS_PATH, "utf-8");

let _passed = 0;
let _failed = 0;

function check(name, cond) {
    if (cond) {
        _passed += 1;
    } else {
        _failed += 1;
        console.log(`  FAIL: ${name}`);
    }
}

function section(name) {
    console.log(`\n=== ${name} ===`);
}

function loadEscapeHtml() {
    const win = {
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        location: { pathname: "/", search: "", protocol: "https:", hostname: "www.xfoot.site" },
        crypto: { randomUUID: () => "00000000-0000-0000-0000-000000000000" },
        Capacitor: undefined,
    };
    const fakeFetch = async () => ({ ok: true, json: async () => ({}) });
    const fn = new Function("window", "fetch", "console", SOURCE + "\nreturn { escapeHtml };");
    return fn(win, fakeFetch, console).escapeHtml;
}

const escapeHtml = loadEscapeHtml();

// Les 3 payloads explicitement demandés (Phase 16.2, Partie B) — représentatifs des 3
// familles d'injection HTML : balise avec gestionnaire d'événement, balise <script>, et
// évasion d'un attribut existant via `">`.
const PAYLOADS = [
    "<img src=x onerror=alert(1)>",
    "<script>alert(1)</script>",
    "\"><img src=x onerror=alert(1)>",
];

section("escapeHtml — les 3 payloads XSS requis ne contiennent plus aucun caractère structurant HTML");
for (const payload of PAYLOADS) {
    const escaped = escapeHtml(payload);
    // Le résultat attendu : affiché comme TEXTE, jamais interprété — donc aucun '<', '>' ou '"'
    // brut ne doit survivre (ce sont les seuls caractères qui peuvent ouvrir une balise, fermer
    // un attribut, ou échapper un attribut existant).
    check(`"${payload}" -> aucun '<' brut restant`, !escaped.includes("<"));
    check(`"${payload}" -> aucun '>' brut restant`, !escaped.includes(">"));
    check(`"${payload}" -> aucun '"' brut restant`, !escaped.includes('"'));
    check(`"${payload}" -> le payload original n'apparaît plus tel quel`, escaped !== payload);
}

section("escapeHtml — preuve que le résultat, réinjecté en innerHTML, ne peut PAS créer d'élément DOM exécutable");
// On ne charge pas jsdom (aucune dépendance ajoutée, Phase 16.2 l'interdit explicitement) :
// la preuve se fait par construction — un texte qui ne contient aucun '<'/'>' brut ne peut, par
// définition du parsing HTML, ouvrir/fermer une balise une fois inséré comme innerHTML.
for (const payload of PAYLOADS) {
    const escaped = escapeHtml(payload);
    check(`"${payload}" -> composé uniquement d'entités/caractères sûrs (regex /^[^<>]*$/)`, /^[^<>]*$/.test(escaped));
}
check(
    '<script>alert(1)</script> -> devient littéralement "&lt;script&gt;alert(1)&lt;/script&gt;"',
    escapeHtml("<script>alert(1)</script>") === "&lt;script&gt;alert(1)&lt;/script&gt;",
);
check(
    '<img src=x onerror=alert(1)> -> devient littéralement "&lt;img src=x onerror=alert(1)&gt;"',
    escapeHtml("<img src=x onerror=alert(1)>") === "&lt;img src=x onerror=alert(1)&gt;",
);

section("escapeHtml — valeurs légitimes (référence externe, nom d'utilisateur) inchangées dans leur sens");
check("référence de paiement sans caractère spécial -> identique", escapeHtml("TMONEY-2026-0042") === "TMONEY-2026-0042");
check("nom d'utilisateur accentué sans caractère spécial -> identique", escapeHtml("Koffi Adjovi") === "Koffi Adjovi");
check('apostrophe -> échappée sans casser l\'affichage', escapeHtml("O'Brien") === "O&#39;Brien");
check('esperluette -> échappée en premier (jamais de double échappement)', escapeHtml("Sel & Poivre") === "Sel &amp; Poivre");

section("escapeHtml — valeurs absentes/nulles (champs optionnels, ex. external_reference vide)");
check('null -> chaîne vide (jamais "null" affiché à la place du tiret placeholder)', escapeHtml(null) === "");
check('undefined -> chaîne vide', escapeHtml(undefined) === "");
check("valeur numérique -> convertie en texte sans erreur", escapeHtml(42) === "42");

console.log(`\n${"=".repeat(60)}\n${_passed}/${_passed + _failed} assertions reussies\n${"=".repeat(60)}`);
process.exit(_failed ? 1 : 0);
