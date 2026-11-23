"""RUClosed tools. Pattern from the workshop reference (AnviroAi/Workshop app/tools.py):

- every tool reads the agency (tenant) and the customer from runtime.context, NEVER from a model argument,
  so the model cannot be talked into reading another agency's listings or another user's memory;
- risky actions pause for a human: book_visit calls interrupt() so the customer confirms in the UI,
  and schedule_followup only queues a message that an agency admin must approve before it is sent;
- long-term memory goes through runtime.store under ("memories", agency_id, user_id).
"""
import json
from datetime import datetime, timedelta
from typing import Literal

from langchain.tools import ToolRuntime, tool
from langgraph.types import interrupt

from backend import db
from backend.agent import parsing
from backend.agent.context import DealContext, memory_namespace

SLOT_TIMES = ["11:00", "14:00", "16:00"]
PropertyType = Literal["plot", "house", "apartment", "commercial"]


def tool_error(code: str, message: str, retryable: bool = False, **extra) -> str:
    """Lecture 7: a tool failure is an OBSERVATION, not a crash. Same shape for every tool, so the model (and the
    offline planner) can read the error code, whether a retry makes sense, and what to do next."""
    return json.dumps({"status": "error", "error": code, "message": message, "retryable": retryable, **extra})


def error_code(result: str) -> str | None:
    """Returns the error code of a tool_error() result, or None for a normal result."""
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return None
    return data.get("error") if isinstance(data, dict) and data.get("status") == "error" else None


def _card(p: dict) -> str:
    beds = f", {p['bedrooms']} bed" if p.get("bedrooms") else ""
    plan = (f"installments: yes (min {p['min_down_payment_pct']:.0f}% down, up to {p['max_months']} months)"
            if p["installments"] else "installments: no (cash/bank finance)")
    return (f"#{p['id']} {p['title']} | {p['area']} {p.get('block') or ''}, {p.get('city') or ''} | "
            f"{p['size_marla']:g} marla {p['property_type']}{beds} | {db.fmt_pkr(p['price'])} | {plan}")


def available_slots(agency_id: int, property_id: int, days: int = 7, start: datetime | None = None) -> list[str]:
    """CUSTOM: visit policy: next 7 days, 11:00 / 14:00 / 16:00, no 14:00 on Friday (Jummah), minus booked."""
    start = start or datetime.now()
    taken = db.booked_slots(agency_id, property_id)
    out = []
    for d in range(1, days + 1):
        day = start + timedelta(days=d)
        for t in SLOT_TIMES:
            if day.weekday() == 4 and t == "14:00":
                continue
            slot = f"{day:%Y-%m-%d} {t}"
            if slot not in taken:
                out.append(slot)
    return out


def installment_plan(price: int, down_payment_pct: float, months: int, annual_markup_pct: float = 0.0) -> dict:
    """CUSTOM: plain-maths calculator (most society plans are 0% markup; markup uses standard amortisation)."""
    down = round(price * down_payment_pct / 100)
    balance = price - down
    r = annual_markup_pct / 100 / 12
    monthly = balance / months if r == 0 else balance * r / (1 - (1 + r) ** -months)
    return {"price": price, "down_payment": down, "balance": balance, "months": months,
            "monthly": round(monthly), "total_paid": round(down + monthly * months)}


# ── search + details ──

@tool
def search_properties(runtime: ToolRuntime[DealContext], area: str | None = None, property_type: PropertyType | None = None,
                      max_price_pkr: int | None = None, min_price_pkr: int | None = None,
                      size_marla: float | None = None, installments_only: bool = False, limit: int = 5) -> str:
    """Search THIS agency's available listings. The ONLY source of prices.
    area: e.g. 'Bahria Town', 'DHA', 'Gulberg Greens'. property_type: plot | house | apartment | commercial.
    Prices are in PKR (1 crore = 10,000,000; 1 lakh = 100,000). size_marla: 1 kanal = 20 marla."""
    ctx = runtime.context
    results = db.search_properties(ctx.agency_id, area, property_type, min_price_pkr, max_price_pkr,
                                   size_marla, installments_only, limit)
    relaxed = ""
    if not results and (size_marla or max_price_pkr):
        # nothing exact: show the closest real alternatives instead of inventing one
        results = db.search_properties(ctx.agency_id, area, property_type, None,
                                       int(max_price_pkr * 1.25) if max_price_pkr else None, None,
                                       installments_only, limit)
        relaxed = "No exact match. Closest real listings:\n" if results else ""
    if not results and area:
        results = db.search_properties(ctx.agency_id, area, property_type, None, None, None, False, 3)
        relaxed = ("No listing matches the budget/size. Cheapest real options in that area (outside the stated "
                   "budget/size, tell the customer):\n") if results else ""
    if not results:
        return tool_error("NO_MATCH", "No listing in our database matches those filters. Do not invent one. "
                                      "Offer to widen the search or log the requirement for an agent.")
    return relaxed + "\n".join(_card(p) for p in results)


@tool
def get_property_details(property_id: int, runtime: ToolRuntime[DealContext]) -> str:
    """Full details of one listing by its id (e.g. 514 for '#514')."""
    p = db.get_property(runtime.context.agency_id, int(property_id))
    if not p:
        return tool_error("NOT_FOUND", f"Listing #{property_id} is not in this agency's database.", property_id=property_id)
    status = "" if p["status"] == "available" else f" (status: {p['status']})"
    return f"{_card(p)}{status}\n{p.get('description') or ''}"


# ── money ──

@tool
def calculate_installments(runtime: ToolRuntime[DealContext], property_id: int | None = None,
                           price_pkr: int | None = None, down_payment_pct: float | None = None,
                           down_payment_pkr: int | None = None, months: int | None = None,
                           annual_markup_pct: float = 0.0) -> str:
    """Work out the monthly installment. Prefer property_id so the price comes from the database.
    down_payment_pct e.g. 25; months e.g. 24 or 36. Uses the listing's own plan limits."""
    note = []
    p = None
    if property_id:
        p = db.get_property(runtime.context.agency_id, int(property_id))
        if not p:
            return tool_error("NOT_FOUND", f"Listing #{property_id} is not in this agency's database.", property_id=property_id)
        price = p["price"]
        if not p["installments"]:
            return (f"#{p['id']} {p['title']} is cash only ({db.fmt_pkr(price)}); the developer has no "
                    f"installment plan. An agent can advise on bank financing.")
        min_dp, max_m = p["min_down_payment_pct"], p["max_months"]
    elif price_pkr:
        price, min_dp, max_m = int(price_pkr), 0, 120
        note.append("(price given by the customer, not a listing)")
    else:
        return tool_error("MISSING_ARGUMENT", "Need a property_id or a price to calculate installments.",
                          retryable=True)
    if down_payment_pkr and not down_payment_pct:
        down_payment_pct = 100 * down_payment_pkr / price
    dp = down_payment_pct if down_payment_pct is not None else min_dp or 25
    if dp < min_dp:
        note.append(f"minimum down payment for this plan is {min_dp:.0f}%, so I used {min_dp:.0f}%")
        dp = min_dp
    m = int(months or max_m or 36)
    if m > max_m:
        note.append(f"this plan runs up to {max_m} months, so I used {max_m}")
        m = max_m
    plan = installment_plan(price, dp, m, annual_markup_pct)
    head = f"#{p['id']} {p['title']}" if p else "Your property"
    out = (f"{head}: price {db.fmt_pkr(plan['price'])}. Down payment {dp:g}% = {db.fmt_pkr(plan['down_payment'])}. "
           f"Balance {db.fmt_pkr(plan['balance'])} over {plan['months']} months = "
           f"{db.fmt_pkr(plan['monthly'])} per month"
           f"{'' if not annual_markup_pct else f' at {annual_markup_pct:g}% markup'}"
           f". Total paid {db.fmt_pkr(plan['total_paid'])}.")
    return out + (" Note: " + "; ".join(note) if note else "")


# ── site visits ──

@tool
def get_visit_slots(property_id: int, runtime: ToolRuntime[DealContext]) -> str:
    """Free site-visit slots for a listing over the next 7 days (format 'YYYY-MM-DD HH:MM')."""
    p = db.get_property(runtime.context.agency_id, int(property_id))
    if not p:
        return tool_error("NOT_FOUND", f"Listing #{property_id} is not in this agency's database.", property_id=property_id)
    slots = available_slots(runtime.context.agency_id, p["id"])
    pretty = [f"{datetime.strptime(s, '%Y-%m-%d %H:%M'):%a %d %b %I:%M %p} ({s})" for s in slots[:9]]
    return f"Free visit slots for #{p['id']} {p['title']}:\n" + "\n".join(pretty)


@tool
def book_visit(property_id: int, slot: str, runtime: ToolRuntime[DealContext],
               name: str | None = None, phone: str | None = None) -> str:
    """Book a site visit. slot is 'YYYY-MM-DD HH:MM' or natural text like 'Saturday 11am'.
    Needs the customer's name and phone (use what they told you). The customer confirms before it is saved."""
    ctx = runtime.context
    p = db.get_property(ctx.agency_id, int(property_id))
    if not p:
        return tool_error("NOT_FOUND", f"Listing #{property_id} is not in this agency's database.", property_id=property_id)
    normal = slot if len(slot) == 16 and slot[4] == "-" else parsing.parse_slot(slot)
    free = available_slots(ctx.agency_id, p["id"])
    if not normal or normal not in free:
        return tool_error("SLOT_UNAVAILABLE", f"'{slot}' is not a free slot for #{p['id']}. Offer a free slot.",
                          retryable=True, free_slots=free[:6])
    lead = db.get_lead(ctx.agency_id, ctx.lead_id) or {}
    name = name or lead.get("name")
    phone = phone or lead.get("phone")
    if not name or not phone:
        return tool_error("NEED_CONTACT", "Ask the customer for their name and mobile number, then call "
                                            "book_visit again.", retryable=True)

    if ctx.confirm_bookings:
        # Human in the loop: the customer confirms (button in the widget, or "yes" in chat). The run pauses here.
        decision = interrupt({"type": "confirm_booking", "property_id": p["id"], "property_title": p["title"],
                              "slot": normal, "name": name, "phone": phone,
                              "question": f"Confirm site visit for #{p['id']} {p['title']} on {normal}?"})
        if not (isinstance(decision, dict) and decision.get("approved")):
            return "Customer did not confirm. Nothing was booked."

    visit_id = db.create_visit(ctx.agency_id, ctx.lead_id, p["id"], normal, name, phone)
    db.update_lead(ctx.agency_id, ctx.lead_id, status="visit_booked", score="hot", name=name, phone=phone)
    if runtime.store is not None:
        ns = memory_namespace(ctx.agency_id, ctx.user_id)
        hist = runtime.store.get(ns, "history")
        value = hist.value if hist else {"viewed": [], "booked": []}
        value.setdefault("booked", []).append({"property_id": p["id"], "slot": normal})
        runtime.store.put(ns, "history", value)
    when = datetime.strptime(normal, "%Y-%m-%d %H:%M")
    return (f"BOOKED: visit #{visit_id} confirmed for #{p['id']} {p['title']} on {when:%A %d %B, %I:%M %p}. "
            f"Our agent will call {name} on the number given to share the exact pin location.")


# ── human hand-offs ──

@tool
def schedule_followup(message: str, runtime: ToolRuntime[DealContext], when: str | None = None) -> str:
    """Draft a follow-up WhatsApp message to this customer (e.g. they asked to be contacted later).
    It is NOT sent: it goes to the agency's approval queue and a human approves it first."""
    ctx = runtime.context
    qid = db.create_approval(ctx.agency_id, ctx.lead_id, "followup", message,
                             reason=f"customer asked for follow-up {when or ''}".strip())
    return f"QUEUED: follow-up #{qid} is waiting for an agent's approval before it is sent."


@tool
def request_human_review(reason: str, runtime: ToolRuntime[DealContext]) -> str:
    """Hand the conversation to a human agent: discount or price negotiation, anything not in the database,
    legal/transfer questions, or complaints. Never promise a discount yourself."""
    ctx = runtime.context
    qid = db.create_approval(ctx.agency_id, ctx.lead_id, "escalation", reason, reason="needs a human")
    db.update_lead(ctx.agency_id, ctx.lead_id, score="hot")
    return (f"ESCALATED: ticket #{qid}. A human agent from {ctx.agency_name} will get back to the customer "
            f"shortly. Do not quote any discount or unlisted price.")


# ── long-term memory ──

@tool
def remember_fact(fact: str, runtime: ToolRuntime[DealContext]) -> str:
    """Save a lasting preference worth remembering in FUTURE conversations, e.g. 'prefers corner plots',
    'overseas Pakistani in Dubai', 'wants to move before Eid'. Never phone numbers, CNIC, income or salary."""
    ctx = runtime.context
    if parsing.contains_pii(fact):
        return tool_error("NOT_SAVED", "That contains personal/financial data (phone, CNIC, income, email), "
                                       "which we never store.")
    if runtime.store is None:
        return tool_error("NOT_SAVED", "Memory is unavailable right now.", retryable=True)
    ns = memory_namespace(ctx.agency_id, ctx.user_id)
    facts = runtime.store.get(ns, "facts")
    items = (facts.value.get("items", []) if facts else [])
    fact = fact.strip()[:200]
    if fact and fact not in items:
        items = (items + [fact])[-20:]
        runtime.store.put(ns, "facts", {"items": items})
    return f"SAVED: '{fact}' will be remembered next time."


@tool
def forget_my_data(runtime: ToolRuntime[DealContext]) -> str:
    """Delete everything remembered about this customer (preferences and history). Only when they ask."""
    ctx = runtime.context
    n = 0
    if runtime.store is not None:
        ns = memory_namespace(ctx.agency_id, ctx.user_id)
        for item in runtime.store.search(ns, limit=1000):
            runtime.store.delete(item.namespace, item.key)
            n += 1
    db.log_event(ctx.agency_id, ctx.user_id, "memory_forgotten", {"items": n})
    return f"FORGOTTEN: deleted {n} saved memory item(s). I won't use your past preferences any more."


TOOLS = [search_properties, get_property_details, calculate_installments, get_visit_slots, book_visit,
         schedule_followup, request_human_review, remember_fact, forget_my_data]
