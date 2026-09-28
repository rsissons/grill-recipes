/*
 * Add-a-recipe relay for Flat Top, Barrel & Grill.
 *
 * The site is a static page, so it can't write to GitHub itself. This Cloudflare Worker
 * takes a recipe link from the site's form, checks it, and opens an "add-recipe" issue.
 * The GitHub Action (.github/workflows/add-recipe.yml) does the real import and replies
 * on the issue; the site polls /status to show that reply.
 *
 * Secrets (set with `wrangler secret put`, never committed):
 *   GH_TOKEN          fine-grained GitHub token: this repo only, Issues read/write
 *   TURNSTILE_SECRET  Cloudflare Turnstile secret key
 * Bindings:
 *   LIMITER           rate limiter for /add (see wrangler.toml)
 *   HEART_LIMITER     rate limiter for /heart
 *   DB                D1 database "grill-hearts": tables hearts and ratings (one row per recipe per device)
 */
const REPO = "rsissons/grill-recipes";
const ALLOWED_ORIGINS = ["https://rsissons.github.io", "http://localhost:8788", "http://127.0.0.1:8788"];
const UA = "Mozilla/5.0";

const COOKERS = ["Auto-detect", "Blackstone", "Pit Barrel", "Grill", "Sous Vide"];
const TYPES = ["Auto-detect", "Beef main", "Chicken main", "Pork main", "Seafood main", "Other main (lamb, duck, tofu)", "Side dish", "Appetizer"];

function cors(origin) {
  const ok = ALLOWED_ORIGINS.includes(origin) || (origin || "").startsWith("file://") || origin === "null";
  return {
    "Access-Control-Allow-Origin": ok ? origin : ALLOWED_ORIGINS[0],
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Vary": "Origin",
  };
}

function json(data, status, origin) {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json", ...cors(origin) } });
}

async function gh(env, path, init = {}) {
  return fetch(`https://api.github.com/repos/${REPO}${path}`, {
    ...init,
    headers: {
      "Authorization": `Bearer ${env.GH_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "grill-recipes-relay",
      ...(init.headers || {}),
    },
  });
}

/* Quick look at the page. Returns "recipe", "not-recipe" or "unknown" (site blocked us; let the Action try its fallbacks). */
async function looksLikeRecipe(url) {
  try {
    const r = await fetch(url, { headers: { "User-Agent": UA }, redirect: "follow", cf: { cacheTtl: 0 } });
    if (r.status !== 200) return "unknown";
    const type = r.headers.get("content-type") || "";
    if (!type.includes("html")) return "not-recipe";
    const html = (await r.text()).slice(0, 2_000_000);
    const blocks = [...html.matchAll(/<script[^>]*application\/ld\+json[^>]*>([\s\S]*?)<\/script>/gi)].map(m => m[1]);
    if (blocks.some(b => /"@type"\s*:\s*(\[[^\]]*"Recipe"[^\]]*\]|"Recipe")/.test(b))) return "recipe";
    if (/itemtype=["']https?:\/\/schema\.org\/Recipe["']/i.test(html)) return "recipe";
    return "not-recipe";
  } catch {
    return "unknown";
  }
}

async function handleAdd(req, env, origin) {
  let body;
  try { body = await req.json(); } catch { return json({ ok: false, reason: "Bad request." }, 400, origin); }

  const url = String(body.url || "").trim();
  if (!/^https?:\/\/[^\s/$.?#].[^\s]*$/i.test(url) || url.length > 500) {
    return json({ ok: false, reason: "That doesn't look like a web link. Paste the full address, starting with https://" }, 400, origin);
  }

  // Bot check
  const ip = req.headers.get("CF-Connecting-IP") || "unknown";
  const form = new FormData();
  form.append("secret", env.TURNSTILE_SECRET);
  form.append("response", String(body.token || ""));
  form.append("remoteip", ip);
  const ts = await (await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", { method: "POST", body: form })).json();
  if (!ts.success) return json({ ok: false, reason: "The human check didn't pass. Reload the page and try again." }, 403, origin);

  // Rate limit per visitor (limit set in wrangler.toml)
  if (env.LIMITER) {
    const { success } = await env.LIMITER.limit({ key: ip });
    if (!success) return json({ ok: false, reason: "That's a lot of recipes at once. Wait a minute and try again." }, 429, origin);
  }

  // Not a recipe? Don't add it.
  if ((await looksLikeRecipe(url)) === "not-recipe") {
    return json({ ok: false, reason: "That page isn't a recipe (it has no recipe card), so it wasn't added." }, 422, origin);
  }

  const cooker = COOKERS.includes(body.cooker) ? body.cooker : "Auto-detect";
  const type = TYPES.includes(body.type) ? body.type : "Auto-detect";
  const region = String(body.region || "").replace(/[^\w\s-]/g, "").slice(0, 30).trim();
  const issueBody = `### Recipe link\n\n${url}\n\n### Cooker\n\n${cooker}\n\n### Type\n\n${type}\n\n### Region (optional)\n\n${region || "_No response_"}\n\n<!-- submitted from the site form -->`;

  const r = await gh(env, "/issues", {
    method: "POST",
    body: JSON.stringify({ title: "Add recipe (from the site)", body: issueBody, labels: ["add-recipe"] }),
  });
  if (!r.ok) return json({ ok: false, reason: "Couldn't hand it off right now. Try again in a minute." }, 502, origin);
  const issue = await r.json();
  return json({ ok: true, issue: issue.number }, 200, origin);
}

/* Shared hearts: GET /hearts -> {recipeId: count}; POST /heart {id, device, on} -> {count} */
const ID_RE = /^[a-z0-9-]{1,80}$/;
const DEVICE_RE = /^[a-f0-9-]{16,64}$/;

async function handleHearts(env, origin) {
  const { results } = await env.DB.prepare("SELECT recipe, COUNT(*) AS n FROM hearts GROUP BY recipe").all();
  const counts = {};
  for (const r of results) counts[r.recipe] = r.n;
  const res = json({ ok: true, counts }, 200, origin);
  res.headers.set("Cache-Control", "public, max-age=20");
  return res;
}

async function handleHeart(req, env, origin) {
  let body;
  try { body = await req.json(); } catch { return json({ ok: false }, 400, origin); }
  const id = String(body.id || ""), device = String(body.device || "").toLowerCase();
  if (!ID_RE.test(id) || !DEVICE_RE.test(device)) return json({ ok: false }, 400, origin);
  if (env.HEART_LIMITER) {
    const ip = req.headers.get("CF-Connecting-IP") || "unknown";
    const { success } = await env.HEART_LIMITER.limit({ key: ip });
    if (!success) return json({ ok: false, reason: "slow down" }, 429, origin);
  }
  if (body.on) {
    await env.DB.prepare("INSERT OR IGNORE INTO hearts (recipe, device, created) VALUES (?, ?, ?)").bind(id, device, Date.now()).run();
  } else {
    await env.DB.prepare("DELETE FROM hearts WHERE recipe = ? AND device = ?").bind(id, device).run();
  }
  const row = await env.DB.prepare("SELECT COUNT(*) AS n FROM hearts WHERE recipe = ?").bind(id).first();
  return json({ ok: true, count: row ? row.n : 0 }, 200, origin);
}

/* Everything shared in one call: GET /community -> {hearts: {id: n}, ratings: {id: [avg, n]}} */
async function handleCommunity(env, origin) {
  const [h, r] = await env.DB.batch([
    env.DB.prepare("SELECT recipe, COUNT(*) AS n FROM hearts GROUP BY recipe"),
    env.DB.prepare("SELECT recipe, AVG(stars) AS avg, COUNT(*) AS n FROM ratings GROUP BY recipe"),
  ]);
  const hearts = {}, ratings = {};
  for (const x of h.results) hearts[x.recipe] = x.n;
  for (const x of r.results) ratings[x.recipe] = [Math.round(x.avg * 10) / 10, x.n];
  const res = json({ ok: true, hearts, ratings }, 200, origin);
  res.headers.set("Cache-Control", "public, max-age=20");
  return res;
}

/* POST /rate {id, device, stars}: stars 1-5 sets this device's rating, 0 removes it. Returns the new [avg, n]. */
async function handleRate(req, env, origin) {
  let body;
  try { body = await req.json(); } catch { return json({ ok: false }, 400, origin); }
  const id = String(body.id || ""), device = String(body.device || "").toLowerCase(), stars = parseInt(body.stars, 10);
  if (!ID_RE.test(id) || !DEVICE_RE.test(device) || !(stars >= 0 && stars <= 5)) return json({ ok: false }, 400, origin);
  if (env.HEART_LIMITER) {
    const ip = req.headers.get("CF-Connecting-IP") || "unknown";
    const { success } = await env.HEART_LIMITER.limit({ key: ip });
    if (!success) return json({ ok: false, reason: "slow down" }, 429, origin);
  }
  if (stars === 0) {
    await env.DB.prepare("DELETE FROM ratings WHERE recipe = ? AND device = ?").bind(id, device).run();
  } else {
    await env.DB.prepare("INSERT INTO ratings (recipe, device, stars, updated) VALUES (?, ?, ?, ?) ON CONFLICT (recipe, device) DO UPDATE SET stars = excluded.stars, updated = excluded.updated")
      .bind(id, device, stars, Date.now()).run();
  }
  const row = await env.DB.prepare("SELECT AVG(stars) AS avg, COUNT(*) AS n FROM ratings WHERE recipe = ?").bind(id).first();
  return json({ ok: true, rating: row && row.n ? [Math.round(row.avg * 10) / 10, row.n] : null }, 200, origin);
}

async function handleStatus(req, env, origin) {
  const n = parseInt(new URL(req.url).searchParams.get("issue") || "", 10);
  if (!n) return json({ ok: false }, 400, origin);
  const [iss, com] = await Promise.all([gh(env, `/issues/${n}`), gh(env, `/issues/${n}/comments?per_page=5`)]);
  if (!iss.ok) return json({ ok: false }, 404, origin);
  const issue = await iss.json();
  const comments = com.ok ? await com.json() : [];
  const reply = comments.length ? comments[comments.length - 1].body : "";
  const state = !reply ? "working" : issue.state_reason === "completed" ? "added" : "rejected";
  return json({ ok: true, state, reply }, 200, origin);
}

export default {
  async fetch(req, env) {
    const origin = req.headers.get("Origin") || "";
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors(origin) });
    const path = new URL(req.url).pathname;
    try {
      if (req.method === "POST" && path === "/add") return await handleAdd(req, env, origin);
      if (req.method === "GET" && path === "/status") return await handleStatus(req, env, origin);
      if (req.method === "GET" && path === "/hearts") return await handleHearts(env, origin);
      if (req.method === "POST" && path === "/heart") return await handleHeart(req, env, origin);
      if (req.method === "GET" && path === "/community") return await handleCommunity(env, origin);
      if (req.method === "POST" && path === "/rate") return await handleRate(req, env, origin);
      return json({ ok: true, service: "grill-recipes relay" }, 200, origin);
    } catch (e) {
      return json({ ok: false, reason: "Something went wrong on our end." }, 500, origin);
    }
  },
};
