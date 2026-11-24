// Customer chat widget. Identity = a server-signed token per agency; session = a chat thread.
(function () {
  const slug = RU.slug();
  const TK = "ru_token_" + slug, SK = "ru_session_" + slug;
  let token = RU.store.get(TK);
  let session = RU.store.get(SK) || newSessionId();
  RU.store.set(SK, session);
  let busy = false;

  function newSessionId() { return "s" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6); }

  const root = document.createElement("div");
  root.innerHTML = `
    <button class="btn primary chat-fab" id="chatFab">💬 Ask our AI agent</button>
    <section class="chat card" id="chatBox" aria-label="Chat with the sales assistant">
      <div class="chat-head">
        <div><div class="t" id="chatTitle">Sales assistant</div><div class="s">Replies in seconds · 24/7</div></div>
        <div class="icons">
          <button id="chatMem" title="What do you remember about me?">Memory</button>
          <button id="chatNew" title="Start a new conversation">New chat</button>
          <button id="chatClose" aria-label="Close">✕</button>
        </div>
      </div>
      <div class="chat-log" id="chatLog" aria-live="polite"></div>
      <div class="chips" id="chatChips"></div>
      <form class="chat-form" id="chatForm">
        <input id="chatInput" autocomplete="off" placeholder="e.g. 10 marla plot in Bahria under 2 crore" maxlength="2000" />
        <button class="btn primary" id="chatSend">Send</button>
      </form>
      <div class="offline-note hidden" id="offlineNote">Running on the offline demo model (no API key configured).</div>
    </section>`;
  document.body.appendChild(root);
  const $ = id => document.getElementById(id);
  const log = $("chatLog");

  const CHIPS = [
    "10 marla plot in Bahria under 2 crore, installments",
    "Show me houses in DHA",
    "My name is Ali, 0300-1234567",
    "Book a visit for #514 on Saturday 11am",
    "Anything new in market?",
  ];
  $("chatChips").innerHTML = CHIPS.map(c => `<button type="button" class="chip">${RU.esc(c)}</button>`).join("");
  $("chatChips").addEventListener("click", e => { if (e.target.classList.contains("chip")) send(e.target.textContent); });

  async function ensureToken() {
    const r = await RU.api(`/api/a/${slug}/session`, { body: { token } });
    token = r.token; RU.store.set(TK, token);
    $("chatTitle").textContent = r.agency.name + " assistant";
  }

  // Minimal, safe markdown for bot replies: escape first, then **bold**, *italic* and "- " bullets.
  function md(text) {
    return RU.esc(text || "")
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<i>$2</i>")
      .replace(/^\s*[-*] /gm, "• ")
      .replace(/^#{1,4} /gm, "");
  }

  function add(role, text, extra = {}) {
    const d = document.createElement("div");
    d.className = "msg " + (role === "user" ? "user" : "bot") + (extra.blocked ? " blocked" : "");
    if (role === "user") d.textContent = text;
    else d.innerHTML = md(text);
    if (extra.tools && extra.tools.length) {
      const t = document.createElement("div"); t.className = "tools";
      t.innerHTML = extra.tools.map(x => `<span>⚙ ${RU.esc(x)}</span>`).join("");
      d.appendChild(t);
    }
    if (extra.blocked) {
      const t = document.createElement("div"); t.className = "tools";
      t.innerHTML = `<span>🛡 guard: ${RU.esc(extra.reason || "blocked")}</span>`; d.appendChild(t);
    }
    log.appendChild(d); log.scrollTop = log.scrollHeight;
  }

  function confirmCard(p) {
    const d = document.createElement("div"); d.className = "confirm";
    d.innerHTML = `<b>Please confirm your site visit</b><br>#${p.property_id} ${RU.esc(p.property_title)}<br>
      🗓 ${RU.esc(p.slot)} · 👤 ${RU.esc(p.name)} · 📞 ${RU.esc(p.phone)}
      <div class="row"><button class="btn primary sm" data-a="1">Confirm booking</button><button class="btn sm" data-a="0">Cancel</button></div>`;
    d.addEventListener("click", async e => {
      const a = e.target.dataset.a; if (a === undefined) return;
      d.querySelectorAll("button").forEach(b => b.disabled = true);
      await resume(a === "1"); d.remove();
    });
    log.appendChild(d); log.scrollTop = log.scrollHeight;
  }

  function handle(r) {
    if (r.offline_model) $("offlineNote").classList.remove("hidden");
    if (r.interrupt) { confirmCard(r.interrupt); return; }
    add("bot", r.reply || "…", { tools: r.tools_used, blocked: r.blocked, reason: r.block_reason });
  }

  async function send(text) {
    text = (text || "").trim(); if (!text || busy) return;
    busy = true; add("user", text); $("chatChips").classList.add("hidden"); $("chatInput").value = "";
    const typing = document.createElement("div"); typing.className = "typing"; typing.textContent = "Assistant is typing…";
    log.appendChild(typing); log.scrollTop = log.scrollHeight;
    try {
      if (!token) await ensureToken();
      let r;
      try { r = await RU.api("/api/chat", { token, body: { message: text, session } }); }
      catch (e) { if (e.status === 401 || e.status === 403) { token = null; await ensureToken(); r = await RU.api("/api/chat", { token, body: { message: text, session } }); } else throw e; }
      typing.remove(); handle(r);
    } catch (e) { typing.remove(); add("bot", "⚠ " + e.message); }
    busy = false;
  }

  async function resume(approved) {
    try { handle(await RU.api("/api/chat/resume", { token, body: { session, approved } })); }
    catch (e) { add("bot", "⚠ " + e.message); }
  }

  async function loadHistory() {
    log.innerHTML = "";
    try {
      const h = await RU.api("/api/chat/history?session=" + encodeURIComponent(session), { token });
      if (!h.length) add("bot", "Assalam-o-Alaikum! 👋 I can show you real listings, work out monthly installments and book a site visit. What are you looking for?");
      h.forEach(m => add(m.role, m.content));
    } catch { add("bot", "Assalam-o-Alaikum! 👋 What are you looking for?"); }
  }

  async function open(prefill) {
    $("chatBox").classList.add("open"); $("chatFab").classList.add("hidden");
    if (!token || !log.childElementCount) { await ensureToken(); await loadHistory(); }
    if (prefill) send(prefill); else $("chatInput").focus();
  }

  $("chatFab").onclick = () => open();
  $("chatClose").onclick = () => { $("chatBox").classList.remove("open"); $("chatFab").classList.remove("hidden"); };
  $("chatForm").onsubmit = e => { e.preventDefault(); send($("chatInput").value); };
  $("chatNew").onclick = async () => {
    session = newSessionId(); RU.store.set(SK, session); log.innerHTML = "";
    add("bot", "New conversation started. I still remember your saved preferences (if any) from before. How can I help?");
  };
  $("chatMem").onclick = async () => {
    if (!token) await ensureToken();
    const m = await RU.api("/api/me/memory", { token });
    const keys = Object.keys(m);
    const text = keys.length ? "Here's what I remember across chats (no phone/CNIC/income is ever stored):\n" +
      keys.map(k => `• ${k}: ${JSON.stringify(m[k])}`).join("\n") + "\n\nSay \"forget my data\" to delete it." :
      "I don't have anything saved about you yet.";
    add("bot", text);
  };

  window.RUChat = { open };
})();
