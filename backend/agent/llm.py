"""Model factory.

- Production: ChatAnthropic with the model the brief requires (claude-haiku-5-5), key from the environment.
- Offline: a small rule-based chat model with the same interface (bind_tools + tool calls).
  It lets tests, CI and the benchmark harness run without an API key or spend, and keeps the demo
  alive if the key's spend limit is hit. It is clearly labelled as offline in the API responses.
"""
import json
import re
from typing import Any
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from backend import config
from backend.agent import parsing


def error_code(result: str):
    from backend.agent.tools import error_code as _code  # lazy: tools imports llm indirectly
    return _code(result)

_model = None


def get_model():
    global _model
    if _model is None:
        if config.OFFLINE_MODE:
            _model = OfflineDealModel()
        else:
            from langchain_anthropic import ChatAnthropic

            _model = ChatAnthropic(model=config.MODEL, max_tokens=600, max_retries=2,
                                   timeout=30, api_key=config.ANTHROPIC_API_KEY)
    return _model


def reset_model():
    global _model
    _model = None


def is_offline() -> bool:
    return isinstance(get_model(), OfflineDealModel)


BOOK = re.compile(r"\b(book|schedule|reserve|site visit|visit|see it|see the|viewing|inspect|come and see)\b", re.I)
SEARCH = re.compile(r"\b(show|available|options?|anything|new|recommend|suggest|looking|buy|want|need|find|search|"
                    r"market|plots?|houses?|apartments?|flats?|listings?|under|budget)\b", re.I)
DETAILS = re.compile(r"\b(details?|tell me (more )?about|more about|info|information|describe)\b", re.I)
FOLLOWUP = re.compile(r"\b(call me|contact me|follow ?up|remind me|get back to me|message me|whatsapp me)\b", re.I)


def _call(name: str, args: dict) -> dict:
    return {"name": name, "args": {k: v for k, v in args.items() if v is not None}, "id": f"call_{uuid4().hex[:10]}",
            "type": "tool_call"}


class OfflineDealModel(BaseChatModel):
    """Deterministic planner: reads the lead profile the sales node puts in the system prompt."""

    @property
    def _llm_type(self) -> str:
        return "ruclosed-offline"

    def bind_tools(self, tools: Any, **kwargs: Any):
        return self

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        msg = self._respond(messages)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    # ── planning ──
    def _respond(self, messages: list[BaseMessage]) -> AIMessage:
        system = next((m.content for m in messages if isinstance(m, SystemMessage)), "")
        prompt = next((str(m.content) for m in messages if "SUMMARISE_CONVERSATION" in str(m.content)), "")
        if prompt:  # summarisation request from SummarizationMiddleware
            body = prompt.split("<messages>")[-1]
            said = [x.strip() for x in re.findall(r"(?im)^\s*(?:human|user)\s*:\s*(.{3,160})$", body)
                    if not x.lower().startswith("here is a summary")]
            said = [re.sub(r"</?message>", "", x) for x in said]
            return AIMessage(content="Earlier the customer said: " + parsing.redact(" | ".join(said))[:600])
        prof_m = re.search(r"PROFILE_JSON: (\{.*\})", system)
        profile = json.loads(prof_m.group(1)) if prof_m else {}
        last_h = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
        human = str(messages[last_h].content) if last_h >= 0 else ""
        turn = messages[last_h + 1:]
        results = {m.name: str(m.content) for m in turn if isinstance(m, ToolMessage)}
        if not results:
            calls = self._plan(human, profile)
            if calls:
                return AIMessage(content="", tool_calls=calls)
            return AIMessage(content=self._qualify(human, profile))
        # second step: installments for the top search result if the customer asked for them
        if ("search_properties" in results and "calculate_installments" not in results
                and profile.get("wants_installments") and error_code(results["search_properties"]) != "NO_MATCH"):
            m = re.search(r"#(\d+)", results["search_properties"])
            if m:
                return AIMessage(content="", tool_calls=[_call("calculate_installments", {
                    "property_id": int(m.group(1)), "down_payment_pct": parsing.parse_down_payment_pct(human),
                    "months": parsing.parse_months(human) if parsing.wants_installments(human) else None})])
        return AIMessage(content=self._compose(human, profile, results))

    def _plan(self, text: str, p: dict) -> list[dict]:
        pid = parsing.parse_property_id(text) or (p.get("last_property_id") if not parsing.parse_area(text) else None)
        slot = parsing.parse_slot(text)
        if FOLLOWUP.search(text):
            return [_call("schedule_followup", {"message": f"Hi{(' ' + p['name']) if p.get('name') else ''}, following up "
                          f"on your property search as requested. Are you free to talk about the options?",
                          "when": parsing.parse_slot(text) or ("later" if "later" in text.lower() else None)})]
        if pid and (BOOK.search(text) or (slot and p.get("awaiting_slot"))):
            if slot:
                return [_call("book_visit", {"property_id": pid, "slot": slot,
                                             "name": parsing.parse_name(text) or p.get("name"),
                                             "phone": parsing.parse_phone(text) or p.get("phone")})]
            return [_call("get_visit_slots", {"property_id": pid})]
        if pid and parsing.wants_installments(text):
            return [_call("calculate_installments", {"property_id": pid,
                                                      "down_payment_pct": parsing.parse_down_payment_pct(text),
                                                      "months": parsing.parse_months(text)})]
        if parsing.parse_property_id(text) and (DETAILS.search(text) or len(text.split()) < 8):
            return [_call("get_property_details", {"property_id": pid})]
        found = parsing.extract_profile(text)
        has_filter = any(k in found for k in ("area", "size_marla", "budget_max", "property_type"))
        if has_filter or (SEARCH.search(text) and any(p.get(k) for k in ("area", "size_marla", "budget_max"))):
            return [_call("search_properties", {
                "area": p.get("area"), "property_type": p.get("property_type"),
                "max_price_pkr": p.get("budget_max"), "min_price_pkr": p.get("budget_min"),
                "size_marla": p.get("size_marla"), "installments_only": bool(p.get("wants_installments"))})]
        return []

    def _qualify(self, text: str, p: dict) -> str:
        if parsing.parse_name(text) or parsing.parse_phone(text):
            who = f" {p['name']}" if p.get("name") else ""
            nxt = (f"Would you like to visit #{p['last_property_id']}? Tell me a day and time."
                   if p.get("last_property_id") else "Which listing would you like to visit, and what day and time suits you?")
            return f"Thanks{who}, I've noted your contact details for our agent. {nxt}"
        missing = [label for key, label in (("area", "which area (e.g. Bahria Town, DHA, Gulberg Greens)"),
                                            ("budget_max", "your budget"), ("size_marla", "the size in marla/kanal"))
                   if not p.get(key)]
        if missing:
            return ("Happy to help you find the right property! Could you tell me " + ", ".join(missing[:2]) +
                    "? For example: \"10 marla plot in Bahria on installments\".")
        return "Would you like me to show matching listings, work out installments, or book a site visit?"

    def _compose(self, text: str, p: dict, r: dict) -> str:
        parts = []
        if "search_properties" in r:
            s = r["search_properties"]
            if error_code(s) == "NO_MATCH":
                parts.append("I couldn't find a listing matching that exactly in our database. Would you like me to "
                             "widen the area or budget, or should I log your requirement for an agent?")
            else:
                intro = "Here are real listings from our database that match:\n"
                if s.startswith("No "):
                    head, s = s.split("\n", 1)
                    intro = ("I couldn't find an exact match in our database, but here are the closest real options "
                             "(a little outside your budget or size):\n")
                if p.get("from_memory") and not parsing.extract_profile(text):
                    intro = (f"Welcome back! Based on what you were looking for last time "
                             f"({p.get('size_marla', '') and str(p['size_marla']).rstrip('0').rstrip('.') + ' marla '}"
                             f"{p.get('property_type') or 'property'} in {p.get('area') or 'your area'}), here's what's available now:\n")
                parts.append(intro + s)
        if "get_property_details" in r:
            d = r["get_property_details"]
            parts.append(json.loads(d)["message"] if error_code(d) else d)
        if "calculate_installments" in r:
            parts.append("Installment plan: " + r["calculate_installments"])
        if "get_visit_slots" in r:
            parts.append(r["get_visit_slots"] + "\nWhich slot suits you? Please share your name and mobile number too.")
        if "book_visit" in r:
            b = r["book_visit"]
            if error_code(b) == "NEED_CONTACT":
                parts.append("To book the visit I need your name and mobile number, please.")
            elif error_code(b) == "SLOT_UNAVAILABLE":
                parts.append("That time isn't free. Free slots: " + ", ".join(json.loads(b).get("free_slots", [])))
            else:
                parts.append(b.replace("BOOKED: ", "Done! "))
        if "schedule_followup" in r:
            parts.append("Sure, I've asked one of our agents to follow up with you. They'll message you shortly.")
        if "request_human_review" in r:
            parts.append("Prices and any discount are set by our agents, so I've passed your request to a human "
                         "agent who will get back to you shortly. I can't promise a discount myself.")
        if "forget_my_data" in r:
            parts.append("Done. I've deleted your saved preferences and history.")
        if "search_properties" in r and error_code(r["search_properties"]) != "NO_MATCH":
            parts.append("Would you like to book a site visit for any of these?" if p.get("timeline")
                         else "When are you planning to buy, and would you like to book a site visit?")
        return "\n\n".join(parts) or "Done."
