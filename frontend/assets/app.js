// Shared helpers for every page.
const RU = {
  esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },
  pkr(n) {
    n = Number(n || 0);
    if (n >= 1e7) return "PKR " + (n / 1e7).toFixed(2).replace(/\.00$/, "") + " crore";
    if (n >= 1e5) return "PKR " + (n / 1e5).toFixed(1).replace(/\.0$/, "") + " lakh";
    return "PKR " + n.toLocaleString();
  },
  icon(type) { return { plot: "🌳", house: "🏡", apartment: "🏢", commercial: "🏬" }[type] || "📍"; },
  store: {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { } },
    del(k) { try { localStorage.removeItem(k); } catch { } },
  },
  async api(path, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    if (opts.token) headers["Authorization"] = "Bearer " + opts.token;
    if (opts.body && !(opts.body instanceof FormData)) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.body); }
    const res = await fetch(path, { method: opts.method || (opts.body ? "POST" : "GET"), headers, body: opts.body });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch { }
      const err = new Error(detail); err.status = res.status; throw err;
    }
    const ct = res.headers.get("content-type") || "";
    return ct.includes("json") ? res.json() : res.text();
  },
  slug() { const m = location.pathname.match(/^\/a\/([^/]+)/); return m ? m[1] : null; },
};
