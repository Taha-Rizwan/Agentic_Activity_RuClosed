"""For `langgraph dev` / LangGraph Studio: the server supplies its own checkpointer and store.
Set the context in Studio, e.g. {"agency_id": 1, "agency_name": "Capital Estates", "user_id": "studio", "lead_id": 1}."""
from backend import db
from backend.agent.graph import build_graph

db.init_db()
graph = build_graph()
