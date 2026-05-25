/**
 * Dashboards Worker — fronts a Cloudflare R2 bucket and serves the dashboard
 * JSON files written by the various refresh Lambdas.
 *
 * Routing
 * ───────
 *   GET /                                                → game-campaigns/campaigns_life.json  (legacy)
 *   GET /<allowed/r2/key>                                → that key from R2
 *
 * Any path *not* in ALLOWED_KEYS returns 404 — keeps the bucket from being
 * browsable. Add a new entry here whenever a new Lambda starts publishing
 * to a new R2 key.
 *
 * CORS
 * ────
 * `ALLOWED_ORIGINS` is the list of browser origins permitted to call this
 * Worker. Requests from anywhere else are rejected with 403. Same-origin /
 * server-to-server requests (no Origin header) are allowed.
 *
 * Binding
 * ───────
 * Expects an R2 binding called `DASHBOARDS_BUCKET` in the Worker config:
 *
 *   [[r2_buckets]]
 *   binding    = "DASHBOARDS_BUCKET"
 *   bucket_name = "<your bucket>"
 */
export default {
  async fetch(request, env) {
    const ALLOWED_ORIGINS = new Set([
      "https://o.optimism.com",
      // TODO: add the SuperAge dashboard's origin here once it's hosted —
      //       e.g. "https://superage-dashboard.example.com". Without it the
      //       browser will block the fetch from the dashboard page.
    ]);

    // Map of public URL path → R2 object key. The path-and-key are kept
    // identical so each new addition is one line.
    const ALLOWED_KEYS = new Set([
      "game-campaigns/campaigns_life.json",
      "superage-dashboard/superage-metrics.json",
      "superage-dashboard/superage-comparison.json",
    ]);

    const origin       = request.headers.get("Origin");
    const allowOrigin  = origin && ALLOWED_ORIGINS.has(origin) ? origin : "";
    const corsHeaders  = {
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Max-Age":       "86400",
      ...(allowOrigin ? { "Access-Control-Allow-Origin": allowOrigin, "Vary": "Origin" } : {}),
    };

    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    // Block cross-origin browser requests we don't recognise. Server-to-server
    // (no Origin) is allowed through.
    if (origin && !ALLOWED_ORIGINS.has(origin)) {
      return new Response("Forbidden", { status: 403 });
    }

    if (request.method !== "GET") {
      return new Response("Method Not Allowed", { status: 405, headers: corsHeaders });
    }

    // Resolve the R2 key:
    //   "/"  → legacy default (game-campaigns/campaigns_life.json)
    //   "/<path>" → that exact key, if whitelisted
    const url = new URL(request.url);
    const raw = url.pathname.replace(/^\/+/, "");
    const key = raw === "" ? "game-campaigns/campaigns_life.json" : raw;

    if (!ALLOWED_KEYS.has(key)) {
      return new Response("Not found", { status: 404, headers: corsHeaders });
    }

    const object = await env.DASHBOARDS_BUCKET.get(key);
    if (!object) {
      return new Response("Not found", { status: 404, headers: corsHeaders });
    }

    return new Response(object.body, {
      headers: {
        "Content-Type":  "application/json",
        "Cache-Control": "public, max-age=60",
        ...corsHeaders,
      },
    });
  },
};
