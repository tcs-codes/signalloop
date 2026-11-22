"""F16: personalized outreach drafting (email + LinkedIn note), reviewed, then queued.

Flow per contact and channel:
    draft -> review (F17 critic) -> up to MAX_REWRITES rewrites with the violations ->
    touch(status=draft, critic_json) -> approvals.enqueue(kind="outreach")
A draft that still fails after the rewrites is queued with ``needs_human=True`` and its
violations visible. Nothing is sent: approved drafts are only exported (Phase 5).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlmodel import select

from signalloop.config import get_settings
from signalloop.db import session_scope
from signalloop.llm import call_llm
from signalloop.models import (
    Account, ApprovalKind, Channel, Contact, ContactRole, Touch, TouchStatus,
)
from signalloop.schemas import Brief, CriticResult, EmailDraft, LinkedInDraft
from signalloop.services import approvals, briefs, evidence, scoring, suppression, watch

SUBJECT_MAX_WORDS = 8
BODY_MAX_WORDS = 120
LINKEDIN_MAX_CHARS = 300
MAX_REWRITES = 2
ROLE_ORDER = {ContactRole.champion: 0, ContactRole.economic_buyer: 1, ContactRole.other: 2}

ReviewFn = Callable[[str, dict], CriticResult]


def _words(s: str) -> int:
    return len((s or "").split())


def _email_check(d: EmailDraft) -> None:
    errs = []
    if _words(d.subject) > SUBJECT_MAX_WORDS:
        errs.append(f"subject has {_words(d.subject)} words; max {SUBJECT_MAX_WORDS}")
    if _words(d.body) > BODY_MAX_WORDS:
        errs.append(f"body has {_words(d.body)} words; max {BODY_MAX_WORDS}")
    if errs:
        raise ValueError("; ".join(errs))


def _linkedin_check(d: LinkedInDraft) -> None:
    if len(d.note) > LINKEDIN_MAX_CHARS:
        raise ValueError(f"note has {len(d.note)} characters; max {LINKEDIN_MAX_CHARS}")


def _contact_text(c: Contact) -> str:
    return f"{c.name}, {c.title}" + (f" ({c.seniority})" if c.seniority else "")


def write(channel: Channel, contact: Contact, brief: Brief, doc: dict,
          violations: list[str] | None = None) -> EmailDraft | LinkedInDraft:
    cfg = get_settings()
    v = "\n".join(f"- {x}" for x in violations) if violations else "(none)"
    brief_text = brief.headline + "\n" + "\n".join(f"- {b.text}" for b in brief.bullets)
    common = {"contact": _contact_text(contact), "role": contact.role.value.replace("_", " "),
              "brief": brief_text, "evidence": doc, "violations": v}
    if channel == Channel.email:
        return call_llm("write_email", {**common, "angle": brief.recommended_angle}, EmailDraft,
                        model=cfg.MODEL_STRONG, agent="outreach_writer", validator=_email_check)
    return call_llm("write_linkedin", common, LinkedInDraft, model=cfg.MODEL_FAST,
                    agent="outreach_writer", validator=_linkedin_check)


def draft_text(d: EmailDraft | LinkedInDraft) -> str:
    return f"Subject: {d.subject}\n\n{d.body}" if isinstance(d, EmailDraft) else d.note


def _default_review(text: str, doc: dict) -> CriticResult:
    from signalloop.services import critic  # F17; imported lazily

    return critic.review(text, doc)


def draft_and_review(channel: Channel, contact: Contact, brief: Brief, doc: dict,
                     review: ReviewFn) -> tuple[EmailDraft | LinkedInDraft, CriticResult]:
    d = write(channel, contact, brief, doc)
    result = review(draft_text(d), doc)
    rewrites = 0
    while not result.passed and rewrites < MAX_REWRITES:
        rewrites += 1
        d = write(channel, contact, brief, doc, violations=result.violations)
        result = review(draft_text(d), doc)
    result.rewrites = rewrites
    result.needs_human = not result.passed
    return d, result


@dataclass
class DraftOutcome:
    touch: Touch
    critic: CriticResult


def _draft_one(account: Account, contact: Contact, brief_row, brief: Brief, doc: dict,
               ch: Channel, review: ReviewFn) -> DraftOutcome:
    d, res = draft_and_review(ch, contact, brief, doc, review)
    touch = Touch(contact_id=contact.id, account_id=account.id, channel=ch,
                  subject=getattr(d, "subject", ""), body=getattr(d, "body", None) or d.note,
                  angle=brief.recommended_angle, critic_json=res.model_dump(),
                  status=TouchStatus.draft)
    with session_scope() as s:
        s.add(touch)
        s.flush()
    appr = approvals.enqueue(ApprovalKind.outreach, touch.id, {
        "touch_id": touch.id, "account_id": account.id, "account": account.name,
        "domain": account.domain, "contact_id": contact.id, "contact": contact.name,
        "title": contact.title, "role": contact.role.value, "channel": ch.value,
        "subject": touch.subject, "body": touch.body, "brief_id": brief_row.id,
        "critic_passed": res.passed, "needs_human": res.needs_human,
        "violations": res.violations, "rewrites": res.rewrites,
    })
    with session_scope() as s:
        s.get(Touch, touch.id).approval_id = appr.id
    touch.approval_id = appr.id
    return DraftOutcome(touch, res)


def _run_parallel(tasks: list[Callable[[], DraftOutcome]]) -> list[DraftOutcome]:
    """Run drafting tasks concurrently (LLM calls dominate); results keep task order.
    Each task runs in a copy of the caller's context so meter run_id/agent labels apply."""
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    workers = max(1, get_settings().OUTREACH_PARALLELISM)
    if workers == 1 or len(tasks) <= 1:
        return [t() for t in tasks]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(contextvars.copy_context().run, t) for t in tasks]
        return [f.result() for f in futures]


def draft_for_contacts(account: Account, people: list[Contact], brief_row, *,
                       channels: tuple[Channel, ...] = (Channel.email, Channel.linkedin),
                       review: ReviewFn | None = None) -> list[DraftOutcome]:
    review = review or _default_review
    brief = briefs.brief_model(brief_row)
    signals = watch.signals_for(account.id)
    tasks = []
    for contact in people:
        if suppression.is_suppressed(contact=contact):
            continue
        doc = evidence.build(account, signals, contact)
        for ch in channels:
            tasks.append(lambda c=contact, d=doc, ch=ch: _draft_one(account, c, brief_row, brief, d, ch, review))
    return _run_parallel(tasks)


def draft_for_contact(account: Account, contact: Contact, brief_row, **kw) -> list[DraftOutcome]:
    return draft_for_contacts(account, [contact], brief_row, **kw)


def _contacts_of(account_id: int) -> list[Contact]:
    with session_scope() as s:
        return list(s.exec(select(Contact).where(Contact.account_id == account_id)).all())


def _has_open_draft(contact_id: int) -> bool:
    with session_scope() as s:
        return s.exec(select(Touch.id).where(Touch.contact_id == contact_id,
                                             Touch.status == TouchStatus.draft)).first() is not None


def draft_this_week(n: int = 10, review: ReviewFn | None = None,
                    channels: tuple[Channel, ...] = (Channel.email, Channel.linkedin),
                    staff_missing: bool = True) -> list[DraftOutcome]:
    """Draft for the top-n in-market accounts. Accounts that became in-market but were not
    staffed during FIND get decision-makers found on demand (F11, ~1.4 credits each)."""
    from signalloop.services import contacts as contacts_svc

    outcomes: list[DraftOutcome] = []
    for b in scoring.this_week(n):
        with session_scope() as s:
            account = s.get(Account, b.account_id)
        everyone = _contacts_of(account.id)
        if not everyone and staff_missing:
            contacts_svc.find_contacts(account_ids=[account.id])
            everyone = _contacts_of(account.id)
        people = sorted(suppression.filter_contacts(everyone), key=lambda c: ROLE_ORDER[c.role])
        people = [c for c in people if not _has_open_draft(c.id)]
        if not people:
            continue
        brief_row = briefs.latest_brief(account.id) or briefs.build_brief(account.id)
        outcomes.extend(draft_for_contacts(account, people, brief_row, channels=channels, review=review))
    return outcomes
