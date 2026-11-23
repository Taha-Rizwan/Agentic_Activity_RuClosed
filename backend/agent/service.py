"""Runs the graph for one customer turn. Owns the checkpointer (short-term) and the store (long-term).

Production (DATABASE_URL = Supabase / Neon Postgres): PostgresSaver + PostgresStore, survives restarts.
Development: SQLite files.
"""
import os
import re
import sqlite3
import threading
import time

from langchain_core.callbacks import get_usage_metadata_callback
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.sqlite import SqliteStore
from langgraph.types import Command

from backend import config, db
from backend.agent.context import DealContext, memory_namespace, thread_key
from backend.agent.graph import build_graph, last_human_index, text_of
from backend.agent.llm import is_offline

_lock = threading.Lock()
_graph = None
_store = None

YES = re.compile(r"^\s*(yes|y|yeah|yep|confirm|confirmed|ok|okay|sure|haan|han|ji|jee|g|book it|go ahead|done)\b", re.I)
NO = re.compile(r"^\s*(no|n|nope|cancel|nahi|nahin|na|don'?t|stop)\b", re.I)


def get_graph():
    global _graph, _store
    with _lock:
        if _graph is None:
            if db.is_postgres():
                # Production: Supabase / Neon Postgres. Survives restarts and redeploys.
                from langgraph.checkpoint.postgres import PostgresSaver
                from langgraph.store.postgres import PostgresStore

                checkpointer = PostgresSaver(db.pg_pool())
                _store = PostgresStore(db.pg_pool())
            else:
                # Development: a local SQLite file.
                path = os.getenv("MEMORY_DB", str(config.MEMORY_DB))
                checkpointer = SqliteSaver(sqlite3.connect(path, check_same_thread=False))
                _store = SqliteStore(sqlite3.connect(path, check_same_thread=False, isolation_level=None))
            checkpointer.setup()
            _store.setup()
            _graph = build_graph(checkpointer=checkpointer, store=_store)
        return _graph


def get_store():
    get_graph()
    return _store


def reset():
    """Used by tests/benchmark to point at fresh databases."""
    global _graph, _store
    with _lock:
        _graph = None
        _store = None


def _context(agency: dict, user_id: str, lead_id: int, thread_id: str, confirm: bool = True) -> DealContext:
    return DealContext(agency_id=agency["id"], agency_name=agency["name"], user_id=user_id, lead_id=lead_id,
                       thread_id=thread_id, confirm_bookings=confirm)


def _pending(snap):
    for task in snap.tasks:
        for intr in task.interrupts:
            return intr.value
    return None


def _result(graph, cfg: dict, thread_id: str, agency_id: int, lead_id: int, usage: dict | None = None) -> dict:
    snap = graph.get_state(cfg)
    messages = snap.values.get("messages", [])
    li = last_human_index(messages)
    turn = messages[li + 1:] if li >= 0 else messages
    calls = [{"name": c["name"], "args": c.get("args", {})} for m in turn if isinstance(m, AIMessage) for c in m.tool_calls]
    pending = _pending(snap)
    if pending:
        reply = f"{pending.get('question', 'Please confirm.')} Reply \"yes\" to confirm or \"no\" to cancel."
        if not any(c["name"] == "book_visit" for c in calls):
            calls.append({"name": "book_visit", "args": {k: pending.get(k) for k in ("property_id", "slot")}})
    else:
        last = messages[-1] if messages else None
        reply = text_of(last) if isinstance(last, AIMessage) else ""
    if reply:
        db.log_message(agency_id, lead_id, thread_id, "assistant", reply)
    tin = tout = 0
    for u in (usage or {}).values():
        tin += int(u.get("input_tokens", 0) or 0)
        tout += int(u.get("output_tokens", 0) or 0)
    profile = {k: v for k, v in (snap.values.get("profile") or {}).items() if k not in ("name", "phone")}
    return {"reply": reply, "interrupt": pending, "tool_calls": calls,
            "tools_used": list(dict.fromkeys(c["name"] for c in calls)), "thread_id": thread_id,
            "blocked": bool(snap.values.get("blocked")), "block_reason": snap.values.get("block_reason") or None,
            "profile": profile, "tokens": {"in": tin, "out": tout}, "offline_model": is_offline()}


def chat(agency: dict, user_id: str, session: str, message: str, source: str = "website",
         confirm_bookings: bool = True) -> dict:
    graph = get_graph()
    lead = db.get_or_create_lead(agency["id"], user_id, source=source)
    thread_id = thread_key(agency["id"], user_id, session)
    cfg = {"configurable": {"thread_id": thread_id}}
    ctx = _context(agency, user_id, lead["id"], thread_id, confirm_bookings)
    db.log_message(agency["id"], lead["id"], thread_id, "user", message)
    t0 = time.perf_counter()
    with get_usage_metadata_callback() as cb:
        _run_turn(graph, cfg, ctx, message)
        result = _result(graph, cfg, thread_id, agency["id"], lead["id"], cb.usage_metadata)
    _log_run(agency["id"], user_id, thread_id, result, t0)
    return result


def _run_turn(graph, cfg: dict, ctx: DealContext, message: str) -> None:
    # A booking is waiting for the customer's yes/no: a plain-text answer resumes it.
    if _pending(graph.get_state(cfg)):
        if YES.match(message):
            graph.invoke(Command(resume={"approved": True}), cfg, context=ctx)
            return
        graph.invoke(Command(resume={"approved": False}), cfg, context=ctx)
        if NO.match(message) and len(message.split()) <= 3:
            return
    graph.invoke({"messages": [HumanMessage(message[:2000])]}, cfg, context=ctx)


def _log_run(agency_id: int, user_id: str, thread_id: str, result: dict, t0: float) -> None:
    """LLMOps trace (Lectures 3-5): every run records model + prompt/guard/tool versions, tokens, tools and latency."""
    try:
        db.log_event(agency_id, user_id, "agent_run", {
            "model": "offline" if result.get("offline_model") else config.MODEL,
            "prompt_version": config.PROMPT_VERSION, "guard_version": config.GUARD_VERSION,
            "tools_version": config.TOOLS_VERSION, "thread_id": thread_id, "tokens": result.get("tokens"),
            "tools": result.get("tools_used"), "blocked": result.get("block_reason"),
            "latency_ms": round((time.perf_counter() - t0) * 1000)})
    except Exception:
        pass


def resume(agency: dict, user_id: str, session: str, approved: bool) -> dict:
    graph = get_graph()
    lead = db.get_or_create_lead(agency["id"], user_id)
    thread_id = thread_key(agency["id"], user_id, session)
    cfg = {"configurable": {"thread_id": thread_id}}
    if not _pending(graph.get_state(cfg)):
        return {**_result(graph, cfg, thread_id, agency["id"], lead["id"]), "reply": "Nothing is waiting for confirmation."}
    db.log_message(agency["id"], lead["id"], thread_id, "user", "[confirmed booking]" if approved else "[declined booking]")
    with get_usage_metadata_callback() as cb:
        graph.invoke(Command(resume={"approved": bool(approved)}), cfg,
                     context=_context(agency, user_id, lead["id"], thread_id))
        return _result(graph, cfg, thread_id, agency["id"], lead["id"], cb.usage_metadata)


def history(agency: dict, user_id: str, session: str) -> list[dict]:
    graph = get_graph()
    snap = graph.get_state({"configurable": {"thread_id": thread_key(agency["id"], user_id, session)}})
    out = []
    for m in snap.values.get("messages", []):
        if isinstance(m, HumanMessage) and not text_of(m).startswith("Here is a summary"):
            out.append({"role": "user", "content": text_of(m)})
        elif isinstance(m, AIMessage) and text_of(m) and not m.tool_calls:
            out.append({"role": "assistant", "content": text_of(m)})
    return out


def memories(agency_id: int, user_id: str) -> dict:
    store = get_store()
    return {i.key: i.value for i in store.search(memory_namespace(agency_id, user_id), limit=50)}


def forget(agency_id: int, user_id: str) -> int:
    store = get_store()
    items = store.search(memory_namespace(agency_id, user_id), limit=1000)
    for i in items:
        store.delete(i.namespace, i.key)
    return len(items)
