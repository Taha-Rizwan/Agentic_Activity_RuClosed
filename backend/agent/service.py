"""Runs the graph for one customer turn. Owns the checkpointer (short-term) and the store (long-term)."""
import os
import sqlite3
import threading

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


def get_graph():
    global _graph, _store
    with _lock:
        if _graph is None:
            path = os.getenv("MEMORY_DB", str(config.MEMORY_DB))
            cp_conn = sqlite3.connect(path, check_same_thread=False)
            st_conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            checkpointer = SqliteSaver(cp_conn)
            checkpointer.setup()
            _store = SqliteStore(st_conn)
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


def _context(agency: dict, user_id: str, lead_id: int, thread_id: str) -> DealContext:
    return DealContext(agency_id=agency["id"], agency_name=agency["name"], user_id=user_id, lead_id=lead_id,
                       thread_id=thread_id)


def _result(graph, config_: dict, thread_id: str, agency_id: int, lead_id: int) -> dict:
    snap = graph.get_state(config_)
    messages = snap.values.get("messages", [])
    li = last_human_index(messages)
    turn = messages[li + 1:] if li >= 0 else messages
    tools = [m.name for m in turn if isinstance(m, ToolMessage)]
    pending = None
    for task in snap.tasks:
        for intr in task.interrupts:
            pending = intr.value
    reply = ""
    if not pending:
        last = messages[-1] if messages else None
        reply = text_of(last) if isinstance(last, AIMessage) else ""
        if reply:
            db.log_message(agency_id, lead_id, thread_id, "assistant", reply)
    else:
        if isinstance(pending, dict) and pending.get("type") == "confirm_booking":
            tools = sorted(set(tools + ["book_visit"]))
    profile = {k: v for k, v in (snap.values.get("profile") or {}).items() if k not in ("name", "phone")}
    return {"reply": reply, "interrupt": pending, "tools_used": tools, "thread_id": thread_id,
            "blocked": bool(snap.values.get("blocked")), "block_reason": snap.values.get("block_reason") or None,
            "profile": profile, "offline_model": is_offline()}


def chat(agency: dict, user_id: str, session: str, message: str, source: str = "website") -> dict:
    graph = get_graph()
    lead = db.get_or_create_lead(agency["id"], user_id, source=source)
    thread_id = thread_key(agency["id"], user_id, session)
    cfg = {"configurable": {"thread_id": thread_id}}
    # A new message while a booking confirmation is pending counts as "not confirmed".
    snap = graph.get_state(cfg)
    if any(t.interrupts for t in snap.tasks):
        graph.invoke(Command(resume={"approved": False}), cfg,
                     context=_context(agency, user_id, lead["id"], thread_id))
    db.log_message(agency["id"], lead["id"], thread_id, "user", message)
    graph.invoke({"messages": [HumanMessage(message[:2000])]}, cfg,
                 context=_context(agency, user_id, lead["id"], thread_id))
    return _result(graph, cfg, thread_id, agency["id"], lead["id"])


def resume(agency: dict, user_id: str, session: str, approved: bool) -> dict:
    graph = get_graph()
    lead = db.get_or_create_lead(agency["id"], user_id)
    thread_id = thread_key(agency["id"], user_id, session)
    cfg = {"configurable": {"thread_id": thread_id}}
    snap = graph.get_state(cfg)
    if not any(t.interrupts for t in snap.tasks):
        return {**_result(graph, cfg, thread_id, agency["id"], lead["id"]), "reply": "Nothing is waiting for confirmation."}
    db.log_message(agency["id"], lead["id"], thread_id, "user", "[confirmed booking]" if approved else "[declined booking]")
    graph.invoke(Command(resume={"approved": bool(approved)}), cfg,
                 context=_context(agency, user_id, lead["id"], thread_id))
    return _result(graph, cfg, thread_id, agency["id"], lead["id"])


def history(agency: dict, user_id: str, session: str) -> list[dict]:
    graph = get_graph()
    snap = graph.get_state({"configurable": {"thread_id": thread_key(agency["id"], user_id, session)}})
    out = []
    for m in snap.values.get("messages", []):
        if isinstance(m, HumanMessage):
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
