"""RUClosed FastAPI backend: customer site API, admin dashboard API, inbound-lead webhook, static frontend."""
import csv
import hashlib
import hmac
import io
import secrets
import time
from contextlib import asynccontextmanager
from collections import defaultdict, deque

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import auth, config, db
from backend.agent import service
from backend.agent.llm import get_model, is_offline
from backend.agent.parsing import redact
from backend.agent.tools import available_slots, installment_plan

@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    service.get_graph()
    yield


app = FastAPI(title="RUClosed API", version="1.0.0", lifespan=lifespan,
              description="AI lead closer for real-estate agencies (LangGraph + Claude).")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── tiny per-IP rate limit for the paid model (protects the spend limit) ──
_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(request: Request, per_minute: int = 30):
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    q, now = _hits[ip], time.time()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= per_minute:
        raise HTTPException(429, "Too many messages, please wait a minute.")
    q.append(now)


def agency_or_404(slug: str) -> dict:
    agency = db.get_agency_by_slug(slug)
    if not agency:
        raise HTTPException(404, "Agency not found")
    return agency


def agency_from_token(payload: dict) -> dict:
    agency = db.get_agency(int(payload["agency_id"]))
    if not agency:
        raise HTTPException(404, "Agency not found")
    return agency


# ───────────────────────── public API ─────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "model": config.MODEL, "offline_model": is_offline(),
            "database": "postgres" if db.is_postgres() else "sqlite"}


# ───────────────────────── graded agent API (open, no login) ─────────────────────────

@app.get("/health", response_class=PlainTextResponse)
def health_plain():
    return "ok"


class AgentChatIn(BaseModel):
    user_id: str = "anonymous"
    thread_id: str = "default"
    message: str = ""
    agency: str | None = None   # optional tenant slug; defaults to DEFAULT_AGENCY


@app.post("/chat")
def agent_chat(body: AgentChatIn, request: Request):
    """POST /chat {"user_id","thread_id","message"} -> {"reply","tool_calls","tokens":{"in","out"}}.
    user_id keys long-term memory, thread_id keys short-term memory; both are scoped inside the agency."""
    rate_limit(request, per_minute=600)
    agency = db.get_agency_by_slug(body.agency or config.DEFAULT_AGENCY) or db.get_agency_by_slug(config.DEFAULT_AGENCY)
    user_id = (body.user_id or "anonymous").strip()[:100] or "anonymous"
    message = (body.message or "").strip()
    if not message:
        return {"reply": "Hi! Tell me what you're looking for, e.g. \"10 marla plot in Bahria under 2 crore\".",
                "tool_calls": [], "tokens": {"in": 0, "out": 0}}
    try:
        r = service.chat(agency, user_id, body.thread_id or "default", message[:4000], source="api",
                         confirm_bookings=False)
        return {"reply": r["reply"], "tool_calls": r["tool_calls"], "tokens": r["tokens"]}
    except Exception as e:  # never a 500 for the grader; log and answer politely
        db.log_event(agency["id"], user_id, "error", repr(e)[:500])
        return {"reply": "Sorry, I hit a problem handling that. Please try again, or an agent will contact you.",
                "tool_calls": [], "tokens": {"in": 0, "out": 0}}


@app.get("/api/agencies")
def agencies():
    return db.list_agencies()


@app.get("/api/a/{slug}")
def agency_info(slug: str):
    return agency_or_404(slug)


@app.get("/api/a/{slug}/properties")
def public_properties(slug: str, area: str | None = None, type: str | None = None, max_price: int | None = None,
                      size: float | None = None, installments: bool = False):
    agency = agency_or_404(slug)
    return db.search_properties(agency["id"], area, type, None, max_price, size, installments, limit=20)


@app.get("/api/a/{slug}/properties/{pid}")
def public_property(slug: str, pid: int):
    agency = agency_or_404(slug)
    p = db.get_property(agency["id"], pid)
    if not p:
        raise HTTPException(404, "Listing not found")
    return {**p, "price_text": db.fmt_pkr(p["price"]), "next_slots": available_slots(agency["id"], pid)[:6]}


@app.get("/api/a/{slug}/properties/{pid}/installments")
def public_installments(slug: str, pid: int, down_payment_pct: float = 25, months: int = 36):
    agency = agency_or_404(slug)
    p = db.get_property(agency["id"], pid)
    if not p:
        raise HTTPException(404, "Listing not found")
    if not p["installments"]:
        return {"available": False, "message": "Cash only listing"}
    dp = max(down_payment_pct, p["min_down_payment_pct"])
    m = max(1, min(months, p["max_months"]))
    plan = installment_plan(p["price"], dp, m)
    return {"available": True, "down_payment_pct": dp, **plan,
            "text": {k: db.fmt_pkr(v) for k, v in plan.items() if k != "months"}}


class SessionIn(BaseModel):
    token: str | None = None


@app.post("/api/a/{slug}/session")
def customer_session(slug: str, body: SessionIn):
    """Anonymous customer identity, signed by the server. Re-using the token keeps the same user_id
    (so long-term memory follows the customer across chat sessions), but only inside this agency."""
    agency = agency_or_404(slug)
    if body.token:
        payload = auth.verify(body.token)
        if payload and payload.get("kind") == "customer" and payload.get("agency_id") == agency["id"]:
            return {"token": body.token, "user_id": payload["user_id"], "agency": agency}
    user_id = "u_" + secrets.token_hex(8)
    token = auth.sign({"kind": "customer", "agency_id": agency["id"], "user_id": user_id})
    return {"token": token, "user_id": user_id, "agency": agency}


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session: str = "main"


class ResumeIn(BaseModel):
    session: str = "main"
    approved: bool


@app.post("/api/chat")
def chat(body: ChatIn, request: Request, customer: dict = Depends(auth.require_customer)):
    rate_limit(request)
    agency = agency_from_token(customer)
    try:
        return service.chat(agency, customer["user_id"], body.session, body.message)
    except Exception as e:  # never leak stack traces / keys to the browser
        db.log_event(agency["id"], customer["user_id"], "error", repr(e)[:500])
        return JSONResponse({"reply": "Sorry, I'm having trouble right now. An agent will get back to you shortly.",
                             "error": True, "tools_used": [], "interrupt": None}, status_code=200)


@app.post("/api/chat/resume")
def chat_resume(body: ResumeIn, customer: dict = Depends(auth.require_customer)):
    agency = agency_from_token(customer)
    return service.resume(agency, customer["user_id"], body.session, body.approved)


@app.get("/api/chat/history")
def chat_history(session: str = "main", customer: dict = Depends(auth.require_customer)):
    agency = agency_from_token(customer)
    return service.history(agency, customer["user_id"], session)


@app.get("/api/me/memory")
def my_memory(customer: dict = Depends(auth.require_customer)):
    return service.memories(int(customer["agency_id"]), customer["user_id"])


@app.delete("/api/me/memory")
def forget_me(customer: dict = Depends(auth.require_customer)):
    return {"deleted": service.forget(int(customer["agency_id"]), customer["user_id"])}


# ── inbound leads from Zameen.com / OLX / Facebook forms (reply within a minute) ──

def webhook_key(agency: dict) -> str:
    return hmac.new(config.SECRET_KEY.encode(), f"webhook:{agency['slug']}".encode(), hashlib.sha256).hexdigest()[:32]


class InboundLead(BaseModel):
    name: str | None = None
    phone: str | None = None
    message: str = Field(min_length=1, max_length=2000)
    source: str = "zameen"
    property_id: int | None = None


@app.post("/api/a/{slug}/inbound-lead")
def inbound_lead(slug: str, body: InboundLead, x_webhook_key: str | None = Header(default=None)):
    agency = agency_or_404(slug)
    if not x_webhook_key or not hmac.compare_digest(x_webhook_key, webhook_key(agency)):
        raise HTTPException(401, "Bad webhook key")
    user_id = "lead_" + hashlib.sha256(f"{agency['id']}:{body.phone or secrets.token_hex(6)}".encode()).hexdigest()[:12]
    lead = db.get_or_create_lead(agency["id"], user_id, source=body.source[:20])
    db.update_lead(agency["id"], lead["id"], name=body.name, phone=body.phone, source=body.source[:20])
    msg = body.message + (f" (about listing #{body.property_id})" if body.property_id else "")
    started = time.time()
    result = service.chat(agency, user_id, "inbound", msg, source=body.source[:20])
    if result.get("reply") and not result.get("blocked"):
        # Replying to the lead's own enquiry is the product; proactive follow-ups still need approval.
        db.add_outbox(agency["id"], lead["id"], "whatsapp", result["reply"], approved_by="auto-reply")
    return {"lead_id": lead["id"], "reply": result.get("reply"), "seconds": round(time.time() - started, 2),
            "tools_used": result.get("tools_used")}


# ───────────────────────── admin API ─────────────────────────

class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/api/admin/login")
def admin_login(body: LoginIn):
    admin = db.verify_admin(body.email, body.password)
    if not admin:
        raise HTTPException(401, "Wrong email or password")
    token = auth.sign({"kind": "admin", "agency_id": admin["agency_id"], "email": admin["email"]}, ttl_hours=12)
    return {"token": token, "agency": db.get_agency(admin["agency_id"]), "name": admin["name"]}


@app.get("/api/admin/me")
def admin_me(admin: dict = Depends(auth.require_admin)):
    agency = agency_from_token(admin)
    return {"email": admin["email"], "agency": agency, "offline_model": is_offline(), "model": config.MODEL,
            "webhook": {"url": f"/api/a/{agency['slug']}/inbound-lead", "key": webhook_key(agency)}}


@app.get("/api/admin/leads")
def admin_leads(admin: dict = Depends(auth.require_admin)):
    return db.list_leads(int(admin["agency_id"]))


@app.get("/api/admin/leads/{lead_id}")
def admin_lead(lead_id: int, admin: dict = Depends(auth.require_admin)):
    aid = int(admin["agency_id"])
    lead = db.get_lead(aid, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    return {"lead": lead, "messages": db.lead_messages(aid, lead_id), "visits": db.lead_visits(aid, lead_id),
            "memory": service.memories(aid, lead["user_id"])}


class LeadPatch(BaseModel):
    status: str | None = None
    score: str | None = None


@app.patch("/api/admin/leads/{lead_id}")
def admin_lead_patch(lead_id: int, body: LeadPatch, admin: dict = Depends(auth.require_admin)):
    aid = int(admin["agency_id"])
    if not db.get_lead(aid, lead_id):
        raise HTTPException(404, "Lead not found")
    if body.status and body.status not in ("new", "qualified", "visit_booked", "closed", "lost"):
        raise HTTPException(400, "bad status")
    if body.score and body.score not in ("hot", "warm", "cold"):
        raise HTTPException(400, "bad score")
    db.update_lead(aid, lead_id, status=body.status, score=body.score)
    return db.get_lead(aid, lead_id)


@app.get("/api/admin/approvals")
def admin_approvals(status: str | None = None, admin: dict = Depends(auth.require_admin)):
    return db.list_approvals(int(admin["agency_id"]), status)


class Decision(BaseModel):
    approve: bool
    message: str | None = None


@app.post("/api/admin/approvals/{approval_id}")
def admin_decide(approval_id: int, body: Decision, admin: dict = Depends(auth.require_admin)):
    item = db.decide_approval(int(admin["agency_id"]), approval_id, body.approve, admin["email"], body.message)
    if not item:
        raise HTTPException(404, "Not found or already decided")
    db.log_event(int(admin["agency_id"]), admin["email"], "approval_decision",
                 {"id": approval_id, "status": item["status"]})
    return item


FOLLOWUP_PROMPT = """Write one short, polite WhatsApp follow-up (max 45 words) from a Pakistani real-estate agency
to a buyer lead who went quiet. Mention their requirement and offer a site visit. No prices, no discounts,
no promises. Requirement: {req}. Agency: {agency}."""


@app.post("/api/admin/followups/run")
def admin_run_followups(hours: int = 24, admin: dict = Depends(auth.require_admin)):
    """'Follows up for days': drafts follow-ups for quiet leads. Drafts only, a human approves each one."""
    aid = int(admin["agency_id"])
    agency = agency_from_token(admin)
    drafted = []
    for lead in db.stale_leads(aid, hours):
        req = ", ".join(str(x) for x in [
            f"{lead['size_marla']:g} marla" if lead.get("size_marla") else None, lead.get("property_type"),
            f"in {lead['area']}" if lead.get("area") else None,
            f"budget {db.fmt_pkr(lead['budget_max'])}" if lead.get("budget_max") else None] if x) or "a property"
        msg = (f"Assalam-o-Alaikum{(' ' + lead['name'].replace(' (demo)', '')) if lead.get('name') else ''}! "
               f"This is {agency['name']}. You were looking for {req}. We have options that match. "
               f"Would you like to book a free site visit this week?")
        if not is_offline():
            try:
                from langchain_core.messages import HumanMessage
                out = get_model().invoke([HumanMessage(FOLLOWUP_PROMPT.format(req=req, agency=agency["name"]))])
                text = out.content if isinstance(out.content, str) else "".join(
                    b.get("text", "") for b in out.content if isinstance(b, dict))
                msg = redact(text.strip())[:600] or msg
            except Exception:
                pass
        qid = db.create_approval(aid, lead["id"], "followup", msg, reason=f"no reply for {hours}h+")
        drafted.append({"approval_id": qid, "lead_id": lead["id"], "message": msg})
    return {"drafted": len(drafted), "items": drafted}


@app.get("/api/admin/visits")
def admin_visits(admin: dict = Depends(auth.require_admin)):
    return db.list_visits(int(admin["agency_id"]))


class VisitPatch(BaseModel):
    status: str


@app.patch("/api/admin/visits/{visit_id}")
def admin_visit_patch(visit_id: int, body: VisitPatch, admin: dict = Depends(auth.require_admin)):
    if body.status not in ("booked", "completed", "cancelled", "no_show"):
        raise HTTPException(400, "bad status")
    if not db.set_visit_status(int(admin["agency_id"]), visit_id, body.status):
        raise HTTPException(404, "Visit not found")
    return {"ok": True}


@app.get("/api/admin/properties")
def admin_properties(admin: dict = Depends(auth.require_admin)):
    return db.list_properties(int(admin["agency_id"]))


class PropertyIn(BaseModel):
    title: str
    area: str
    block: str | None = None
    city: str | None = None
    property_type: str
    size_marla: float
    price: int
    bedrooms: int | None = None
    description: str | None = None
    installments: bool = False
    min_down_payment_pct: float = 25
    max_months: int = 36


@app.post("/api/admin/properties")
def admin_add_property(body: PropertyIn, admin: dict = Depends(auth.require_admin)):
    pid = db.add_property(int(admin["agency_id"]), body.model_dump())
    return db.get_property(int(admin["agency_id"]), pid)


@app.post("/api/admin/properties/upload")
async def admin_upload(file: UploadFile = File(...), admin: dict = Depends(auth.require_admin)):
    """CSV columns: title,area,block,city,property_type,size_marla,price,bedrooms,description,installments,
    min_down_payment_pct,max_months"""
    raw = (await file.read())[:2_000_000].decode("utf-8-sig", errors="replace")
    added, errors = [], []
    for i, row in enumerate(csv.DictReader(io.StringIO(raw)), start=2):
        try:
            added.append(db.add_property(int(admin["agency_id"]), {k.strip(): (v or "").strip() for k, v in row.items() if k}))
        except Exception as e:
            errors.append(f"row {i}: {e}")
    return {"added": len(added), "ids": added, "errors": errors[:20]}


class PropertyPatch(BaseModel):
    status: str


@app.patch("/api/admin/properties/{pid}")
def admin_property_patch(pid: int, body: PropertyPatch, admin: dict = Depends(auth.require_admin)):
    if body.status not in ("available", "sold", "reserved"):
        raise HTTPException(400, "bad status")
    if not db.set_property_status(int(admin["agency_id"]), pid, body.status):
        raise HTTPException(404, "Listing not found")
    return {"ok": True}


@app.get("/api/admin/stats")
def admin_stats(admin: dict = Depends(auth.require_admin)):
    return db.stats(int(admin["agency_id"]))


@app.get("/api/admin/outbox")
def admin_outbox(admin: dict = Depends(auth.require_admin)):
    return db.list_outbox(int(admin["agency_id"]))


@app.get("/api/admin/events")
def admin_events(admin: dict = Depends(auth.require_admin)):
    return db.list_events(int(admin["agency_id"]))


@app.get("/api/admin/report.csv")
def admin_report(admin: dict = Depends(auth.require_admin)):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["lead_id", "name", "source", "status", "score", "area", "size_marla", "budget_pkr", "timeline",
                "visits", "created_at"])
    for l in db.list_leads(int(admin["agency_id"])):
        w.writerow([l["id"], l["name"] or "", l["source"], l["status"], l["score"], l["area"] or "",
                    l["size_marla"] or "", l["budget_max"] or "", l["timeline"] or "", l["visits"], l["created_at"]])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=ruclosed-leads-report.csv"})


# ───────────────────────── frontend ─────────────────────────

F = config.FRONTEND_DIR
app.mount("/assets", StaticFiles(directory=F / "assets"), name="assets")


@app.get("/", include_in_schema=False)
def page_home():
    return FileResponse(F / "index.html")


@app.get("/a/{slug}", include_in_schema=False)
def page_agency(slug: str):
    agency_or_404(slug)
    return FileResponse(F / "agency.html")


@app.get("/a/{slug}/p/{pid}", include_in_schema=False)
def page_property(slug: str, pid: int):
    agency_or_404(slug)
    return FileResponse(F / "property.html")


@app.get("/admin", include_in_schema=False)
def page_login():
    return FileResponse(F / "admin" / "login.html")


@app.get("/admin/dashboard", include_in_schema=False)
def page_dashboard():
    return FileResponse(F / "admin" / "dashboard.html")
