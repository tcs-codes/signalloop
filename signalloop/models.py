"""SQLModel tables (build plan §5)."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json(default=dict):
    return Field(default_factory=default, sa_column=Column(JSON))


# ---------- enums ----------
class IcpSource(str, Enum):
    seed = "seed"
    manual = "manual"
    learned = "learned"


class IcpStatus(str, Enum):
    draft = "draft"
    approved = "approved"
    superseded = "superseded"


class AccountStatus(str, Enum):
    new = "new"
    watching = "watching"
    engaged = "engaged"
    suppressed = "suppressed"


class ContactRole(str, Enum):
    champion = "champion"
    economic_buyer = "economic_buyer"
    other = "other"


class SignalType(str, Enum):
    funding = "funding"
    headcount_growth = "headcount_growth"
    job_post = "job_post"
    leader_change = "leader_change"
    post = "post"


class SignalSource(str, Enum):
    cron = "cron"
    webhook = "webhook"
    replay = "replay"


class Channel(str, Enum):
    email = "email"
    linkedin = "linkedin"


class TouchStatus(str, Enum):
    draft = "draft"
    approved = "approved"
    rejected = "rejected"
    exported = "exported"


class OutcomeEvent(str, Enum):
    sent = "sent"
    open = "open"
    reply = "reply"
    meeting = "meeting"
    unsubscribe = "unsubscribe"


class CampaignSource(str, Enum):
    csv = "csv"
    internal = "internal"


class ApprovalKind(str, Enum):
    outreach = "outreach"
    icp_diff = "icp_diff"
    crm_write = "crm_write"


class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    edited = "edited"


class SuppressionKind(str, Enum):
    domain = "domain"
    email = "email"
    person_id = "person_id"


# ---------- tables ----------
class IcpVersion(SQLModel, table=True):
    __tablename__ = "icp_version"
    id: Optional[int] = Field(default=None, primary_key=True)
    version: int = Field(index=True)
    description: str = ""
    filters_json: dict[str, Any] = _json()
    traits_json: dict[str, Any] = _json()
    rationale: str = ""
    source: IcpSource = IcpSource.seed
    status: IcpStatus = IcpStatus.draft
    created_at: datetime = Field(default_factory=utcnow)


class Account(SQLModel, table=True):
    __tablename__ = "account"
    id: Optional[int] = Field(default=None, primary_key=True)
    crustdata_company_id: Optional[str] = Field(default=None, unique=True, index=True)
    domain: str = Field(unique=True, index=True)
    name: str = ""
    firmographics_json: dict[str, Any] = _json()
    fit_score: float = 0.0
    timing_score: float = 0.0
    priority: float = 0.0
    similarity_reason: str = ""
    status: AccountStatus = AccountStatus.new
    icp_version_id: Optional[int] = Field(default=None, foreign_key="icp_version.id")
    is_seed: bool = False
    updated_at: datetime = Field(default_factory=utcnow)


class Contact(SQLModel, table=True):
    __tablename__ = "contact"
    id: Optional[int] = Field(default=None, primary_key=True)
    crustdata_person_id: Optional[str] = Field(default=None, unique=True, index=True)
    account_id: Optional[int] = Field(default=None, foreign_key="account.id", index=True)
    name: str = ""
    title: str = ""
    seniority: str = ""
    function: str = ""
    email: Optional[str] = Field(default=None, index=True)
    profile_url: Optional[str] = Field(default=None, index=True)
    profile_json: dict[str, Any] = _json()
    role: ContactRole = ContactRole.other
    suppressed: bool = False


class Signal(SQLModel, table=True):
    __tablename__ = "signal"
    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id", index=True)
    type: SignalType
    strength: float = 0.0  # 0-1
    payload_json: dict[str, Any] = _json()
    dedupe_key: str = Field(default="", index=True)
    detected_at: datetime = Field(default_factory=utcnow)
    source: SignalSource = SignalSource.cron


class Brief(SQLModel, table=True):
    __tablename__ = "brief"
    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id", index=True)
    signal_id: Optional[int] = Field(default=None, foreign_key="signal.id")
    why_now_json: dict[str, Any] = _json()
    evidence_refs_json: list[Any] = _json(list)
    created_at: datetime = Field(default_factory=utcnow)


class Campaign(SQLModel, table=True):
    __tablename__ = "campaign"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    source: CampaignSource = CampaignSource.csv
    imported_at: datetime = Field(default_factory=utcnow)


class Approval(SQLModel, table=True):
    __tablename__ = "approval"
    id: Optional[int] = Field(default=None, primary_key=True)
    kind: ApprovalKind
    ref_id: Optional[int] = None
    payload_json: dict[str, Any] = _json()
    status: ApprovalStatus = ApprovalStatus.pending
    created_at: datetime = Field(default_factory=utcnow)
    decided_at: Optional[datetime] = None
    note: str = ""


class Touch(SQLModel, table=True):
    __tablename__ = "touch"
    id: Optional[int] = Field(default=None, primary_key=True)
    contact_id: int = Field(foreign_key="contact.id", index=True)
    account_id: int = Field(foreign_key="account.id", index=True)
    campaign_id: Optional[int] = Field(default=None, foreign_key="campaign.id")
    channel: Channel = Channel.email
    subject: str = ""
    body: str = ""
    angle: str = ""
    critic_json: dict[str, Any] = _json()
    approval_id: Optional[int] = Field(default=None, foreign_key="approval.id")
    status: TouchStatus = TouchStatus.draft
    created_at: datetime = Field(default_factory=utcnow)


class Outcome(SQLModel, table=True):
    __tablename__ = "outcome"
    id: Optional[int] = Field(default=None, primary_key=True)
    touch_id: Optional[int] = Field(default=None, foreign_key="touch.id")
    campaign_id: Optional[int] = Field(default=None, foreign_key="campaign.id", index=True)
    contact_id: Optional[int] = Field(default=None, foreign_key="contact.id", index=True)
    account_id: Optional[int] = Field(default=None, foreign_key="account.id")
    contact_email: Optional[str] = None
    domain: Optional[str] = None
    event: OutcomeEvent
    occurred_at: Optional[datetime] = None


class Insight(SQLModel, table=True):
    __tablename__ = "insight"
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    dimension: str
    segment_value: str
    n: int
    responders: int
    rate: float
    baseline_rate: float
    lift: float
    low_confidence: bool = False
    narrative: str = ""


class Suppression(SQLModel, table=True):
    __tablename__ = "suppression"
    id: Optional[int] = Field(default=None, primary_key=True)
    kind: SuppressionKind
    value: str = Field(index=True)
    reason: str = ""


class ApiCache(SQLModel, table=True):
    __tablename__ = "api_cache"
    key: str = Field(primary_key=True)
    endpoint: str = Field(index=True)
    request_json: dict[str, Any] = _json()
    response_json: Any = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class RunLog(SQLModel, table=True):
    __tablename__ = "run_log"
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: Optional[str] = Field(default=None, index=True)
    agent: str = ""
    endpoint_or_model: str = ""
    credits: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cached: bool = False
    ok: bool = True
    error: str = ""
    created_at: datetime = Field(default_factory=utcnow)


ALL_TABLES = [
    "icp_version", "account", "contact", "signal", "brief", "touch", "outcome",
    "campaign", "insight", "approval", "suppression", "api_cache", "run_log",
]
