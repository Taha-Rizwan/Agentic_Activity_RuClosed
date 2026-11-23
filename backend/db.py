"""SQLite business database. Every table carries agency_id and every helper filters by it (multi-tenancy).

All listing data is SYNTHETIC demo data.
"""
import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from backend import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS agencies (
    id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL, city TEXT,
    tagline TEXT, phone TEXT, plan TEXT DEFAULT 'monthly', created_at TEXT
);
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY, agency_id INTEGER NOT NULL REFERENCES agencies(id),
    email TEXT UNIQUE NOT NULL, salt TEXT NOT NULL, password_hash TEXT NOT NULL, name TEXT
);
CREATE TABLE IF NOT EXISTS properties (
    id INTEGER PRIMARY KEY, agency_id INTEGER NOT NULL REFERENCES agencies(id),
    title TEXT NOT NULL, area TEXT NOT NULL, block TEXT, city TEXT, property_type TEXT NOT NULL,
    size_marla REAL NOT NULL, price BIGINT NOT NULL, bedrooms INTEGER, description TEXT,
    installments INTEGER DEFAULT 0, min_down_payment_pct REAL DEFAULT 25, max_months INTEGER DEFAULT 36,
    status TEXT DEFAULT 'available', created_at TEXT
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY, agency_id INTEGER NOT NULL REFERENCES agencies(id), user_id TEXT NOT NULL,
    name TEXT, phone TEXT, source TEXT DEFAULT 'website', status TEXT DEFAULT 'new', score TEXT DEFAULT 'cold',
    budget_max BIGINT, area TEXT, size_marla REAL, property_type TEXT, timeline TEXT,
    wants_installments INTEGER DEFAULT 0, last_message TEXT, created_at TEXT, updated_at TEXT,
    UNIQUE(agency_id, user_id)
);
CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY, agency_id INTEGER NOT NULL REFERENCES agencies(id),
    lead_id INTEGER REFERENCES leads(id), property_id INTEGER REFERENCES properties(id),
    slot TEXT NOT NULL, name TEXT, phone TEXT, status TEXT DEFAULT 'booked', fee_pkr INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY, agency_id INTEGER NOT NULL REFERENCES agencies(id), lead_id INTEGER,
    kind TEXT NOT NULL, message TEXT, reason TEXT, status TEXT DEFAULT 'pending',
    created_at TEXT, decided_at TEXT, decided_by TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY, agency_id INTEGER NOT NULL, lead_id INTEGER, channel TEXT,
    message TEXT, approved_by TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, agency_id INTEGER NOT NULL, lead_id INTEGER, thread_id TEXT,
    role TEXT, content TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, agency_id INTEGER, user_id TEXT, kind TEXT, detail TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_prop_agency ON properties(agency_id);
CREATE INDEX IF NOT EXISTS ix_leads_agency ON leads(agency_id);
CREATE INDEX IF NOT EXISTS ix_visits_agency ON visits(agency_id);
CREATE INDEX IF NOT EXISTS ix_msgs_lead ON messages(agency_id, lead_id);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── one tiny adapter, two engines ──
# DATABASE_URL set (Supabase / Neon Postgres) -> Postgres with a connection pool (production).
# Not set -> a local SQLite file (development, tests). Same SQL for both ("?" placeholders).

def database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def is_postgres() -> bool:
    return database_url().startswith(("postgres://", "postgresql://"))


class Row(dict):
    """A dict row that also supports row[0] (like sqlite3.Row)."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self, data, rowcount, lastrowid):
        self._rows, self.rowcount, self.lastrowid = data, rowcount, lastrowid

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class Conn:
    def __init__(self, raw, pg: bool):
        self.raw, self.pg = raw, pg

    def execute(self, sql: str, args=()):
        if self.pg:
            sql = sql.replace("?", "%s")
            insert = sql.lstrip().upper().startswith("INSERT") and "RETURNING" not in sql.upper()
            if insert:
                sql += " RETURNING id"
            cur = self.raw.execute(sql, tuple(args))
            data = [Row(r) for r in cur.fetchall()] if cur.description else []
            lastrowid = data[0]["id"] if insert and data else None
            return Cursor([] if insert else data, cur.rowcount, lastrowid)
        cur = self.raw.execute(sql, tuple(args))
        data = [Row(dict(r)) for r in cur.fetchall()] if cur.description else []
        return Cursor(data, cur.rowcount, cur.lastrowid)

    def executescript(self, script: str):
        for stmt in script.split(";"):
            if stmt.strip():
                self.execute(stmt)


_pool = None


def pg_pool():
    """Shared by the business tables AND LangGraph's PostgresSaver / PostgresStore."""
    global _pool
    if _pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        _pool = ConnectionPool(database_url(), min_size=1, max_size=int(os.getenv("DB_POOL_SIZE", "8")), open=True,
                               kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
                               check=ConnectionPool.check_connection)
        import atexit

        atexit.register(_pool.close)
    return _pool


def _db_path():
    return os.getenv("APP_DB", str(config.APP_DB))


@contextmanager
def conn():
    if is_postgres():
        with pg_pool().connection() as raw:
            yield Conn(raw, True)
        return
    c = sqlite3.connect(_db_path(), check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield Conn(c, False)
        c.commit()
    finally:
        c.close()


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def one(cur) -> dict | None:
    r = cur.fetchone()
    return dict(r) if r else None


# ── formatting ──

def fmt_pkr(amount: float) -> str:
    """PKR in the units Pakistani buyers use: crore (1e7) and lakh (1e5)."""
    amount = float(amount)
    if amount >= 1e7:
        return f"PKR {amount / 1e7:.2f} crore".replace(".00 ", " ")
    if amount >= 1e5:
        return f"PKR {amount / 1e5:.1f} lakh".replace(".0 ", " ")
    return f"PKR {amount:,.0f}"


# ── auth helpers ──

def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()


def verify_admin(email: str, password: str) -> dict | None:
    with conn() as c:
        admin = one(c.execute("SELECT * FROM admins WHERE email = ?", (email.strip().lower(),)))
    if not admin:
        return None
    if not secrets.compare_digest(hash_password(password, admin["salt"]), admin["password_hash"]):
        return None
    return admin


# ── agencies ──

def get_agency_by_slug(slug: str) -> dict | None:
    with conn() as c:
        return one(c.execute("SELECT id, slug, name, city, tagline, phone FROM agencies WHERE slug = ?", (slug,)))


def get_agency(agency_id: int) -> dict | None:
    with conn() as c:
        return one(c.execute("SELECT id, slug, name, city, tagline, phone FROM agencies WHERE id = ?", (agency_id,)))


def list_agencies() -> list[dict]:
    with conn() as c:
        return rows(c.execute("SELECT id, slug, name, city, tagline FROM agencies ORDER BY id"))


# ── properties ──

def search_properties(agency_id: int, area: str | None = None, property_type: str | None = None,
                      min_price: int | None = None, max_price: int | None = None,
                      size_marla: float | None = None, installments_only: bool = False,
                      limit: int = 5) -> list[dict]:
    q = "SELECT * FROM properties WHERE agency_id = ? AND status = 'available'"
    args: list = [agency_id]
    if area:
        q += " AND (LOWER(area) LIKE ? OR LOWER(block) LIKE ? OR LOWER(title) LIKE ?)"
        like = f"%{area.lower().strip()}%"
        args += [like, like, like]
    if property_type:
        q += " AND LOWER(property_type) = ?"
        args.append(property_type.lower().strip())
    if min_price:
        q += " AND price >= ?"
        args.append(int(min_price))
    if max_price:
        q += " AND price <= ?"
        args.append(int(max_price))
    if size_marla:
        q += " AND size_marla BETWEEN ? AND ?"
        args += [float(size_marla) * 0.9, float(size_marla) * 1.1]
    if installments_only:
        q += " AND installments = 1"
    q += " ORDER BY price ASC LIMIT ?"
    args.append(max(1, min(int(limit), 20)))
    with conn() as c:
        return rows(c.execute(q, args))


def get_property(agency_id: int, property_id: int) -> dict | None:
    with conn() as c:
        return one(c.execute("SELECT * FROM properties WHERE agency_id = ? AND id = ?", (agency_id, property_id)))


def list_properties(agency_id: int, include_sold: bool = True) -> list[dict]:
    q = "SELECT * FROM properties WHERE agency_id = ?" + ("" if include_sold else " AND status = 'available'")
    with conn() as c:
        return rows(c.execute(q + " ORDER BY created_at DESC, id DESC", (agency_id,)))


def known_areas(agency_id: int | None = None) -> list[str]:
    with conn() as c:
        if agency_id is None:
            return [r[0] for r in c.execute("SELECT DISTINCT area FROM properties")]
        return [r[0] for r in c.execute("SELECT DISTINCT area FROM properties WHERE agency_id = ?", (agency_id,))]


PROPERTY_FIELDS = ["title", "area", "block", "city", "property_type", "size_marla", "price", "bedrooms",
                   "description", "installments", "min_down_payment_pct", "max_months", "status"]


def add_property(agency_id: int, data: dict) -> int:
    clean = {k: data.get(k) for k in PROPERTY_FIELDS if data.get(k) not in (None, "")}
    for req in ("title", "area", "property_type", "size_marla", "price"):
        if req not in clean:
            raise ValueError(f"missing field: {req}")
    clean["price"] = int(float(clean["price"]))
    clean["size_marla"] = float(clean["size_marla"])
    clean["property_type"] = str(clean["property_type"]).lower()
    if "installments" in clean:
        clean["installments"] = 1 if str(clean["installments"]).lower() in ("1", "true", "yes", "y") else 0
    cols = ["agency_id", "created_at"] + list(clean)
    vals = [agency_id, now()] + list(clean.values())
    with conn() as c:
        cur = c.execute(f"INSERT INTO properties ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        return cur.lastrowid


def set_property_status(agency_id: int, property_id: int, status: str) -> bool:
    with conn() as c:
        cur = c.execute("UPDATE properties SET status = ? WHERE agency_id = ? AND id = ?",
                        (status, agency_id, property_id))
        return cur.rowcount > 0


# ── leads ──

def get_or_create_lead(agency_id: int, user_id: str, source: str = "website") -> dict:
    with conn() as c:
        lead = one(c.execute("SELECT * FROM leads WHERE agency_id = ? AND user_id = ?", (agency_id, user_id)))
        if lead:
            return lead
        c.execute("INSERT INTO leads (agency_id, user_id, source, created_at, updated_at) VALUES (?,?,?,?,?)",
                  (agency_id, user_id, source, now(), now()))
        return one(c.execute("SELECT * FROM leads WHERE agency_id = ? AND user_id = ?", (agency_id, user_id)))


LEAD_FIELDS = {"name", "phone", "status", "score", "budget_max", "area", "size_marla", "property_type",
               "timeline", "wants_installments", "last_message", "source"}


def update_lead(agency_id: int, lead_id: int, **fields) -> None:
    fields = {k: v for k, v in fields.items() if k in LEAD_FIELDS and v is not None}
    if not fields:
        return
    fields["updated_at"] = now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE leads SET {sets} WHERE agency_id = ? AND id = ?", [*fields.values(), agency_id, lead_id])


def list_leads(agency_id: int) -> list[dict]:
    with conn() as c:
        return rows(c.execute(
            """SELECT l.*, (SELECT COUNT(*) FROM visits v WHERE v.lead_id = l.id AND v.agency_id = l.agency_id) AS visits,
                      (SELECT COUNT(*) FROM messages m WHERE m.lead_id = l.id AND m.agency_id = l.agency_id) AS messages
               FROM leads l WHERE l.agency_id = ? ORDER BY l.updated_at DESC""", (agency_id,)))


def get_lead(agency_id: int, lead_id: int) -> dict | None:
    with conn() as c:
        return one(c.execute("SELECT * FROM leads WHERE agency_id = ? AND id = ?", (agency_id, lead_id)))


def stale_leads(agency_id: int, hours: int = 24) -> list[dict]:
    """Leads with no visit booked and no activity for `hours` and no pending follow-up: follow-up candidates."""
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with conn() as c:
        return rows(c.execute(
            """SELECT * FROM leads l WHERE agency_id = ? AND status NOT IN ('visit_booked','closed','lost')
               AND updated_at <= ? AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.lead_id = l.id
               AND a.kind = 'followup' AND a.status = 'pending')""", (agency_id, cutoff)))


# ── visits ──

def booked_slots(agency_id: int, property_id: int) -> set[str]:
    with conn() as c:
        return {r[0] for r in c.execute(
            "SELECT slot FROM visits WHERE agency_id = ? AND property_id = ? AND status = 'booked'",
            (agency_id, property_id))}


def create_visit(agency_id: int, lead_id: int, property_id: int, slot: str, name: str, phone: str) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO visits (agency_id, lead_id, property_id, slot, name, phone, fee_pkr, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (agency_id, lead_id, property_id, slot, name, phone, config.PER_VISIT_PKR, now()))
        return cur.lastrowid


def list_visits(agency_id: int) -> list[dict]:
    with conn() as c:
        return rows(c.execute(
            """SELECT v.*, p.title AS property_title, p.area AS property_area FROM visits v
               JOIN properties p ON p.id = v.property_id AND p.agency_id = v.agency_id
               WHERE v.agency_id = ? ORDER BY v.slot ASC""", (agency_id,)))


def lead_visits(agency_id: int, lead_id: int) -> list[dict]:
    with conn() as c:
        return rows(c.execute(
            """SELECT v.*, p.title AS property_title FROM visits v JOIN properties p ON p.id = v.property_id
               WHERE v.agency_id = ? AND v.lead_id = ?""", (agency_id, lead_id)))


def set_visit_status(agency_id: int, visit_id: int, status: str) -> bool:
    with conn() as c:
        return c.execute("UPDATE visits SET status = ? WHERE agency_id = ? AND id = ?",
                         (status, agency_id, visit_id)).rowcount > 0


# ── approval queue (human in the loop for risky actions) ──

def create_approval(agency_id: int, lead_id: int | None, kind: str, message: str, reason: str = "") -> int:
    with conn() as c:
        return c.execute(
            "INSERT INTO approvals (agency_id, lead_id, kind, message, reason, created_at) VALUES (?,?,?,?,?,?)",
            (agency_id, lead_id, kind, message, reason, now())).lastrowid


def list_approvals(agency_id: int, status: str | None = None) -> list[dict]:
    q = """SELECT a.*, l.name AS lead_name, l.phone AS lead_phone FROM approvals a
           LEFT JOIN leads l ON l.id = a.lead_id AND l.agency_id = a.agency_id WHERE a.agency_id = ?"""
    args: list = [agency_id]
    if status:
        q += " AND a.status = ?"
        args.append(status)
    with conn() as c:
        return rows(c.execute(q + " ORDER BY a.created_at DESC", args))


def decide_approval(agency_id: int, approval_id: int, approve: bool, admin_email: str,
                    edited_message: str | None = None) -> dict | None:
    with conn() as c:
        item = one(c.execute("SELECT * FROM approvals WHERE agency_id = ? AND id = ? AND status = 'pending'",
                             (agency_id, approval_id)))
        if not item:
            return None
        message = edited_message or item["message"]
        status = "approved" if approve else "rejected"
        c.execute("UPDATE approvals SET status = ?, message = ?, decided_at = ?, decided_by = ? WHERE id = ?",
                  (status, message, now(), admin_email, approval_id))
        if approve and item["kind"] == "followup":
            # The actual "send". Real deployment: WhatsApp Business / SMS gateway. Here: the outbox.
            c.execute("INSERT INTO outbox (agency_id, lead_id, channel, message, approved_by, created_at) "
                      "VALUES (?,?,?,?,?,?)", (agency_id, item["lead_id"], "whatsapp", message, admin_email, now()))
            c.execute("UPDATE approvals SET status = 'sent' WHERE id = ?", (approval_id,))
            status = "sent"
        item.update(status=status, message=message)
        return item


def list_outbox(agency_id: int) -> list[dict]:
    with conn() as c:
        return rows(c.execute(
            """SELECT o.*, l.name AS lead_name FROM outbox o LEFT JOIN leads l ON l.id = o.lead_id
               WHERE o.agency_id = ? ORDER BY o.created_at DESC""", (agency_id,)))


def add_outbox(agency_id: int, lead_id: int, channel: str, message: str, approved_by: str = "auto-reply") -> None:
    with conn() as c:
        c.execute("INSERT INTO outbox (agency_id, lead_id, channel, message, approved_by, created_at) "
                  "VALUES (?,?,?,?,?,?)", (agency_id, lead_id, channel, message, approved_by, now()))


# ── conversation log + events (for the dashboard and the audit trail) ──

def log_message(agency_id: int, lead_id: int, thread_id: str, role: str, content: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO messages (agency_id, lead_id, thread_id, role, content, created_at) VALUES (?,?,?,?,?,?)",
                  (agency_id, lead_id, thread_id, role, content, now()))


def lead_messages(agency_id: int, lead_id: int) -> list[dict]:
    with conn() as c:
        return rows(c.execute("SELECT role, content, thread_id, created_at FROM messages "
                              "WHERE agency_id = ? AND lead_id = ? ORDER BY id", (agency_id, lead_id)))


def log_event(agency_id: int | None, user_id: str | None, kind: str, detail: dict | str = "") -> None:
    detail = detail if isinstance(detail, str) else json.dumps(detail, default=str)
    with conn() as c:
        c.execute("INSERT INTO events (agency_id, user_id, kind, detail, created_at) VALUES (?,?,?,?,?)",
                  (agency_id, user_id, kind, detail[:2000], now()))


def list_events(agency_id: int, limit: int = 100) -> list[dict]:
    with conn() as c:
        return rows(c.execute("SELECT * FROM events WHERE agency_id = ? ORDER BY id DESC LIMIT ?", (agency_id, limit)))


def stats(agency_id: int) -> dict:
    with conn() as c:
        def scalar(q, *a):
            return c.execute(q, (agency_id, *a)).fetchone()[0] or 0

        leads_by_score = {r[0]: r[1] for r in c.execute(
            "SELECT score, COUNT(*) FROM leads WHERE agency_id = ? GROUP BY score", (agency_id,))}
        leads_by_source = {r[0]: r[1] for r in c.execute(
            "SELECT source, COUNT(*) FROM leads WHERE agency_id = ? GROUP BY source", (agency_id,))}
        leads_by_day = rows(c.execute(
            "SELECT substr(created_at,1,10) AS day, COUNT(*) AS n FROM leads WHERE agency_id = ? "
            "GROUP BY day ORDER BY day DESC LIMIT 14", (agency_id,)))
        tools = {r[0]: r[1] for r in c.execute(
            "SELECT detail, COUNT(*) FROM events WHERE agency_id = ? AND kind = 'tool_call' GROUP BY detail",
            (agency_id,))}
        total_leads = scalar("SELECT COUNT(*) FROM leads WHERE agency_id = ?")
        visits = scalar("SELECT COUNT(*) FROM visits WHERE agency_id = ? AND status != 'cancelled'")
        booked_leads = scalar("SELECT COUNT(DISTINCT lead_id) FROM visits WHERE agency_id = ? AND status != 'cancelled'")
        return {
            "total_leads": total_leads,
            "leads_by_score": leads_by_score,
            "leads_by_source": leads_by_source,
            "leads_by_day": list(reversed(leads_by_day)),
            "visits_booked": visits,
            "conversion_pct": round(100 * booked_leads / total_leads, 1) if total_leads else 0.0,
            "listings_available": scalar("SELECT COUNT(*) FROM properties WHERE agency_id = ? AND status='available'"),
            "pending_approvals": scalar("SELECT COUNT(*) FROM approvals WHERE agency_id = ? AND status='pending'"),
            "messages_sent": scalar("SELECT COUNT(*) FROM outbox WHERE agency_id = ?"),
            "guard_blocks": scalar("SELECT COUNT(*) FROM events WHERE agency_id = ? AND kind = 'guard_block'"),
            "conversations": scalar("SELECT COUNT(DISTINCT thread_id) FROM messages WHERE agency_id = ?"),
            "tool_calls": tools,
            "revenue": {
                "subscription_pkr": config.SUBSCRIPTION_PKR,
                "visit_fees_pkr": visits * config.PER_VISIT_PKR,
                "total_pkr": config.SUBSCRIPTION_PKR + visits * config.PER_VISIT_PKR,
            },
        }


# ── seed (synthetic demo data) ──

AGENCIES = [
    {"id": 1, "slug": "capital-estates", "name": "Capital Estates", "city": "Islamabad / Rawalpindi",
     "tagline": "Plots and homes in Bahria Town, DHA and Gulberg Greens", "phone": "051-0000000 (demo)",
     "admin": ("admin@capital.demo", "demo123", "Ayesha (Capital Estates)")},
    {"id": 2, "slug": "lahore-homes", "name": "Lahore Homes", "city": "Lahore",
     "tagline": "DHA, Bahria Town Lahore and Johar Town specialists", "phone": "042-0000000 (demo)",
     "admin": ("admin@lahore.demo", "demo123", "Bilal (Lahore Homes)")},
]

C = 10_000_000  # 1 crore
L = 100_000     # 1 lakh

PROPERTIES = [
    # id, agency, title, area, block, city, type, marla, price, beds, desc, inst, min_dp, months
    (514, 1, "10 Marla Plot, Bahria Town Phase 8", "Bahria Town", "Phase 8, Block F", "Rawalpindi", "plot", 10, int(1.85 * C), None,
     "Corner-adjacent residential plot near the park, possession ready, all dues clear.", 1, 25, 36),
    (515, 1, "10 Marla Plot, Bahria Town Phase 8 (Overseas Block)", "Bahria Town", "Phase 8, Overseas 6", "Rawalpindi", "plot", 10, int(1.62 * C), None,
     "Level plot on a 40 ft street, development complete, on installments.", 1, 30, 24),
    (516, 1, "10 Marla Plot, Bahria Town Phase 7", "Bahria Town", "Phase 7", "Rawalpindi", "plot", 10, int(2.35 * C), None,
     "Prime location near Spring North commercial, possession and map approved.", 0, 100, 0),
    (517, 1, "5 Marla House, Bahria Town Phase 8", "Bahria Town", "Phase 8, Usman Block", "Rawalpindi", "house", 5, int(1.65 * C), 3,
     "Brand new double-storey house, 3 beds, 4 baths, near mosque and school.", 0, 100, 0),
    (518, 1, "1 Kanal House, Bahria Town Phase 4", "Bahria Town", "Phase 4", "Rawalpindi", "house", 20, int(6.5 * C), 5,
     "Luxury 5-bed house with basement, solar system and lawn.", 0, 100, 0),
    (519, 1, "8 Marla Plot, Bahria Enclave", "Bahria Enclave", "Sector C1", "Islamabad", "plot", 8, int(1.1 * C), None,
     "Mountain-view plot, easy 3-year installment plan.", 1, 20, 36),
    (520, 1, "1 Kanal Plot, DHA Phase 2", "DHA", "Phase 2, Sector J", "Islamabad", "plot", 20, int(5.8 * C), None,
     "Main boulevard approach, possession plot.", 0, 100, 0),
    (521, 1, "10 Marla House, DHA Phase 2", "DHA", "Phase 2, Sector A", "Islamabad", "house", 10, int(4.75 * C), 4,
     "Designer house, 4 beds, fully furnished kitchen.", 0, 100, 0),
    (522, 1, "7 Marla Plot, Gulberg Greens", "Gulberg Greens", "Block C", "Islamabad", "plot", 7, int(95 * L), None,
     "Residential plot near Islamabad Expressway, installments available.", 1, 25, 30),
    (523, 1, "2-Bed Apartment, Gulberg Greens", "Gulberg Greens", "Business Square", "Islamabad", "apartment", 4.5, int(85 * L), 2,
     "Ready apartment on 6th floor with lift and backup.", 1, 30, 24),
    (524, 1, "5 Marla Plot, B-17 Multi Gardens", "B-17", "Block E", "Islamabad", "plot", 5, int(62 * L), None,
     "Affordable plot on easy installments.", 1, 20, 36),
    (525, 1, "4 Marla Commercial, Bahria Town Phase 8", "Bahria Town", "Phase 8, Business District", "Rawalpindi", "commercial", 4, int(3.4 * C), None,
     "Commercial plot facing main boulevard, ideal for plaza.", 0, 100, 0),
    (531, 2, "10 Marla Plot, DHA Phase 9 Prism", "DHA", "Phase 9 Prism, Block K", "Lahore", "plot", 10, int(1.95 * C), None,
     "Plot near ring road access, file with possession.", 1, 30, 24),
    (532, 2, "10 Marla Plot, Bahria Town Lahore", "Bahria Town", "Sector E", "Lahore", "plot", 10, int(1.45 * C), None,
     "Developed plot near Eiffel Tower landmark.", 1, 25, 36),
    (533, 2, "5 Marla House, Johar Town", "Johar Town", "Block R", "Lahore", "house", 5, int(2.1 * C), 3,
     "Renovated house near Emporium Mall.", 0, 100, 0),
    (534, 2, "1 Kanal House, DHA Phase 6", "DHA", "Phase 6, Block C", "Lahore", "house", 20, int(9.5 * C), 5,
     "Modern house with pool, double unit.", 0, 100, 0),
    (535, 2, "8 Marla Plot, Bahria Orchard", "Bahria Orchard", "Phase 2", "Lahore", "plot", 8, int(75 * L), None,
     "Low-cost plot with 3-year plan.", 1, 20, 36),
]

DEMO_LEADS = [
    # agency, user, name, phone, source, status, score, budget, area, marla, type, timeline, inst, msg, days ago
    (1, "demo-zameen-1", "Usman (demo)", "0300-0000001", "zameen", "qualified", "hot", 2 * C, "Bahria Town", 10, "plot", "this month", 1,
     "Looking for 10 marla in Bahria, can pay 25% now", 2),
    (1, "demo-olx-1", "Hina (demo)", "0300-0000002", "olx", "new", "warm", 1 * C, "Gulberg Greens", 7, "plot", "3 months", 1,
     "Any 7 marla plots on installments?", 3),
    (1, "demo-web-1", None, None, "website", "new", "cold", None, "DHA", None, "house", None, 0,
     "price of DHA houses?", 1),
    (2, "demo-lhr-1", "Kamran (demo)", "0300-0000003", "zameen", "qualified", "hot", int(2.2 * C), "DHA", 10, "plot", "this week", 1,
     "10 marla DHA phase 9", 1),
]


def init_db(seed: bool = True) -> None:
    with conn() as c:
        c.executescript(SCHEMA.replace("INTEGER PRIMARY KEY", "BIGSERIAL PRIMARY KEY") if c.pg else SCHEMA)
        if not seed or c.execute("SELECT COUNT(*) FROM agencies").fetchone()[0]:
            return
        for a in AGENCIES:
            c.execute("INSERT INTO agencies (id, slug, name, city, tagline, phone, created_at) VALUES (?,?,?,?,?,?,?)",
                      (a["id"], a["slug"], a["name"], a["city"], a["tagline"], a["phone"], now()))
            email, pw, name = a["admin"]
            salt = secrets.token_hex(8)
            c.execute("INSERT INTO admins (agency_id, email, salt, password_hash, name) VALUES (?,?,?,?,?)",
                      (a["id"], email, salt, hash_password(pw, salt), name))
        for p in PROPERTIES:
            c.execute(
                "INSERT INTO properties (id, agency_id, title, area, block, city, property_type, size_marla, price, "
                "bedrooms, description, installments, min_down_payment_pct, max_months, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*p, now()))
        for (ag, uid, name, phone, src, status, score, budget, area, marla, ptype, timeline, inst, msg, ago) in DEMO_LEADS:
            ts = (datetime.now() - timedelta(days=ago)).strftime("%Y-%m-%d %H:%M:%S")
            cur = c.execute(
                "INSERT INTO leads (agency_id, user_id, name, phone, source, status, score, budget_max, area, size_marla, "
                "property_type, timeline, wants_installments, last_message, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ag, uid, name, phone, src, status, score, budget, area, marla, ptype, timeline, inst, msg, ts, ts))
            lead_id = cur.lastrowid
            c.execute("INSERT INTO messages (agency_id, lead_id, thread_id, role, content, created_at) VALUES (?,?,?,?,?,?)",
                      (ag, lead_id, f"{ag}:{uid}:seed", "user", msg, ts))
        if c.pg:  # explicit ids were inserted: move the sequences past them
            for table in ("agencies", "properties"):
                c.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT MAX(id) FROM {table}))")
