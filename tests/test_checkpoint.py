"""MVP checkpoint invariants (build plan Phase 6)."""
import re
from pathlib import Path

from sqlmodel import select

from signalloop.db import session_scope
from signalloop.models import Insight, Touch
from signalloop.schemas import CriticResult

ROOT = Path(__file__).resolve().parents[1]
CODE = [p for d in ("signalloop", "app") for p in (ROOT / d).rglob("*.py")]

FORBIDDEN = [r"\bsmtplib\b", r"\bsendmail\b", r"\bhubspot\b", r"\bgmail\b", r"slack_sdk", r"hooks\.slack",
             r"\brequests\.(post|put|patch)\b", r"\burllib\.request\b", r"\bboto3\b", r"\bsendgrid\b"]
HTTP_WRITE = re.compile(r"\.(post|put|patch|delete)\(")


def test_no_code_path_sends_or_writes_externally():
    for p in CODE:
        text = p.read_text(encoding="utf-8")
        for pat in FORBIDDEN:
            assert not re.search(pat, text, re.I), f"{p.relative_to(ROOT)} matches forbidden '{pat}'"
        rel = p.relative_to(ROOT).as_posix()
        if rel != "signalloop/tools/crustdata.py":
            for line in text.splitlines():
                if HTTP_WRITE.search(line) and ("http" in line or "client" in line.lower()):
                    raise AssertionError(f"outbound HTTP write outside the Crustdata client: {rel}: {line.strip()}")
        if "import anthropic" in text:
            assert rel == "signalloop/llm.py", f"Anthropic SDK used outside llm.py: {rel}"


def test_crustdata_client_only_posts_to_crustdata():
    text = (ROOT / "signalloop" / "tools" / "crustdata.py").read_text(encoding="utf-8")
    assert "base_url=self.base_url" in text
    from signalloop.config import get_settings
    assert get_settings().CRUSTDATA_BASE_URL == "https://api.crustdata.com"


def test_every_draft_has_a_critic_result(world, fake_llm):   # noqa: F811
    from signalloop.services import outreach

    def sometimes_failing(text, doc):
        return CriticResult(passed="Hi Sam" not in text, violations=["x"] if "Hi Sam" in text else [])

    outreach.draft_this_week(review=sometimes_failing)
    with session_scope() as s:
        touches = s.exec(select(Touch)).all()
    assert touches
    for t in touches:
        CriticResult.model_validate(t.critic_json)            # parses
        assert "passed" in t.critic_json and t.approval_id


def test_every_insight_carries_n():
    for field in ("n", "responders", "low_confidence"):
        assert field in Insight.model_fields


from tests.test_outreach import world  # noqa: E402,F401  (fixture)
