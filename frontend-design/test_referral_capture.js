// test_referral_capture.js — Phase 15.7.6 : non-régression de captureReferralFromUrl()
// (frontend-design/api.js) après ajout du repli pathname (Phase 15.7.5 : cause racine
// prouvée — la réécriture .htaccess de /{slug} est INTERNE, jamais de redirection HTTP,
// donc window.location.search reste vide pour un visiteur arrivant via /promoter-2).
//
// Charge le VRAI fichier api.js (lecture seule, jamais dupliqué/réécrit ici) dans un
// environnement window/localStorage/fetch simulé — même technique que la preuve exécutée
// de reports/phase15_7/xfoot_phase15_7_5_referral_root_cause_*.md, désormais formalisée en
// suite de non-régression reproductible.
//
// Usage : node frontend-design/test_referral_capture.js

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

// Backend simulé : SEUL "promoter-2" est un promoteur réel/ACTIVE (mêmes règles que
// POST /referral/resolve/{slug}, api/app/referral/router.py:65-81 — un slug inexistant ou
// réservé renvoie exactement la même réponse {valid:false}, jamais distingué).
function makeSandbox({ pathname, search }) {
    const localStorageStore = {};
    const localStorage = {
        getItem: (k) => (Object.prototype.hasOwnProperty.call(localStorageStore, k) ? localStorageStore[k] : null),
        setItem: (k, v) => { localStorageStore[k] = v; },
        removeItem: (k) => { delete localStorageStore[k]; },
    };
    const fetchCalls = [];
    const fetchImpl = async (url) => {
        fetchCalls.push(url);
        const valid = url.includes("/referral/resolve/promoter-2");
        return { ok: true, json: async () => ({ valid }) };
    };
    const win = {
        localStorage,
        location: { pathname, search, protocol: "https:", hostname: "www.xfoot.site" },
        crypto: { randomUUID: () => "00000000-0000-0000-0000-000000000000" },
        Capacitor: undefined,
    };
    const fn = new Function("window", "fetch", "console",
        SOURCE + "\nreturn { captureReferralFromUrl };");
    const exported = fn(win, fetchImpl, console);
    return { exported, localStorageStore, fetchCalls };
}

async function scenario(name, { pathname, search }) {
    const { exported, localStorageStore, fetchCalls } = makeSandbox({ pathname, search });
    await exported.captureReferralFromUrl();
    return {
        name,
        slugCaptured: localStorageStore["xfoot_referral_slug"] ?? null,
        fetchCalls,
    };
}

async function main() {
    section("TEST A — query parameter historique (/login.html?ref=promoter-2) -> referral détecté");
    {
        const r = await scenario("A", { pathname: "/login.html", search: "?ref=promoter-2" });
        check("slug capturé = promoter-2", r.slugCaptured === "promoter-2");
        check("1 appel resolve émis", r.fetchCalls.length === 1);
    }

    section("TEST B — URL propre (/promoter-2, réécriture .htaccess interne) -> referral détecté");
    {
        const r = await scenario("B", { pathname: "/promoter-2", search: "" });
        check("slug capturé = promoter-2 (repli pathname)", r.slugCaptured === "promoter-2");
        check("1 appel resolve émis", r.fetchCalls.length === 1);
    }

    section("TEST C — URL normale (/login.html, sans query ni slug exploitable) -> aucun referral");
    {
        const r = await scenario("C", { pathname: "/login.html", search: "" });
        check("aucun slug capturé", r.slugCaptured === null);
        check("aucun appel resolve (login.html contient un point, exclu par le regex)", r.fetchCalls.length === 0);
    }

    section("TEST D — slash final (/promoter-2/) -> jamais considéré comme un slug valide");
    {
        const r = await scenario("D", { pathname: "/promoter-2/", search: "" });
        check("aucun slug capturé", r.slugCaptured === null);
        check("aucun appel resolve (segment contient un '/')", r.fetchCalls.length === 0);
    }

    section("TEST E — majuscule (/Promoter-2) -> jamais considéré comme un slug valide");
    {
        const r = await scenario("E", { pathname: "/Promoter-2", search: "" });
        check("aucun slug capturé", r.slugCaptured === null);
        check("aucun appel resolve (regex strictement minuscules)", r.fetchCalls.length === 0);
    }

    section("TEST F — page système (/billing.html) -> aucun referral");
    {
        const r = await scenario("F", { pathname: "/billing.html", search: "" });
        check("aucun slug capturé", r.slugCaptured === null);
        check("aucun appel resolve (billing.html contient un point, exclu)", r.fetchCalls.length === 0);
    }

    section("TEST G — slug inexistant (/slug-inexistant) -> candidat préparé, backend rejette proprement");
    {
        const r = await scenario("G", { pathname: "/slug-inexistant", search: "" });
        check("le frontend a préparé le candidat et interrogé le backend (comportement attendu, §4)", r.fetchCalls.length === 1);
        check("mais AUCUNE valeur n'est persistée : backend a répondu valid=false, jamais un faux promoteur", r.slugCaptured === null);
    }

    section("TEST — priorité query > pathname (jamais l'inverse)");
    {
        // pathname porterait un slug DIFFÉRENT (jamais un vrai promoteur) si jamais lu par erreur —
        // le query, présent, doit systématiquement gagner et le pathname ne doit jamais être consulté.
        const r = await scenario("priority", { pathname: "/should-never-be-read", search: "?ref=promoter-2" });
        check("slug capturé = promoter-2 (issu du query, jamais du pathname)", r.slugCaptured === "promoter-2");
        check("1 seul appel resolve, vers promoter-2", r.fetchCalls.length === 1 && r.fetchCalls[0].includes("promoter-2"));
    }

    section("TEST — cas réel Phase 15.7.5 (preuve avant correctif, non-régression)");
    {
        // Exactement le scénario qui produisait 0 appel fetch / localStorage null AVANT ce correctif
        // (voir reports/phase15_7/xfoot_phase15_7_5_referral_root_cause_*.md) — doit maintenant réussir.
        const r = await scenario("regression-15.7.5", { pathname: "/promoter-2", search: "" });
        check("le bug de la Phase 15.7.5 est corrigé (slug capturé, alors qu'avant : null)", r.slugCaptured === "promoter-2");
    }

    console.log(`\n${"=".repeat(60)}\n${_passed}/${_passed + _failed} tests reussis\n${"=".repeat(60)}`);
    return _failed === 0;
}

main().then((ok) => process.exit(ok ? 0 : 1));
