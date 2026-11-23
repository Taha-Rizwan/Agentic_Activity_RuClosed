"""Who is talking. Set by the server from a SIGNED token, never by the chat or the model.

Same idea as the workshop's CareContext: tools read the tenant and user from runtime.context,
so a prompt-injected tool argument can never point at another agency's or user's data.
"""
from dataclasses import dataclass


@dataclass
class DealContext:
    agency_id: int = 1
    agency_name: str = "Capital Estates"
    user_id: str = "demo-user"
    lead_id: int = 0
    thread_id: str = ""
    confirm_bookings: bool = True   # web widget: confirm card; graded /chat API: book on explicit request


def memory_namespace(agency_id: int, user_id: str) -> tuple:
    """Long-term memory is keyed by user_id, inside the agency (tenant)."""
    return ("memories", str(agency_id), user_id)


def thread_key(agency_id: int, user_id: str, session: str) -> str:
    """Short-term memory (checkpointer) key. Built server-side, so it always carries the caller's identity."""
    safe = "".join(ch for ch in (session or "main") if ch.isalnum() or ch in "-_")[:40] or "main"
    return f"{agency_id}:{user_id}:{safe}"
