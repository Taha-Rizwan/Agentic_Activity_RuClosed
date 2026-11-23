# RUClosed: project handoff (everything done so far)

Use this file to continue the work in any new session or on another machine. It covers the brief, what has been built,
the design decisions and why, how to run and deploy, what is left, and draft answers for the submission form.

---

## 1. The brief (instructor's final instructions)

**Design to Production: Agentic AI Hackathon (5 absolutes).** Build and deploy a complete AI agent product that can earn money.
Group key: `claude-haiku-5-5`, **$2 spend limit**. Use only our group's key, and never commit or share it.

- **Track:** 3, DealCloser: lead closer for real estate (qualify, follow up, book visits). Product name **RUClosed**, Group 5,
  members 23i-0670, 23i-0664, 23i-0611.
- **Must have:** a website with a customer page and an admin dashboard; LangGraph with at least 2 agents/nodes; at least 2 real tools;
  short-term memory by `thread_id`; long-term memory by `user_id`; users kept separate; safety (refuse off-topic and
  prompt injection, ask before risky actions).
- **Agent API (graded automatically, open without login):**
  - `POST /chat` with body `{"user_id","thread_id","message"}` returns `{"reply","tool_calls":[...],"tokens":{"in","out"}}`
  - `GET /health` returns `ok`
- **Deployment (free):** website on Vercel; agent API on Hugging Face Spaces (Docker) or Render; long-term memory in
  Supabase or Neon Postgres (not in-memory).
- **Prepare:** (1) live website URL, (2) agent API base URL, (3) a public GitHub repo whose README has an architecture
  diagram, setup, demo GIF, benchmark table and memory design note, plus a `.env.example` and no key, (4) `tests.json` with 10 own
  cases, (5) a 2–3 minute demo video (unlisted YouTube or Drive), (6) a photo of the paper canvas (Drive link).
- **Submit:** the Google Form (group leader only) **and** Google Classroom. The form's answers about nodes, tools, memory,
  safety and demo data decide what the agent is tested on.
  Form: https://docs.google.com/forms/d/e/1FAIpQLSd0cLrSXbuGEUVwVh2cLP3m58tq6-eE5sgR58QgxKpJux-mPA/viewform
- **Grading (100):** task success 25, tool accuracy 10, short-term memory 10, long-term memory 10, user isolation 5,
  reliability 15, cost efficiency 10, safety 5, product/GitHub/demo 10.
- **Deadline:** Monday 23 November 2026, 11:59 PM. Evaluation runs Tuesday 24 Nov. **Keep the website and API live until
  Wednesday 25 Nov**; an agent that is offline scores 0 on the automated tests.

## 2. Product canvas (paper, from the workshop)

- **Customer:** small real-estate agencies getting leads from Zameen.com and OLX.
- **Problem:** leads arrive 24/7 but human agents reply late, so hot and warm leads go cold and commission is lost.
- **Earns:** PKR 15,000/month per agency + PKR 1,000 per booked site visit.
- **Demo moment:** "10 marla plot in Bahria under 2 crore, installments" → real plots, monthly installments, book a visit.
- **Graph:** START → Guard → Qualifier → Sales Agent ⇄ Tools → Memory Saver → END.
- **Tools:** Search(area, price, size), Installments(price, down payment, months), VisitSlot(property), BookSlot(property, slot,
  name, contact), FollowUp(lead, msg), which needs approval.
- **Short-term memory:** messages + lead profile; the last 10–15 messages go to the model, older ones are summarised.
- **Long-term memory:** do NOT save CNIC, income, chit-chat or PII; forget on request.
- **Multiple users:** every agency is a tenant; admins log in with a token; every query is filtered by agency id.
- **Biggest risk:** the AI making up prices or discounts, spamming leads, prompt injection. Guard: prices only come from the DB,
  and anything not in the DB goes to a human.

## 3. What is built (all pushed to `main`)

| Area | Where | Notes |
|---|---|---|
| Graph | `backend/agent/graph.py` | guard → qualifier → sales_agent (`create_agent` + middleware) → memory_saver |
| Middleware | `backend/agent/middleware.py` | risk router (discount/forget handled by code), CNIC + card PII redaction, summarisation (keep 12), dynamic prompt with memory, tool access policy, Anthropic prompt caching, fallback to the offline model, model/tool call limits (8), tool retry, audit log, price-grounding output guard |
| Tools (9) | `backend/agent/tools.py` | search_properties, get_property_details, calculate_installments, get_visit_slots, book_visit, schedule_followup (approval queue), request_human_review, remember_fact, forget_my_data. Each tool reads the agency and user from `runtime.context` (the reference-repo pattern) |
| Safety | `backend/agent/safety.py` | regex guard: injection, data exfiltration, off-topic (hard and soft), discount/forget risk flags; with a key set, ambiguous messages also go to a small Claude classifier |
| Parsing | `backend/agent/parsing.py` | marla/kanal, lakh/crore budgets, timelines, slots ("Saturday 11am"), names, PK phones, CNIC, PII |
| Model | `backend/agent/llm.py` | ChatAnthropic `claude-haiku-5-5` (max_tokens 600, timeout 30). Without a key: an offline rule-based model, so the demo and tests run for free |
| Service | `backend/agent/service.py` | Postgres checkpointer + store when `DATABASE_URL` is set, else SQLite. Counts tokens and returns structured tool_calls. A "yes"/"no" message resumes a pending booking confirmation |
| DB | `backend/db.py` | one adapter for Postgres (psycopg pool) and SQLite; tenant-scoped queries; seeds 2 agencies, 17 synthetic listings and 4 demo leads |
| API | `backend/main.py` | **`POST /chat`, `GET /health`** (graded), plus the widget API `/api/...`, admin API, inbound-lead webhook, CSV upload and report |
| Frontend | `frontend/` | landing page, `/a/{slug}` storefront + chat widget, `/a/{slug}/p/{id}` property page + calculator, `/admin` login + dashboard (overview with chart, leads with conversation + memory, approval queue, visits, listings, outbox/audit, integrations) |
| Tests | `tests/` | 23 pytest tests (offline model): rules, middleware, booking, memory, approvals, webhook, tenant isolation |
| Benchmark | `tests.json` + `benchmark/run_benchmark.py` | 10 own cases (tests.json) + 5 attack tests. `--url=` runs them against the deployed API |
| Deploy | `Dockerfile` (port 7860), `deploy/huggingface/README.md`, `render.yaml`, `frontend/vercel.json` | the Vercel config proxies `/api/*` to the agent API |
| Docs | `README.md`, `docs/demo.gif`, `docs/*.png`, `docs/graph.mmd` | the README has the architecture, middleware, tools, API, memory design note, benchmark, setup and deploy |

Demo logins (synthetic): `admin@capital.demo` / `demo123`, `admin@lahore.demo` / `demo123`.

### Key decisions (and why)

- **Booking on `/chat` happens directly** when the customer explicitly asks with listing, time, name and phone, because the grader
  can't click a confirm button. The web widget still shows a confirm card (`interrupt()`). Risky actions (sending
  messages, discounts) always wait for a human in the approval queue.
- **The name is kept in long-term memory** (so "what's my name?" works across chats). Phone, CNIC, income and email are never stored
  in memory; the phone lives only in the agency's CRM lead row.
- **Isolation:** checkpoint key `agency:user:thread`, store namespace `("memories", agency, user)`, both built by the server.
- **Cost:** guard refusals cost 0 tokens; prompt caching; max_tokens 600; summarisation keeps the context small; 8-call cap per turn.

### Fixes along the way

- "My name is …" with a capital M wasn't parsed, so booking kept asking for contact details. Fixed.
- A data-exfiltration phrasing ("phone numbers of your other clients") slipped past the guard. Fixed and tested.
- When `create_agent` ran as a direct subgraph node, the summarisation removals didn't reach the parent state. Fixed by invoking it inside a node.
- Windows `UnicodeEncodeError` when writing the benchmark results (cp1252). Fixed by writing UTF-8.
- The Postgres path was tested against a real local PostgreSQL 16 (seed, search, booking, memory).

## 4. How to run

```powershell
# Windows, in the folder that contains requirements.txt
py -m pip install -r requirements.txt
copy .env.example .env        # then set ANTHROPIC_API_KEY (and DATABASE_URL for Postgres), plus LangSmith if you want tracing
py -m uvicorn backend.main:app --reload --port 8000
py -m benchmark.run_benchmark                         # uses Claude if the key is set
py -m benchmark.run_benchmark --url=https://<api>     # against the deployed API
py -m pytest
```

`.env` keys: `ANTHROPIC_API_KEY`, `MODEL=claude-haiku-5-5`, `SECRET_KEY`, `DATABASE_URL`, `DEFAULT_AGENCY`,
`LANGSMITH_TRACING/API_KEY/PROJECT` (optional), `OFFLINE_MODE`.

## 5. Still to do (in order)

1. **Supabase:** create the project → copy the *Session pooler* URI → that's `DATABASE_URL`.
2. **Hugging Face Space (Docker):** push the repo, use `deploy/huggingface/README.md` as the Space README, and add the secrets
   `ANTHROPIC_API_KEY`, `DATABASE_URL`, `SECRET_KEY`. Check `/health` → `ok`.
3. **Vercel:** put the API URL into `frontend/vercel.json`, then deploy with root directory `frontend`.
4. Run `py -m benchmark.run_benchmark --url=<api>` against the live API. Copy the results into the README benchmark table
   (the current numbers come from the offline model) and fill in the two URLs at the top of the README.
5. Record the 2–3 minute demo video (storefront chat → booking → dashboard → approval queue → attack blocked → new chat remembers).
6. Upload the canvas photo to Drive (anyone with the link can view).
7. Submit the Google Form + Classroom. Keep everything live until Wed 25 Nov (a pinger on `/health` if you use Render).
8. If the instructor's "5 common attack tests" differ from A1–A5, edit `ATTACKS` in `benchmark/run_benchmark.py`.

## 6. Draft answers for the submission form

- **Track:** 3, DealCloser (real estate). **Product:** RUClosed.
- **How the agent works / nodes:** LangGraph StateGraph with 4 nodes: (1) `guard`: a code-level safety policy that blocks prompt
  injection, requests for other clients' data, and off-topic requests before any model call; (2) `qualifier`: extracts area,
  size (marla/kanal), budget (lakh/crore), timeline, installments, name and phone, scores the lead hot/warm/cold and saves it to
  the agency CRM; (3) `sales_agent`: Claude Haiku (`create_agent`) with 9 tools and a middleware stack (PII redaction,
  summarisation, prompt caching, a price-grounding guard, call limits); (4) `memory_saver`: decides what to keep in long-term
  memory.
- **Tools:** search_properties (listings DB), get_property_details, calculate_installments (down payment, monthly plan),
  get_visit_slots, book_visit (saves the site visit, shown on the dashboard), schedule_followup (queued for human approval,
  never auto-sent), request_human_review (discounts, anything not in the DB), remember_fact, forget_my_data.
- **Short-term memory:** Postgres checkpointer keyed by thread_id (Supabase). The last 12 messages go to the model; older ones are
  summarised.
- **Long-term memory:** Postgres store keyed by user_id: name, area, size, type, budget, timeline, installments,
  viewed/booked listings, and saved preferences. Never phone, CNIC or income. "Forget my data" deletes it.
- **Safety:** refuses off-topic and prompt injection (guard node, no tool calls); never invents prices (an output guard checks
  every price against tool results); discounts go to a human; follow-up messages need admin approval before sending;
  CNIC is redacted.
- **User isolation:** all memory keys include user_id (and agency id), built server-side; tools never take a user or agency
  from the model.
- **Demo data (what to test with):** agency "Capital Estates" (default on /chat). Listings include #514 "10 Marla Plot,
  Bahria Town Phase 8" PKR 1.85 crore (installments: 25% down, 36 months); #515 10 marla Bahria Phase 8 PKR 1.62 crore;
  #516 10 marla Bahria Phase 7 PKR 2.35 crore (cash); #517 5 marla house Bahria PKR 1.65 crore; #519 8 marla Bahria
  Enclave PKR 1.1 crore; #520 1 kanal DHA plot PKR 5.8 crore; #521 10 marla DHA house PKR 4.75 crore; #522 7 marla
  Gulberg Greens PKR 95 lakh; #523 2-bed apartment Gulberg Greens PKR 85 lakh; #524 5 marla B-17 PKR 62 lakh.
  Visit slots: the next 7 days at 11:00, 14:00 and 16:00 (no Friday 14:00). Example: "Book a visit for #514 on
  Saturday 11am, I'm Ali, 0300-1234567".
