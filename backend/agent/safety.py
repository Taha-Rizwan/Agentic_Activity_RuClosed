"""CUSTOM: our safety policy, enforced in code by the guard node (not left to the prompt).

The guard runs BEFORE any model call. Blocked messages never reach the model or a tool.
"""
import re

INJECTION = re.compile(
    r"ignore (all |any |the |your )?(previous |prior |above |earlier )?(instructions|rules|prompts?)"
    r"|disregard (all |your |the )?(previous |prior )?(instructions|rules)"
    r"|forget (all |your )?(previous |prior )?(instructions|rules)"
    r"|(reveal|show|print|repeat|leak|tell me) (me )?(your |the )?(system|hidden|initial|original) (prompt|instructions|message)"
    r"|system prompt|you are now|act as (a |an )?(dan|developer|admin|unrestricted)|developer mode|jailbreak"
    r"|\bdan\b mode|pretend (you are|to be) (an? )?(admin|developer|unrestricted|different)"
    r"|new instructions:|<\s*/?\s*system\s*>|\[\s*system\s*\]|override (your|the) (rules|instructions|safety)",
    re.I)

DATA_EXFIL = re.compile(
    r"(other|all|another|previous|existing|your)\s+(clients?|customers?|leads?|buyers?|users?)('s|s')?\s*"
    r"(contact|phone|mobile|numbers?|details?|data|names?|info|information|cnic|emails?|list)"
    r"|(contacts?|phones?|mobiles?|numbers?|details?|cnics?|emails?|address(es)?|names?)\s*(numbers?|details?)?\s*(of|for)\s*(your\s*)?(the\s*)?(other|all|previous|existing|past)?\s*(clients?|customers?|leads?|buyers?|sellers?|owners?|users?)"
    r"|client contact numbers|(dump|export|list) (the |your )?(database|db|table|leads)"
    r"|admin (password|token|login)|api[_ ]?key|other agenc(y|ies)'?s? (data|leads|clients)"
    r"|(who else|which other (people|clients|customers)) (is |are )?(interested|booked|looking)",
    re.I)

# Always out of scope, even if real-estate words appear ("write python code for a real estate agent").
HARD_OFF_TOPIC = re.compile(
    r"\b(write|generate|give me|create|debug|fix)\b.{0,40}\b(code|script|program|function|python|javascript|java|sql|html|essay|poem|story|song|assignment|homework)\b"
    r"|\b(python|javascript|c\+\+|java)\s+(code|script|program)\b",
    re.I)

# Out of scope unless the message is also clearly about property.
SOFT_OFF_TOPIC = re.compile(
    r"\b(weather|cricket|football|match score|recipe|cook|movie|song|joke|politic|election|prime minister|"
    r"religion|stock market|crypto|bitcoin|translate|homework|math|equation|capital of|who won|news|"
    r"medical|doctor|diagnos|medicine|girlfriend|boyfriend|hack|password)\b", re.I)

DOMAIN = re.compile(
    r"\b(plot|plots|house|home|apartment|flat|marla|kanal|crore|lakh|lac|property|properties|bahria|dha|"
    r"gulberg|johar|b-17|enclave|orchard|installments?|qist|visit|book|slot|price|budget|buy|rent|"
    r"possession|file|society|block|phase|sector|commercial|shop|agent|agency|down ?payment|site|"
    r"listing|bed|beds|bedroom|area|location|market|new|available|recommend)\b", re.I)

# Risky: model must not invent prices/discounts. Code forces a human review instead.
DISCOUNT = re.compile(r"\b(discount|lower (the )?price|reduce (the )?price|best price|final price|negotiat|"
                      r"concession|kam kar|less price|cheaper price|price cut|waive)\b", re.I)

FORGET = re.compile(r"\b(forget (me|everything|my (data|details|preferences|info))|delete (my|all my) "
                    r"(data|details|information|memory|preferences)|remove my (data|details))\b", re.I)

REFUSALS = {
    "injection": "I can't change how I work or share internal instructions. I'm here to help you find "
                 "property, check prices from our listings, work out installments and book site visits. "
                 "What are you looking for?",
    "data_exfil": "Sorry, I can't share other clients' details or any internal data. Every client's "
                  "information is private. I'm happy to help with your own property search or a site visit.",
    "off_topic": "I'm the property assistant for this agency, so I can only help with buying property: "
                 "listings, prices, installment plans and site visits. Is there a property I can help you find?",
    "empty": "Hi! Tell me what you're looking for, e.g. \"10 marla plot in Bahria under 2 crore on installments\".",
}


def check_message(text: str) -> tuple[str, str] | None:
    """Return (category, refusal) if the message must be blocked, else None."""
    t = (text or "").strip()
    if not t:
        return "empty", REFUSALS["empty"]
    if len(t) > 2000:
        return "injection", REFUSALS["injection"]
    if INJECTION.search(t):
        return "injection", REFUSALS["injection"]
    if DATA_EXFIL.search(t):
        return "data_exfil", REFUSALS["data_exfil"]
    if HARD_OFF_TOPIC.search(t):
        return "off_topic", REFUSALS["off_topic"]
    if SOFT_OFF_TOPIC.search(t) and not DOMAIN.search(t):
        return "off_topic", REFUSALS["off_topic"]
    return None


def risk_flag(text: str) -> str | None:
    """Messages that are allowed but must be routed to a human (code decides, not the model)."""
    if FORGET.search(text or ""):
        return "forget"
    if DISCOUNT.search(text or ""):
        return "discount"
    return None


def is_on_domain(text: str) -> bool:
    return bool(DOMAIN.search(text or ""))


GREETING = re.compile(r"^\s*(hi|hello|hey|salam|assalam[- ]?o[- ]?alaikum|aoa|good (morning|evening|afternoon)|"
                      r"thanks|thank you|ok|okay|yes|no|sure|great|cool)\b", re.I)


def is_greeting(text: str) -> bool:
    return bool(GREETING.match(text or ""))
