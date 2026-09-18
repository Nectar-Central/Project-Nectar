/* Publishes the obfuscated userscript to Supabase Storage (public `cdn` bucket).
 *
 * Usage: node tools/obfuscate/publish.js [target]
 *   target = "nectar" (default) | "clippednow" (standalone clipper, obfuscated)
 * Reads credentials from server/.env (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
 * — never committed, never logged.
 * Build first: node tools/obfuscate/build.js (nectar) |
 *              node tools/obfuscate/build-clippednow.js (clippednow)
 * Reads credentials from server/.env (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
 * — never committed, never logged.
 * Uploads (nectar):
 *   cdn/nectar-companion.latest.user.js   (stable reinstall + @updateURL target)
 *   cdn/nectar-companion.v2.22.1.user.js  (immutable per-release copy)
 * Uploads (clippednow):
 *   cdn/clippednow.latest.user.js         (stable reinstall + @updateURL target)
   *   cdn/clippednow.v1.2.6.user.js         (immutable per-release copy)
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..", "..");
const VERSION = "2.31.0";

function loadEnv(p) {
  const env = {};
  for (const line of fs.readFileSync(p, "utf8").split("\n")) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
    if (m) env[m[1]] = m[2].replace(/^["']|["']$/g, "");
  }
  return env;
}

(async () => {
  const env = loadEnv(path.join(ROOT, "server", ".env"));
  const URL = (env.SUPABASE_URL || "").replace(/\/$/, "");
  const KEY = env.SUPABASE_SERVICE_ROLE_KEY || "";
  if (!URL || !KEY) throw new Error("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing in server/.env");
  const H = { apikey: KEY, Authorization: `Bearer ${KEY}`, "Content-Type": "application/json" };

  // 1. ensure public `cdn` bucket
  const mk = await fetch(`${URL}/storage/v1/bucket`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ id: "cdn", name: "cdn", public: true }),
  });
  console.log("bucket create:", mk.status, mk.status === 409 ? "(exists)" : await mk.text().catch(() => ""));

  // 2. upload build (latest + versioned)
  const target = (process.argv[2] || "nectar").toLowerCase();
  const isClip = target === "clippednow" || target === "clipped" || target === "standalone";
  const srcFile = isClip ? "clippednow.obfs.user.js" : "buildnow-companion.obfs.user.js";
  // Version comes from the built obfs header (inherits the source header),
  // so releases never need a hardcoded bump here.
  const builtHead = fs.readFileSync(path.join(ROOT, "companion", srcFile), "utf8").slice(0, 2000);
  const srcVer = isClip ? ((builtHead.match(/@version\s+(\S+)/) || [])[1] || "1.2.6") : VERSION;
  const prefix = isClip ? "clippednow" : "nectar-companion";
  const file = fs.readFileSync(path.join(ROOT, "companion", srcFile));
  for (const name of [`${prefix}.latest.user.js`, `${prefix}.v${srcVer}.user.js`]) {
    const up = await fetch(`${URL}/storage/v1/object/cdn/${name}`, {
      method: "POST",
      headers: {
        apikey: KEY,
        Authorization: `Bearer ${KEY}`,
        "Content-Type": "text/javascript; charset=utf-8",
        "x-upsert": "true",
      },
      body: file,
    });
    if (!up.ok) throw new Error(`upload ${name} failed: ${up.status} ${await up.text()}`);
    console.log(`uploaded cdn/${name} (${file.length} bytes)`);
  }

  // 3. verify public reachability
  const pub = `${URL}/storage/v1/object/public/cdn/${prefix}.latest.user.js`;
  const chk = await fetch(pub);
  const txt = await chk.text();
  console.log("public GET:", chk.status, `(${txt.length} chars)`);
  if (chk.status !== 200 || !txt.includes("==UserScript==") || !txt.includes(`@version      ${srcVer}`)) {
    throw new Error("public file verification failed");
  }
  console.log("INSTALL LINK:", pub);
})().catch((e) => {
  console.error("publish failed:", e.message);
  process.exit(1);
});
