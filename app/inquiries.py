from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from flask import abort, current_app, flash, g, redirect, render_template, request, url_for

from app.admin import admin
from app.checkout_details import validate_delivery
from app.persistence import PersistenceError, ServicesiteRepository, SQLiteDatabase
from app.web import public
from app.web_security import FormSecurityError, require_csrf


INQUIRY_SCHEMA_VERSION = 1
CONTACT_MAX_PER_HOUR = 5
CONTACT_TOPICS = {
    "service": "Service question",
    "order": "Existing order",
    "technical": "Technical question",
    "other": "Other",
}
JOIN_SPECIALTIES = {
    "vulnerability_research": "Vulnerability Researcher",
    "detection_engineering": "Detection Engineer",
    "infrastructure": "Infrastructure Specialist",
    "osint": "OSINT Analyst",
    "security_awareness": "Security Awareness Specialist",
    "payment_operations": "Monero / Payment Operations",
}


class InquiryValidationError(ValueError):
    """A public inquiry field failed validation."""


class InquiryRateLimitError(PersistenceError):
    """An account submitted too many contact requests in the current window."""


class InquirySchemaError(PersistenceError):
    """The additive inquiry schema has not been initialized or is unsupported."""


@dataclass(frozen=True)
class ContactInquiry:
    id: str
    customer_id: str
    username: str
    topic: str
    reply_method: str
    reply_address: str = field(repr=False)
    message: str = field(repr=False)
    created_at: datetime


@dataclass(frozen=True)
class JoinApplication:
    id: str
    customer_id: str
    username: str
    specialty: str
    reply_method: str
    reply_address: str = field(repr=False)
    skills: str = field(repr=False)
    tools: str = field(repr=False)
    motivation: str = field(repr=False)
    created_at: datetime


class InquiryStore:
    """Persistence for customer contact requests and one-time join applications."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def initialize(self) -> None:
        """Create additive inquiry tables after the main servicesite schema exists."""
        with self.database.transaction(immediate=True) as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            if "schema_meta" not in tables or "customer_accounts" not in tables:
                raise InquirySchemaError("the main servicesite database must be initialized first")

            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key=?",
                ("inquiries_schema_version",),
            ).fetchone()
            if version is not None and version["value"] != str(INQUIRY_SCHEMA_VERSION):
                raise InquirySchemaError("inquiry database schema version is unsupported")

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS contact_inquiries (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL REFERENCES customer_accounts(id) ON DELETE RESTRICT,
                    topic TEXT NOT NULL CHECK (topic IN ('service', 'order', 'technical', 'other')),
                    reply_method TEXT NOT NULL CHECK (reply_method IN ('email', 'telegram')),
                    reply_address TEXT NOT NULL CHECK (length(reply_address) BETWEEN 1 AND 254),
                    message TEXT NOT NULL CHECK (length(message) BETWEEN 10 AND 4000),
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS join_applications (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL UNIQUE REFERENCES customer_accounts(id) ON DELETE RESTRICT,
                    specialty TEXT NOT NULL CHECK (specialty IN (
                        'vulnerability_research', 'detection_engineering', 'infrastructure',
                        'osint', 'security_awareness', 'payment_operations'
                    )),
                    reply_method TEXT NOT NULL CHECK (reply_method IN ('email', 'telegram')),
                    reply_address TEXT NOT NULL CHECK (length(reply_address) BETWEEN 1 AND 254),
                    skills TEXT NOT NULL CHECK (length(skills) BETWEEN 20 AND 4000),
                    tools TEXT NOT NULL CHECK (length(tools) BETWEEN 1 AND 1000),
                    motivation TEXT NOT NULL CHECK (length(motivation) BETWEEN 20 AND 4000),
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_contact_inquiries_customer_created "
                "ON contact_inquiries(customer_id, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_contact_inquiries_created "
                "ON contact_inquiries(created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_join_applications_created "
                "ON join_applications(created_at)"
            )
            if version is None:
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                    ("inquiries_schema_version", str(INQUIRY_SCHEMA_VERSION)),
                )

    def is_ready(self) -> bool:
        connection = self.database.connect()
        try:
            try:
                _require_schema(connection)
            except (InquirySchemaError, sqlite3.Error):
                return False
            return True
        finally:
            connection.close()

    def create_contact(
        self,
        *,
        customer_id: str,
        topic: str,
        reply_method: str,
        reply_address: str,
        message: str,
        now: datetime,
    ) -> None:
        _validate_customer_id(customer_id)
        topic = _choice(topic, CONTACT_TOPICS, "Choose a contact topic.")
        reply_method, reply_address = _reply_contact(reply_method, reply_address)
        message = _clean_text(message, "Message", minimum=10, maximum=4000)
        _require_aware(now)
        serialized_now = _serialize_datetime(now)
        cutoff = _serialize_datetime(now - timedelta(hours=1))

        with self.database.transaction(immediate=True) as connection:
            _require_schema(connection)
            recent_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM contact_inquiries "
                    "WHERE customer_id=? AND created_at>=?",
                    (customer_id, cutoff),
                ).fetchone()[0]
            )
            if recent_count >= CONTACT_MAX_PER_HOUR:
                raise InquiryRateLimitError(
                    "Too many contact requests were submitted from this account. Try again later."
                )
            try:
                connection.execute(
                    """
                    INSERT INTO contact_inquiries(
                        id, customer_id, topic, reply_method, reply_address, message, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        secrets.token_urlsafe(18),
                        customer_id,
                        topic,
                        reply_method,
                        reply_address,
                        message,
                        serialized_now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PersistenceError("contact request could not be stored") from exc

    def create_join_application(
        self,
        *,
        customer_id: str,
        specialty: str,
        reply_method: str,
        reply_address: str,
        skills: str,
        tools: str,
        motivation: str,
        now: datetime,
    ) -> bool:
        _validate_customer_id(customer_id)
        specialty = _choice(specialty, JOIN_SPECIALTIES, "Choose a specialty.")
        reply_method, reply_address = _reply_contact(reply_method, reply_address)
        skills = _clean_text(skills, "Skills summary", minimum=20, maximum=4000)
        tools = _clean_text(tools, "Tools", minimum=1, maximum=1000)
        motivation = _clean_text(motivation, "Motivation", minimum=20, maximum=4000)
        _require_aware(now)

        with self.database.transaction(immediate=True) as connection:
            _require_schema(connection)
            if connection.execute(
                "SELECT 1 FROM join_applications WHERE customer_id=?",
                (customer_id,),
            ).fetchone() is not None:
                return False
            try:
                connection.execute(
                    """
                    INSERT INTO join_applications(
                        id, customer_id, specialty, reply_method, reply_address,
                        skills, tools, motivation, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        secrets.token_urlsafe(18),
                        customer_id,
                        specialty,
                        reply_method,
                        reply_address,
                        skills,
                        tools,
                        motivation,
                        _serialize_datetime(now),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if connection.execute(
                    "SELECT 1 FROM join_applications WHERE customer_id=?",
                    (customer_id,),
                ).fetchone() is not None:
                    return False
                raise PersistenceError("join application could not be stored") from exc
        return True

    def has_join_application(self, customer_id: str) -> bool:
        _validate_customer_id(customer_id)
        connection = self.database.connect()
        try:
            _require_schema(connection)
            return connection.execute(
                "SELECT 1 FROM join_applications WHERE customer_id=?",
                (customer_id,),
            ).fetchone() is not None
        finally:
            connection.close()

    def list_contacts(self, *, limit: int = 200) -> list[ContactInquiry]:
        _validate_limit(limit)
        connection = self.database.connect()
        try:
            _require_schema(connection)
            rows = connection.execute(
                """
                SELECT q.*, a.username
                FROM contact_inquiries AS q
                JOIN customer_accounts AS a ON a.id=q.customer_id
                ORDER BY q.created_at DESC, q.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                ContactInquiry(
                    row["id"], row["customer_id"], row["username"], row["topic"],
                    row["reply_method"], row["reply_address"], row["message"],
                    _parse_datetime(row["created_at"]),
                )
                for row in rows
            ]
        finally:
            connection.close()

    def list_join_applications(self, *, limit: int = 200) -> list[JoinApplication]:
        _validate_limit(limit)
        connection = self.database.connect()
        try:
            _require_schema(connection)
            rows = connection.execute(
                """
                SELECT q.*, a.username
                FROM join_applications AS q
                JOIN customer_accounts AS a ON a.id=q.customer_id
                ORDER BY q.created_at DESC, q.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                JoinApplication(
                    row["id"], row["customer_id"], row["username"], row["specialty"],
                    row["reply_method"], row["reply_address"], row["skills"],
                    row["tools"], row["motivation"], _parse_datetime(row["created_at"]),
                )
                for row in rows
            ]
        finally:
            connection.close()


@public.post("/contact")
def contact_submit():
    _mark_submission_private()
    if g.customer is None:
        return redirect(
            url_for("customer.login", next=url_for("public.contact")), code=303
        )
    try:
        require_csrf(request.form.get("csrf_token"))
        _store().create_contact(
            customer_id=g.customer.id,
            topic=request.form.get("topic", ""),
            reply_method=request.form.get("reply_method", ""),
            reply_address=request.form.get("reply_address", ""),
            message=request.form.get("message", ""),
            now=_now(),
        )
    except FormSecurityError:
        return _render_contact_form("The form expired. Try again.", 400)
    except InquiryValidationError as exc:
        return _render_contact_form(str(exc), 400, request.form)
    except InquiryRateLimitError as exc:
        return _render_contact_form(str(exc), 429, request.form)
    except (InquirySchemaError, PersistenceError, sqlite3.Error):
        return _render_contact_form(
            "Contact requests are temporarily unavailable. Please try again later.", 503
        )

    flash("Your contact request was submitted.", "success")
    return redirect(url_for("public.contact"), code=303)


@public.post("/join")
def join_submit():
    _mark_submission_private()
    if g.customer is None:
        return redirect(
            url_for("customer.login", next=url_for("public.join")), code=303
        )
    try:
        require_csrf(request.form.get("csrf_token"))
        created = _store().create_join_application(
            customer_id=g.customer.id,
            specialty=request.form.get("specialty", ""),
            reply_method=request.form.get("reply_method", ""),
            reply_address=request.form.get("reply_address", ""),
            skills=request.form.get("skills", ""),
            tools=request.form.get("tools", ""),
            motivation=request.form.get("motivation", ""),
            now=_now(),
        )
    except FormSecurityError:
        return _render_join_form("The form expired. Try again.", 400)
    except InquiryValidationError as exc:
        return _render_join_form(str(exc), 400, request.form)
    except (InquirySchemaError, PersistenceError, sqlite3.Error):
        return _render_join_form(
            "Join applications are temporarily unavailable. Please try again later.", 503
        )

    if not created:
        flash(
            "You already submitted a Join application from this account. A second application is not allowed.",
            "error",
        )
        return redirect(url_for("public.join"), code=303)
    flash("Your Join application was submitted for review.", "success")
    return redirect(url_for("public.join"), code=303)


@admin.get("/inquiries")
def inquiries():
    try:
        contacts = _store().list_contacts()
        applications = _store().list_join_applications()
    except (InquirySchemaError, PersistenceError, sqlite3.Error):
        abort(503)
    return render_template(
        "admin/inquiries.html",
        contacts=contacts,
        applications=applications,
        contact_topics=CONTACT_TOPICS,
        join_specialties=JOIN_SPECIALTIES,
    )


def register_inquiries(app) -> None:
    repository: ServicesiteRepository = app.extensions["servicesite_repository"]
    app.extensions["servicesite_inquiry_store"] = app.config.get(
        "SERVICESITE_INQUIRY_STORE"
    ) or InquiryStore(repository.database)

    @app.context_processor
    def inject_inquiry_form_context():
        context = {
            "inquiry_contact_topics": CONTACT_TOPICS,
            "inquiry_join_specialties": JOIN_SPECIALTIES,
            "inquiry_values": {},
            "inquiry_store_ready": True,
            "inquiry_join_submitted": False,
        }
        if request.endpoint not in {"public.contact", "public.join"}:
            return context
        if g.customer is None:
            return context
        store = _store()
        try:
            context["inquiry_store_ready"] = store.is_ready()
            if request.endpoint == "public.join" and context["inquiry_store_ready"]:
                context["inquiry_join_submitted"] = store.has_join_application(
                    g.customer.id
                )
        except (InquirySchemaError, PersistenceError, sqlite3.Error):
            context["inquiry_store_ready"] = False
        return context


def _render_contact_form(error: str, status_code: int, values=None):
    return (
        render_template(
            "contact.html",
            contact_method=current_app.config.get("PUBLIC_CONTACT_METHOD"),
            contact_address=current_app.config.get("PUBLIC_CONTACT_ADDRESS"),
            inquiry_error=error,
            inquiry_values=values or {},
        ),
        status_code,
    )


def _render_join_form(error: str, status_code: int, values=None):
    return (
        render_template(
            "join.html",
            inquiry_error=error,
            inquiry_values=values or {},
        ),
        status_code,
    )


def _store() -> InquiryStore:
    return current_app.extensions["servicesite_inquiry_store"]


def _now() -> datetime:
    factory = current_app.extensions.get(
        "servicesite_now_factory", lambda: datetime.now(timezone.utc)
    )
    return factory()


def _mark_submission_private() -> None:
    g.no_store = True
    g.private_response = True


def _choice(value: str, choices: dict[str, str], message: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise InquiryValidationError(message)
    return value


def _reply_contact(method: str, address: str) -> tuple[str, str]:
    try:
        normalized = validate_delivery(method, address)
    except ValueError as exc:
        raise InquiryValidationError(str(exc)) from exc
    return method, normalized


def _clean_text(value: str, label: str, *, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise InquiryValidationError(f"{label} is invalid.")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not minimum <= len(normalized) <= maximum:
        raise InquiryValidationError(
            f"{label} must be between {minimum:,} and {maximum:,} characters."
        )
    if any(
        (ord(character) < 32 and character not in "\n\t") or ord(character) == 127
        for character in normalized
    ):
        raise InquiryValidationError(f"{label} contains unsupported control characters.")
    return normalized


def _validate_customer_id(value: str) -> None:
    if not isinstance(value, str) or not 16 <= len(value) <= 64:
        raise InquiryValidationError("The signed-in account is invalid.")


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise PersistenceError("inquiry result limit is invalid")


def _require_schema(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key=?",
        ("inquiries_schema_version",),
    ).fetchone()
    if row is None:
        raise InquirySchemaError("inquiry database schema is not initialized")
    if row["value"] != str(INQUIRY_SCHEMA_VERSION):
        raise InquirySchemaError("inquiry database schema version is unsupported")


def _require_aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InquiryValidationError("Submission time is invalid.")


def _serialize_datetime(value: datetime) -> str:
    _require_aware(value)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InquirySchemaError("stored inquiry timestamp is invalid")
    return parsed
