"""Deterministic, offline analysis agent used in demo mode, tests and as the evaluation baseline.

No network access. Classification uses weighted keyword/regex rules per category; the self-estimated
confidence is the rule-match strength. Sentences flagged by the injection screener are excluded from
rule matching, so injected text ("mark this as critical", "assign to ...") cannot steer the result.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date

from pydantic import BaseModel, validate_email

from app.agents.base import ClassificationResult, ExtractionResult, LLMUsage, PlanningResult
from app.agents.injection import screen_for_injection
from app.agents.prompts import PROMPT_VERSION
from app.agents.tools import ToolRegistry, collect_tool_calls, completed_action_types, offered_tools
from app.domain.enums import ActionType, EmailCategory, Priority, Urgency
from app.schemas.ai import (
    ClassificationOutput,
    EvidenceSnippet,
    ExtractionOutput,
    PlanningContext,
    ProposeAzureDevOpsWorkItemArgs,
    ProposeEmailReplyArgs,
    ProposeFollowUpTaskArgs,
    ProposeNoActionArgs,
)
from app.schemas.email import EmailContext
from app.schemas.settings import AzureDevOpsDefaults, TaxonomyConfig

PROVIDER_NAME = "heuristic"
MODEL_NAME = "heuristic-rules-v1"
MIN_CATEGORY_SCORE = 1.5
_I = re.IGNORECASE


_LOGIN_FAILURE = (
    r"\b(?:unable\s+to|can(?:no|')t|cannot|not\s+able\s+to)\s+(?:log\s?in|sign\s?in|login|access\s+the|"
    r"connect|load|reach|place\s+orders?|check\s?out|pay)\b|\b(?:log-?in|sign-?in)s?\s+(?:is\s+|are\s+)?"
    r"(?:failing|broken|failures?)\b"
)
_BROAD_IMPACT = (
    r"\b(?:all|many|multiple|several|most|hundreds\s+of|thousands\s+of)\s+(?:of\s+(?:our|the)\s+)?"
    r"(?:[\w-]+\s+)?(?:users|customers|clients|employees|stores|sites)\b|\b(?:users|customers)\s+"
    r"(?:are|were)\s+(?:unable|not\s+able|getting|seeing|reporting)\b|\b(?:nobody|no\s+one|everyone)\b"
)
_AFTER_CHANGE = (
    r"\bafter\s+(?:(?:today|yesterday|last\s+night|this\s+morning|the|our|last|latest|recent)'?s?\s+)+"
    r"(?:deployment|deploy|release|update|upgrade|migration|maintenance|patch)\b"
)


@dataclass(frozen=True, slots=True)
class _Rule:
    label: str
    pattern: re.Pattern[str]
    weight: float


def _r(label: str, pattern: str, weight: float, flags: int = _I) -> _Rule:
    return _Rule(label, re.compile(pattern, flags), weight)


CATEGORY_RULES: dict[EmailCategory, tuple[_Rule, ...]] = {
    EmailCategory.INCIDENT: (
        _r(
            "outage wording",
            r"\b(?:outage|downtime|is\s+down|are\s+down|went\s+down|(?:completely|totally)\s+down|unavailable|"
            r"unreachable|not\s+loading|not\s+responding|service\s+disruption)\b",
            3,
        ),
        _r("users unable to sign in or access", _LOGIN_FAILURE, 2.5),
        _r("production environment", r"(?<!pre-)(?<!pre)\b(?:production|prod)\b|\blive\s+(?:site|environment)\b", 2),
        _r("server errors", r"\b5\d\d\s+errors?\b|\binternal\s+server\s+error\b|\bhttp\s+5\d\d\b|\b50[234]\b", 2),
        _r("broad user impact", _BROAD_IMPACT, 1.5),
        _r("urgency wording", r"\b(?:urgent(?:ly)?|immediately|asap|emergency|right\s+away|sev(?:erity)?\s*[12])\b", 1),
        _r(
            "degradation or security event",
            r"\b(?:degraded|degradation|latency\s+spike|timing\s+out|data\s+loss|security\s+(?:incident|breach)|"
            r"breach|compromised|ransomware|unauthori[sz]ed\s+access)\b",
            2,
        ),
        _r("failure after a change", _AFTER_CHANGE, 1.5),
        _r("incident wording", r"\bincident\b", 2),
    ),
    EmailCategory.BUG: (
        _r("bug wording", r"\b(?:bug|defect|regression|glitch)s?\b", 3),
        _r("reproduction steps", r"\bsteps\s+to\s+reproduce\b|\brepro(?:duction)?\s+steps\b|\bto\s+reproduce\b", 3),
        _r(
            "expected vs actual behaviour",
            r"\bexpected\s+(?:result|behaviou?r)?\s*[:\-]|\bactual\s+(?:result|behaviou?r)\s*[:\-]|"
            r"\bexpected\b[^.\n]{0,80}\bbut\b|\binstead\s+of\b|\bshould\s+(?:show|display|be|return)\b",
            2,
        ),
        _r(
            "error or malfunction",
            r"\b(?:error|exception|stack\s*trace|crash(?:es|ed|ing)?|fails?|failing|failed|broken|incorrect(?:ly)?|"
            r"wrong|not\s+working|doesn'?t\s+work|does\s+not\s+work|not\s+saving|blank\s+page|freez(?:es|ing))\b",
            1.5,
        ),
        _r(
            "technical error detail",
            r"\b(?:null\s*pointer|NullReference\w*|TypeError|ValueError|undefined|NaN|traceback|error\s+code)\b",
            2,
        ),
        _r("intermittent behaviour", r"\b(?:intermittent(?:ly)?|sometimes|occasionally|randomly)\b", 0.5),
    ),
    EmailCategory.SERVICE_REQUEST: (
        _r(
            "access request",
            r"\b(?:request(?:ing)?\s+(?:\w+\s+)?(?:access|permissions?)|grant\s+(?:me\s+|us\s+|him\s+|her\s+|them\s+)?"
            r"(?:[\w-]+\s+){0,2}(?:access|permissions?|rights)|need\s+(?:[\w-]+\s+){0,2}access|"
            r"give\s+(?:me|us|him|her|them)\s+(?:[\w-]+\s+)?access|access\s+request|add\s+(?:me|him|her|them)\s+to)\b",
            3,
        ),
        _r(
            "account or credential help",
            r"\b(?:reset\s+(?:my\s+|the\s+|his\s+|her\s+)?(?:password|mfa|2fa)|password\s+reset|unlock\s+"
            r"(?:my|the|his|her)\s+account|locked\s+out)\b",
            3,
        ),
        _r(
            "provisioning",
            r"\b(?:provision(?:ing)?|onboard(?:ing)?|new\s+(?:hire|starter|joiner|employee)|set\s*up\s+(?:a\s+)?"
            r"(?:new\s+)?(?:account|laptop|mailbox|license|vpn)|create\s+(?:a\s+)?(?:new\s+)?(?:account|user|"
            r"mailbox|distribution\s+list)|licen[cs]e|vpn\s+access|shared\s+mailbox|distribution\s+list|"
            r"deactivate|offboard(?:ing)?|revoke)\b",
            2,
        ),
        _r("permission wording", r"\b(?:permissions?|admin\s+rights|read-?only|write\s+access|access\s+level)\b", 1),
        _r("service request wording", r"\bservice\s+request\b", 3),
    ),
    EmailCategory.TASK_REQUEST: (
        _r(
            "request to perform work",
            r"\b(?:please|could\s+you|can\s+you|would\s+you|kindly|i\s+need\s+you\s+to|we\s+need\s+you\s+to)\s+"
            r"(?:please\s+)?(?:prepare|update|review|schedule|draft|compile|create|write|send\s+(?:me|us|over)|"
            r"put\s+together|organi[sz]e|arrange|book|summari[sz]e|analy[sz]e|migrate|clean\s+up|plan|set\s+up|"
            r"coordinate|translate|renew|archive|export|finali[sz]e|proofread|order)\b",
            3,
        ),
        _r(
            "due date wording",
            r"\b(?:by\s+(?:end\s+of\s+(?:day|week|month)|eod|eow|monday|tuesday|wednesday|thursday|friday|"
            r"tomorrow|next\s+week|\d{4}-\d{2}-\d{2}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d)|"
            r"deadline|due\s+(?:date|by|on))\b",
            1,
        ),
        _r(
            "imperative subject",
            r"^\s*(?:prepare|schedule|draft|compile|translate|organi[sz]e|arrange|summari[sz]e|proofread|plan|"
            r"coordinate|renew|finali[sz]e|write|set\s+up|clean\s+up|put\s+together)\b",
            2,
        ),
        _r(
            "deliverable",
            r"\b(?:report|presentation|slides|deck|documentation|spreadsheet|agenda|minutes|budget|forecast|"
            r"checklist|runbook|meeting|workshop|training|invoice|contract)\b",
            1,
        ),
        _r("task wording", r"\b(?:action\s+items?|to-?do|task)\b", 1),
    ),
    EmailCategory.FEATURE_REQUEST: (
        _r(
            "wish wording",
            r"\b(?:would\s+be\s+(?:great|nice|helpful|useful|amazing)|it\s+would\s+help|would\s+love|wish(?:list)?|"
            r"nice\s+to\s+have)\b",
            3,
        ),
        _r("feature wording", r"\b(?:feature\s+request|new\s+feature|enhancement|improvement\s+request)\b", 3),
        _r(
            "new capability",
            r"\b(?:add\s+(?:support\s+for|an?\s+option|the\s+ability|a\s+way|a\s+filter|a\s+setting)|support\s+for|"
            r"ability\s+to|option\s+to|allow\s+(?:us|users|customers)\s+to|integrat(?:e|ion)\s+with|export\s+(?:to|as)|"
            r"dark\s+mode|bulk\s+\w+)\b",
            2,
        ),
        _r("roadmap wording", r"\b(?:roadmap|feature|suggestion)\b", 1),
    ),
    EmailCategory.FOLLOW_UP: (
        _r(
            "follow-up wording",
            r"\b(?:following\s+up|follow(?:ing)?[-\s]up\s+on|just\s+checking\s+in|checking\s+in|any\s+(?:update|news|"
            r"progress)s?|status\s+(?:of|on|update)|where\s+(?:are\s+we|do\s+we\s+stand)|gentle\s+reminder|"
            r"friendly\s+reminder|haven'?t\s+heard\s+back|still\s+waiting|circling\s+back|bump(?:ing)?\s+this|"
            r"per\s+my\s+(?:last|previous)\s+(?:email|message))\b",
            3,
        ),
        _r("reply thread", r"^\s*(?:re|fw|fwd)\s*:", 0.5),
        _r(
            "earlier conversation",
            r"\b(?:last\s+week|previously|my\s+(?:last|previous|earlier)\s+(?:email|message|request)|we\s+discussed|"
            r"you\s+mentioned|opened\s+a\s+(?:ticket|case)|(?:i|we)\s+sent\s+(?:you\s+)?(?:last|on|yesterday)|"
            r"still\s+(?:don'?t|do\s+not|haven'?t|have\s+not|no)\b)",
            1,
        ),
        _r("ticket reference", r"\b[A-Z][A-Z0-9]{1,9}-\d{1,7}\b|\b(?:INC|REQ|RITM|CHG|CASE)\d{4,}\b", 1, 0),
    ),
    EmailCategory.INFORMATION_ONLY: (
        _r(
            "FYI wording",
            r"\b(?:fyi|for\s+your\s+(?:information|awareness|records)|heads[-\s]up|no\s+action\s+(?:is\s+)?"
            r"(?:needed|required)|nothing\s+(?:is\s+)?(?:needed|required)\s+from\s+you|for\s+reference|"
            r"no\s+(?:reply|response)\s+(?:is\s+)?(?:needed|necessary|required))\b",
            3,
        ),
        _r(
            "announcement",
            r"\b(?:newsletter|announcement|announcing|pleased\s+to\s+announce|digest|weekly\s+update|monthly\s+update|"
            r"scheduled\s+maintenance|planned\s+maintenance|maintenance\s+window|will\s+be\s+closed|"
            r"reminder\s+that|welcome\s+(?:to|our)|congratulations\s+to)\b",
            2.5,
        ),
        _r(
            "informational tone",
            r"\b(?:just\s+(?:a\s+)?(?:note|update)|sharing|summary\s+of|recap|release\s+notes)\b",
            1,
        ),
    ),
    EmailCategory.SPAM_OR_IRRELEVANT: (
        _r("unsubscribe footer", r"\bunsubscribe\b", 1),
        _r(
            "prize wording",
            r"\b(?:you(?:'ve|\s+have)\s+(?:been\s+selected|won)|winner|lottery|jackpot|prize|claim\s+your|"
            r"congratulations,?\s+you)\b",
            3,
        ),
        _r(
            "get-rich wording",
            r"\b(?:crypto(?:currency)?|bitcoin|forex|investment\s+opportunity|guaranteed\s+(?:returns?|income|profit)|"
            r"double\s+your|make\s+money|passive\s+income)\b",
            3,
        ),
        _r(
            "marketing pressure",
            r"\b(?:click\s+here|limited[-\s]time|limited\s+offer|act\s+now|special\s+(?:offer|promotion)|"
            r"exclusive\s+(?:deal|offer)|100%\s+free|risk[-\s]free|buy\s+now|order\s+now|\d+%\s+off)\b",
            2,
        ),
        _r(
            "phishing wording",
            r"\b(?:verify\s+your\s+(?:account|identity)|account\s+(?:will\s+be\s+)?(?:suspended|closed)|confirm\s+your\s+"
            r"(?:password|banking|payment)|wire\s+transfer|gift\s+cards?|inheritance|beneficiary)\b",
            3,
        ),
        _r("unrelated promotion", r"\b(?:casino|weight\s+loss|dating|miracle|seo\s+services)\b", 3),
    ),
}

# Earlier categories win ties.
_TIE_ORDER = (
    EmailCategory.SPAM_OR_IRRELEVANT,
    EmailCategory.INCIDENT,
    EmailCategory.BUG,
    EmailCategory.SERVICE_REQUEST,
    EmailCategory.FEATURE_REQUEST,
    EmailCategory.TASK_REQUEST,
    EmailCategory.FOLLOW_UP,
    EmailCategory.INFORMATION_ONLY,
)
_CATEGORY_LABELS = {c: c.value.replace("_", " ") for c in EmailCategory}
_ACTIONABLE = frozenset(
    {
        EmailCategory.INCIDENT,
        EmailCategory.BUG,
        EmailCategory.SERVICE_REQUEST,
        EmailCategory.TASK_REQUEST,
        EmailCategory.FEATURE_REQUEST,
    }
)
_ACK_CATEGORIES = frozenset({EmailCategory.INCIDENT, EmailCategory.BUG, EmailCategory.SERVICE_REQUEST})

_URGENT_RE = re.compile(
    r"\b(?:urgent(?:ly)?|immediately|asap|as\s+soon\s+as\s+possible|right\s+away|emergency|time[-\s]sensitive)\b", _I
)
_LOW_RE = re.compile(
    r"\b(?:no\s+rush|not\s+urgent|low\s+priority|when(?:ever)?\s+you\s+(?:have|get)\s+(?:a\s+)?(?:chance|"
    r"moment|time)|at\s+your\s+convenience|no\s+hurry)\b",
    _I,
)
_SEVERE_RE = re.compile(
    r"\b(?:outage|is\s+down|are\s+down|went\s+down|completely\s+down|data\s+loss|(?:data|security)\s+breach|"
    r"compromised|ransomware|nobody\s+can|no\s+one\s+can|all\s+(?:of\s+our\s+)?(?:users|customers))\b",
    _I,
)
_BROAD_RE = re.compile(_BROAD_IMPACT, _I)
_BLOCKED_RE = re.compile(r"\b(?:blocked|blocking|blocker|cannot\s+proceed|can'?t\s+proceed)\b", _I)

_PRODUCTION_RE = re.compile(r"(?<!pre-)(?<!pre)\b(?:production|prod)\b|\blive\s+(?:site|environment)\b", _I)
_STAGING_RE = re.compile(r"\b(?:staging|pre-?prod(?:uction)?|stage\s+environment)\b", _I)
_TEST_RE = re.compile(
    r"\b(?:test|testing|qa|uat)\s+(?:environment|env|server|instance|tenant|system)\b|\bin\s+(?:qa|uat)\b", _I
)
_DEV_RE = re.compile(r"\b(?:dev|development)\s+(?:environment|env|server|instance|tenant)\b|\bin\s+dev\b", _I)

_PRODUCT_HEADS = (
    r"(?:portal|service|app|application|platform|dashboard|api|website|web\s?site|site|system|server|database|"
    r"gateway|module|integration|pipeline|tool|crm|erp|vpn|mailbox|wiki|repository|repo|cluster|page|checkout|"
    r"cart|store|workspace|channel|drive|printer|network|sandbox|instance|feed|scheduler|queue|warehouse|"
    r"endpoint|plugin|extension|widget|calendar|screen|tab|report|form|console|library|portal)"
)
# Systems that are commonly named without a modifier ("the VPN").
_SINGLE_WORD_SYSTEM_RE = re.compile(r"\b(?:the|our|your|my)\s+(VPN|CRM|ERP|intranet|wiki|Wi-?Fi|SSO|LMS|HRIS)\b", _I)
# Resources typical of service requests that are not "products".
_RESOURCE_RE = re.compile(
    r"\b(?:distribution\s+list|mailing\s+list|security\s+group|shared\s+(?:mailbox|drive|folder)|teams?\s+channel|"
    r"vpn|licen[cs]e|laptop|badge|mailbox|repository)\b",
    _I,
)
_DETERMINER_RE = re.compile(r"\b(?:the|our|your|my|their|its|this|that)\s+", _I)
_PRODUCT_AFTER_DET_RE = re.compile(r"((?:[A-Za-z0-9][\w&+.-]*\s+){1,3}?" + _PRODUCT_HEADS + r")s?\b", _I)
_PRODUCT_CAPS_RE = re.compile(
    r"\b((?:[A-Z][\w&+-]*\s+){1,3}(?:API|Portal|Service|App|Dashboard|Platform|Gateway|Site|Database|CRM|ERP))\b"
)
_PRODUCT_LABEL_RE = re.compile(
    r"^\s*(?:product|system|application|app|service|affected\s+(?:system|service|product|application))\s*:\s*"
    r"([^\n]{2,80})$",
    _I | re.MULTILINE,
)
_KNOWN_SYSTEMS_RE = re.compile(
    r"\b(SharePoint|Salesforce|Jira|Confluence|Azure DevOps|Outlook|Microsoft Teams|SAP|Workday|GitHub|GitLab|"
    r"ServiceNow|Okta|Slack|Power BI|Tableau|Snowflake|Zendesk|HubSpot|Dynamics 365|Microsoft 365|OneDrive|"
    r"Kubernetes|Jenkins)\b"
)
_STOPWORD_TEXT = (
    "a an the this that these those our your my their its is are was were be been being am do does did has have "
    "had to of in on at for from with by and or but not no as it we you they he she i me us them all any some "
    "same new old whole entire current existing main other following above below first second last next only "
    "usual affecting affects affected using use used when where which while into onto over under about after "
    "before during since until also just very really still again please can could would should will shall may "
    "might must"
)
_MODIFIER_STOPWORDS = frozenset(_STOPWORD_TEXT.split())

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])|\n+")
_GREETING_RE = re.compile(r"^(?:hi|hello|hey|dear|good\s+(?:morning|afternoon|evening)|greetings)\b[^.!?]{0,40}$", _I)
_SIGNOFF_RE = re.compile(
    r"^(?:thanks|thank\s+you|many\s+thanks|regards|best|cheers|kind\s+regards|best\s+regards|sincerely|warm\s+regards)"
    r"\b[\s,.!]*(?:in\s+advance)?[\s,.!]*$",
    _I,
)
_QUOTE_CUT_RE = re.compile(r"^(?:On\s.{3,120}\swrote:|-{2,}\s*Original Message\s*-{2,}|_{5,})\s*$", re.MULTILINE)
_REPLY_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|wg)\s*:\s*)+", _I)

_URL_RE = re.compile(r"https?://[^\s<>\"'`)\]]+", _I)
_TICKET_RE = re.compile(
    r"\b(?!(?:ISO|SHA|UTF|COVID|RFC|TLS|SSL|HTTP|HTTPS|MD|AES|RSA|ISBN|Q[1-4]|FY\d*)-)[A-Z][A-Z0-9]{1,9}(?:-\d{1,7})+\b"
    r"|\b(?:INC|REQ|RITM|CHG|PRB|SR|TKT|CASE)\d{4,10}\b|(?<![\w&])#\d{3,8}\b"
)

_MONTH = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"),
    (re.compile(_MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4})\b)?", _I), "mdy"),
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH + r"(?:,?\s+(\d{4})\b)?", _I), "dmy"),
)
_DEADLINE_CUE_RE = re.compile(
    r"\b(?:by|before|due|deadline|no\s+later\s+than|until|till|prior\s+to|ahead\s+of|in\s+time\s+for|"
    r"on\s+or\s+before|needed\s+on|required\s+on|expires?\s+on|meeting\s+on|scheduled\s+for|ready\s+for|"
    r"start(?:s|ing)?\s+on|first\s+day)\b[^.\n]{0,40}$",
    _I,
)
_MONTHS3 = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")

_IMPACT_RE = re.compile(
    r"\b(?:affect\w*|impact\w*|unable|cannot|can'?t|blocked|blocking|prevent\w*|users|customers|revenue|outage|"
    r"down|failing|fails|losing)\b",
    _I,
)
_ORG_RE = re.compile(
    r"\b(?:customer|client|account|on\s+behalf\s+of)\s*(?::|-)?\s+((?:[A-Z][\w&.-]+)(?:\s+(?:[A-Z][\w&.-]+|&)){0,3})"
    r"|\b((?:[A-Z][\w&-]+\s+){1,3}(?:Ltd|Inc|LLC|GmbH|Corp|Corporation|PLC|AG|BV))\b"
)
_OWNER_RE = re.compile(
    r"\b(?:(?i:assign(?:ed)?\s+(?:this\s+|it\s+|the\s+\w+\s+)?to)|(?i:owner\s*(?::|is|should\s+be))|"
    r"(?i:owned\s+by))\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b"
)
_TEAM_RE = re.compile(r"\b(?:(?i:the|our|your)\s+)?([A-Z][\w&-]+(?:\s+[A-Z][\w&-]+)?)\s+(?i:team)\b")
_NOT_NAMES = frozenset(
    {"The", "Our", "Your", "My", "This", "That", "Me", "Us", "Him", "Her", "Them", "It", "Hi", "Dear"}
)

_TRIGGER_RE = re.compile(_AFTER_CHANGE, _I)
_PROBLEMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(_LOGIN_FAILURE, _I), "login failures"),
    (
        re.compile(r"\bpayments?\s+(?:are\s+|is\s+)?(?:fail\w*|declin\w*|error\w*)|\bcheckout\s+fail\w*", _I),
        "payment failures",
    ),
    (re.compile(r"\b(5\d\d)\s+errors?\b|\binternal\s+server\s+error", _I), "server errors"),
    (
        re.compile(
            r"\b(?:outage|downtime|is\s+down|are\s+down|went\s+down|unavailable|not\s+loading|not\s+responding)\b", _I
        ),
        "outage",
    ),
    (re.compile(r"\b(?:data\s+loss|missing\s+data|data\s+(?:is\s+)?missing)\b", _I), "missing data"),
    (re.compile(r"\bcrash(?:es|ed|ing)?\b", _I), "crashes"),
    (re.compile(r"\b(?:slow(?:ness)?|latency|timeouts?|timing\s+out|performance)\b", _I), "performance degradation"),
    (re.compile(r"\b(?:wrong|incorrect)\s+([a-z]+(?:\s+[a-z]+)?)", _I), "incorrect {0}"),
    (re.compile(r"\bnot\s+saving\b|\bfails?\s+to\s+save\b", _I), "failures when saving"),
    (re.compile(r"\b(?:error|exception)s?\b", _I), "errors"),
)
_TASK_PHRASE_RE = re.compile(
    r"\b(?:please|could\s+you|can\s+you|would\s+you|kindly|i\s+need\s+you\s+to|we\s+need\s+you\s+to)\s+"
    r"(?:please\s+)?((?:prepare|update|review|schedule|draft|compile|create|write|send|put\s+together|organi[sz]e|"
    r"arrange|book|summari[sz]e|analy[sz]e|migrate|clean\s+up|plan|set\s+up|coordinate|translate|renew|archive|"
    r"export|finali[sz]e|proofread|order)\b[^.?!\n]{2,100})",
    _I,
)
_TASK_CUT_RE = re.compile(
    r"\s+(?:by|before|no\s+later\s+than|until|for\s+me|so\s+that|and\s+(?:let|send\s+it)|when|if)\b.*$|[,;:].*$", _I
)

_ADO_DEFAULTS = AzureDevOpsDefaults()


@dataclass
class _Analysis:
    """Everything the heuristic derives from one email (computed once per call; deterministic)."""

    subject: str
    sentences: list[str]  # verbatim body sentences, injection-flagged ones removed
    text: str  # subject + clean sentences, used for rule matching
    scores: dict[EmailCategory, float] = field(default_factory=dict)
    matched: dict[EmailCategory, list[tuple[_Rule, str]]] = field(default_factory=dict)
    environment: str | None = None
    product: str | None = None


def _strip_quoted(body: str) -> str:
    cut = _QUOTE_CUT_RE.search(body)
    if cut:
        body = body[: cut.start()]
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith(">"))


def _sentences(body: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(body)]
    return [p for p in parts if p]


def _content_sentences(sentences: list[str]) -> list[str]:
    """Drop greetings and everything from the sign-off on (signature blocks)."""
    kept: list[str] = []
    for sentence in sentences:
        if _SIGNOFF_RE.match(sentence):
            break
        if _GREETING_RE.match(sentence):
            continue
        kept.append(sentence)
    return kept


def _detect_environment(text: str) -> str | None:
    if _PRODUCTION_RE.search(text):
        return "production"
    if _STAGING_RE.search(text):
        return "staging"
    if _TEST_RE.search(text):
        return "test"
    if _DEV_RE.search(text):
        return "development"
    return None


def _clean_product(candidate: str) -> str | None:
    words = candidate.split()
    while words and words[0].lower() in _MODIFIER_STOPWORDS:
        words = words[1:]
    if len(words) < 2 or any(w.lower() in _MODIFIER_STOPWORDS for w in words[:-1]):
        return None
    if words[0].lower() in {"production", "prod", "staging", "test", "dev", "development", "live"}:
        words = words[1:]
        if len(words) < 2:
            return None
    return " ".join(words).strip(" .,;:")


def _detect_product(subject: str, body: str) -> str | None:
    label = _PRODUCT_LABEL_RE.search(body)
    if label:
        return label.group(1).strip(" .")[:80]
    for text in (subject, body):
        for det in _DETERMINER_RE.finditer(text):
            match = _PRODUCT_AFTER_DET_RE.match(text, det.end())
            if match:
                product = _clean_product(match.group(1))
                if product:
                    return product
        caps = _PRODUCT_CAPS_RE.search(text)
        if caps:
            product = _clean_product(caps.group(1))
            if product:
                return product
    combined = f"{subject}\n{body}"
    known = _KNOWN_SYSTEMS_RE.search(combined)
    if known:
        return known.group(1)
    single = _SINGLE_WORD_SYSTEM_RE.search(combined)
    return single.group(1) if single else None


def _analyze(email: EmailContext) -> _Analysis:
    subject = "" if screen_for_injection(email.subject) else email.subject
    sentences = [s for s in _sentences(_strip_quoted(email.body_text)) if not screen_for_injection(s)]
    body = "\n".join(sentences)
    analysis = _Analysis(subject=subject, sentences=sentences, text=f"{subject}\n{body}")
    for category, rules in CATEGORY_RULES.items():
        score = 0.0
        hits: list[tuple[_Rule, str]] = []
        for rule in rules:
            in_subject = rule.pattern.search(subject) if subject else None
            hit = in_subject or rule.pattern.search(body)
            if hit:
                score += rule.weight * (1.5 if in_subject else 1.0)
                hits.append((rule, hit.group(0)))
        analysis.scores[category] = score
        analysis.matched[category] = hits
    analysis.environment = _detect_environment(analysis.text)
    analysis.product = _detect_product(subject, body)
    return analysis


def _choose_category(analysis: _Analysis, enabled: list[EmailCategory]) -> tuple[EmailCategory, float, float]:
    ranked = sorted(
        (c for c in _TIE_ORDER if c in enabled),
        key=lambda c: (-analysis.scores.get(c, 0.0), _TIE_ORDER.index(c)),
    )
    if not ranked:
        return EmailCategory.OTHER, 0.0, 0.0
    top = ranked[0]
    top_score = analysis.scores.get(top, 0.0)
    second = analysis.scores.get(ranked[1], 0.0) if len(ranked) > 1 else 0.0
    if top_score < MIN_CATEGORY_SCORE:
        return EmailCategory.OTHER, top_score, second
    return top, top_score, second


def _confidence(category: EmailCategory, top: float, second: float) -> float:
    if category is EmailCategory.OTHER:
        return 0.4
    strength = min(1.0, top / 6.0)
    margin = (top - second) / top if top else 0.0
    return round(min(0.95, 0.35 + 0.45 * strength + 0.15 * margin), 2)


def _priority_and_urgency(category: EmailCategory, analysis: _Analysis) -> tuple[Priority, Urgency, list[str]]:
    text = analysis.text
    prod = analysis.environment == "production"
    # Low-urgency phrases are removed first so that "not urgent" does not count as urgency wording.
    urgent = bool(_URGENT_RE.search(_LOW_RE.sub(" ", text)))
    low = bool(_LOW_RE.search(text))
    severe = bool(_SEVERE_RE.search(text))
    broad = bool(_BROAD_RE.search(text))
    blocked = bool(_BLOCKED_RE.search(text))
    notes: list[str] = []
    if category is EmailCategory.INCIDENT:
        priority = Priority.HIGH
        if prod and severe and (urgent or broad):
            priority = Priority.CRITICAL
            notes.append("a severe production impact is described")
        elif low and not prod:
            priority = Priority.MEDIUM
        urgency = Urgency.IMMEDIATE if urgent or severe else Urgency.HIGH
    elif category is EmailCategory.BUG:
        priority = Priority.MEDIUM
        if prod and (broad or blocked or urgent):
            priority = Priority.HIGH
            notes.append("it affects production users")
        elif low:
            priority = Priority.LOW
        urgency = Urgency.HIGH if urgent else (Urgency.LOW if low else Urgency.NORMAL)
    elif category in (EmailCategory.SERVICE_REQUEST, EmailCategory.TASK_REQUEST):
        priority = Priority.MEDIUM
        if urgent or blocked:
            priority = Priority.HIGH
            notes.append("the sender is blocked or asks for urgent help")
        elif low:
            priority = Priority.LOW
        urgency = Urgency.HIGH if urgent else (Urgency.LOW if low else Urgency.NORMAL)
    elif category is EmailCategory.FEATURE_REQUEST:
        priority = Priority.MEDIUM if blocked else Priority.LOW
        urgency = Urgency.LOW if low or not urgent else Urgency.NORMAL
    elif category is EmailCategory.FOLLOW_UP:
        priority = Priority.MEDIUM if urgent or blocked else Priority.LOW
        urgency = Urgency.HIGH if urgent else Urgency.NORMAL
    else:
        priority, urgency = Priority.LOW, Urgency.LOW
    return priority, urgency, notes


def _word_count(sentences: list[str]) -> int:
    return sum(len(s.split()) for s in sentences)


def _missing_information(category: EmailCategory, analysis: _Analysis) -> list[str]:
    content = _content_sentences(analysis.sentences)
    words = _word_count(content)
    vague = words < 25
    missing: list[str] = []
    if category in (EmailCategory.INCIDENT, EmailCategory.BUG):
        if analysis.product is None:
            missing.append("affected product or system")
        technical = analysis.matched[EmailCategory.BUG]
        has_detail = any(
            r.label in {"reproduction steps", "expected vs actual behaviour", "technical error detail"}
            for r, _ in technical
        )
        if category is EmailCategory.BUG and vague and not has_detail:
            missing.append("steps to reproduce or the exact error message")
    elif category is EmailCategory.SERVICE_REQUEST:
        if analysis.product is None and not _RESOURCE_RE.search(analysis.text):
            account_help = any(r.label == "account or credential help" for r, _ in analysis.matched[category])
            if not account_help:
                missing.append("system or resource the request is for")
    elif category is EmailCategory.TASK_REQUEST:
        if words < 12:
            missing.append("details of the requested work")
    return missing


def _rationale(category: EmailCategory, analysis: _Analysis, notes: list[str]) -> str:
    if category is EmailCategory.OTHER:
        return "No category-specific indicators were strong enough, so the email was classified as other."
    labels = list(dict.fromkeys(rule.label for rule, _ in analysis.matched.get(category, [])))[:3]
    sentence = f"Classified as {_CATEGORY_LABELS[category]} based on {', '.join(labels) or 'keyword matches'}."
    if notes:
        sentence += f" Priority reflects that {notes[0]}."
    return sentence


def _usage(started: float) -> LLMUsage:
    return LLMUsage(
        provider=PROVIDER_NAME,
        model=MODEL_NAME,
        latency_ms=int((time.perf_counter() - started) * 1000),
        estimated_cost_usd=0.0,
    )


def _deadline(analysis: _Analysis, received: date) -> tuple[str | None, str | None]:
    """First explicit date preceded by a deadline cue -> (ISO date, verbatim sentence)."""
    for sentence in [analysis.subject, *analysis.sentences]:
        for pattern, kind in _DATE_PATTERNS:
            for match in pattern.finditer(sentence):
                if not _DEADLINE_CUE_RE.search(sentence[max(0, match.start() - 60) : match.start()]):
                    continue
                groups = match.groups()
                try:
                    if kind == "ymd":
                        value = date(int(groups[0]), int(groups[1]), int(groups[2]))
                    else:
                        month_name, day = (groups[0], groups[1]) if kind == "mdy" else (groups[1], groups[0])
                        month = _MONTHS3.index(month_name[:3].lower()) + 1
                        year = int(groups[2]) if groups[2] else received.year
                        value = date(year, month, int(day))
                        if not groups[2] and value < received:
                            value = date(year + 1, month, int(day))
                except ValueError:
                    continue
                return value.isoformat(), sentence
    return None, None


def _first_name(email: EmailContext) -> str:
    if email.sender_name:
        name = email.sender_name.split(",")[-1].strip() if "," in email.sender_name else email.sender_name
        first = name.split()[0] if name.split() else ""
        if first.isalpha():
            return first
    return "there"


def _plain_subject(email: EmailContext) -> str:
    return _REPLY_PREFIX_RE.sub("", email.subject).strip() or "your email"


def _clip(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _problem_phrase(analysis: _Analysis) -> str | None:
    for pattern, template in _PROBLEMS:
        match = pattern.search(analysis.text)
        if match:
            if "{0}" in template:
                return template.format(match.group(1).lower())
            if template == "server errors" and match.group(1):
                return f"{match.group(1)} errors"
            return template
    return None


def _summary(category: EmailCategory, analysis: _Analysis, email: EmailContext) -> str:
    subject = _plain_subject(email)
    requester = email.sender_name or "the requester"
    if category in (EmailCategory.INCIDENT, EmailCategory.BUG):
        problem = _problem_phrase(analysis)
        if problem is None:
            return f"Investigate reported issue: {subject}"
        words = ["Investigate"]
        if analysis.environment == "production":
            words.append("production")
        if problem == "outage" and analysis.product:
            words.append(f"{analysis.product} outage")
        else:
            words.append(problem)
        trigger = _TRIGGER_RE.search(analysis.text)
        if trigger:
            change = trigger.group(0).split()[-1].lower()
            words.append(f"after {'deployment' if change == 'deploy' else change}")
        elif analysis.product and analysis.product.lower() not in " ".join(words).lower():
            words.append(f"in {analysis.product}")
        return " ".join(words)
    if category is EmailCategory.SERVICE_REQUEST:
        labels = {rule.label for rule, _ in analysis.matched[category]}
        if "account or credential help" in labels:
            return f"Reset password or unlock account for {requester}"
        if "access request" in labels:
            target = analysis.product or (m.group(1) if (m := _KNOWN_SYSTEMS_RE.search(analysis.text)) else None)
            return f"Grant {target} access for {requester}" if target else f"Grant requested access for {requester}"
        return f"Fulfil service request: {subject}"
    if category is EmailCategory.TASK_REQUEST:
        for sentence in analysis.sentences:
            match = _TASK_PHRASE_RE.search(sentence)
            if match:
                phrase = _TASK_CUT_RE.sub("", match.group(1)).strip(" .?!")
                if len(phrase.split()) >= 2:
                    return phrase[0].upper() + phrase[1:]
        return f"Complete requested task: {subject}"
    if category is EmailCategory.FEATURE_REQUEST:
        return f"Evaluate feature request: {subject}"
    if category is EmailCategory.FOLLOW_UP:
        return f"Follow up on: {subject}"
    if category is EmailCategory.INFORMATION_ONLY:
        return f"Review announcement: {subject}"
    if category is EmailCategory.SPAM_OR_IRRELEVANT:
        return f"Discard unsolicited email: {subject}"
    return f"Review email: {subject}"


class HeuristicAnalysisAgent:
    """Rule-based `EmailAnalysisAgent`. Deterministic and offline; not a substitute for an LLM."""

    provider_name: str = PROVIDER_NAME
    model_name: str = MODEL_NAME
    prompt_version: str = PROMPT_VERSION

    async def classify(self, email: EmailContext, taxonomy: TaxonomyConfig) -> ClassificationResult:
        started = time.perf_counter()
        analysis = _analyze(email)
        category, top, second = _choose_category(analysis, taxonomy.enabled_categories)
        priority, urgency, notes = _priority_and_urgency(category, analysis)
        missing = _missing_information(category, analysis)
        warnings: list[str] = []
        if email.injection_flags:
            warnings.append("The email contains text resembling instructions to an automated system; it was ignored.")
        if email.truncated:
            warnings.append("The email body was truncated before analysis.")
        output = ClassificationOutput(
            category=category,
            rationale=_rationale(category, analysis, notes),
            urgency=urgency,
            priority=priority,
            action_recommended=category in _ACTIONABLE or category is EmailCategory.FOLLOW_UP or bool(missing),
            needs_more_info=bool(missing),
            missing_information=missing,
            extraction_warnings=warnings,
            confidence=_confidence(category, top, second),
        )
        return ClassificationResult(output=output, usage=_usage(started))

    async def extract(self, email: EmailContext, classification: ClassificationOutput) -> ExtractionResult:
        started = time.perf_counter()
        analysis = _analyze(email)
        category = classification.category
        content = _content_sentences(analysis.sentences)
        deadline, deadline_sentence = _deadline(analysis, email.received_at.date())
        impact = next(
            (s for s in content if re.search(r"\b(?:affect\w*|impact\w*)\b", s, _I)),
            next((s for s in content if _IMPACT_RE.search(s)), None),
        )
        if category not in _ACTIONABLE:
            impact = None
        evidence: list[EvidenceSnippet] = []
        for rule, _hit in analysis.matched.get(category, [])[:3]:
            quote = next((s for s in [analysis.subject, *analysis.sentences] if s and rule.pattern.search(s)), None)
            if quote and all(e.quote != quote[:200] for e in evidence):
                evidence.append(EvidenceSnippet(field="category", quote=quote[:200]))
        if impact:
            evidence.append(EvidenceSnippet(field="impact", quote=impact[:200]))
        urgent_sentence = next((s for s in analysis.sentences if _URGENT_RE.search(s)), None)
        if urgent_sentence:
            evidence.append(EvidenceSnippet(field="urgency", quote=urgent_sentence[:200]))
        if deadline_sentence:
            evidence.append(EvidenceSnippet(field="deadline", quote=deadline_sentence[:200]))

        owner = None
        if not email.injection_flags and (m := _OWNER_RE.search(analysis.text)) and m.group(1) not in _NOT_NAMES:
            owner = m.group(1)
        team = None
        for m in _TEAM_RE.finditer("\n".join(content)):
            if m.group(1).split()[0] not in _NOT_NAMES:
                team = f"{m.group(1)} team"
                break
        organization = None
        if m := _ORG_RE.search("\n".join(content)):
            organization = (m.group(1) or m.group(2) or "").strip(" .") or None

        references = list(dict.fromkeys(u.rstrip(".,;:!?") for u in _URL_RE.findall(analysis.text)))
        references += [t for t in dict.fromkeys(_TICKET_RE.findall(analysis.text)) if t not in references]

        requester_email = None
        if email.sender_address:
            try:
                requester_email = validate_email(email.sender_address)[1]
            except ValueError:
                requester_email = None
        description = " ".join(content)[:1500] or email.subject
        output = ExtractionOutput(
            summary=_clip(_summary(category, analysis, email), 120),
            description=description,
            requester_name=email.sender_name,
            requester_email=requester_email,
            organization=organization,
            product_or_system=analysis.product,
            environment=analysis.environment,
            impact=impact[:500] if impact else None,
            deadline=deadline,
            suggested_owner=owner,
            suggested_team=team,
            references=references[:20],
            suggested_work_item_type=_ADO_DEFAULTS.category_work_item_types.get(category),
            proposed_fields=[],
            evidence=evidence,
        )
        return ExtractionResult(output=output, usage=_usage(started))

    async def plan(
        self,
        email: EmailContext,
        classification: ClassificationOutput,
        extraction: ExtractionOutput,
        context: PlanningContext,
    ) -> PlanningResult:
        started = time.perf_counter()
        offered = offered_tools(context)
        completed = completed_action_types(context.already_completed_actions)
        proposals = self._proposals(
            email, classification, extraction, offered=offered, completed=completed, context=context
        )
        calls: list[tuple[str, object]] = [
            (name, args.model_dump(mode="json")) for name, args in proposals if name in offered
        ]
        accepted, rejected = collect_tool_calls(calls, offered, completed=completed)
        return PlanningResult(tool_calls=accepted, rejected_calls=rejected, usage=_usage(started))

    def _proposals(
        self,
        email: EmailContext,
        classification: ClassificationOutput,
        extraction: ExtractionOutput,
        *,
        offered: ToolRegistry,
        completed: set[ActionType],
        context: PlanningContext,
    ) -> list[tuple[str, BaseModel]]:
        category = classification.category
        subject = _plain_subject(email)
        greeting = f"Hello {_first_name(email)},"
        proposals: list[tuple[str, BaseModel]] = []

        if classification.needs_more_info and category in _ACTIONABLE:
            items = "\n".join(f"- {item}" for item in classification.missing_information) or "- more details"
            proposals.append(
                (
                    "propose_email_reply",
                    ProposeEmailReplyArgs(
                        subject=f"Re: {subject}"[:255],
                        body=f"{greeting}\n\nThank you for your message. To help us look into this, could you please "
                        f"share the following information:\n{items}\n\nKind regards",
                        rationale="Essential information is missing, so the sender is asked for it first.",
                        expected_impact="The sender provides the details needed to act on the request.",
                    ),
                )
            )
            return proposals

        if category in _ACTIONABLE:
            work_item_offered = (
                "propose_azure_devops_work_item" in offered
                and ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM not in completed
            )
            if work_item_offered:
                proposals.append(
                    ("propose_azure_devops_work_item", self._work_item(email, classification, extraction, context))
                )
            elif ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM not in completed:
                proposals.append(("propose_follow_up_task", self._follow_up(classification, extraction, subject)))
            if category in _ACK_CATEGORIES:
                proposals.append(
                    (
                        "propose_email_reply",
                        ProposeEmailReplyArgs(
                            subject=f"Re: {subject}"[:255],
                            body=f"{greeting}\n\nThank you for your message. We have received it and our team will "
                            "review it. We will contact you if we need more information.\n\nKind regards",
                            rationale="The sender should know that the request was received and is being reviewed.",
                            expected_impact="The sender receives a timely acknowledgement.",
                        ),
                    )
                )
        elif category is EmailCategory.FOLLOW_UP:
            proposals.append(("propose_follow_up_task", self._follow_up(classification, extraction, subject)))
        else:
            reasons = {
                EmailCategory.INFORMATION_ONLY: "The email is informational and requests no action.",
                EmailCategory.SPAM_OR_IRRELEVANT: "The email appears to be spam or unrelated to our work.",
            }
            proposals.append(
                (
                    "propose_no_action",
                    ProposeNoActionArgs(
                        reason=reasons.get(category, "No actionable request was identified in the email."),
                        rationale=f"Classified as {_CATEGORY_LABELS[category]}.",
                        expected_impact="The email is closed without creating work.",
                    ),
                )
            )
        return proposals

    @staticmethod
    def _work_item(
        email: EmailContext,
        classification: ClassificationOutput,
        extraction: ExtractionOutput,
        context: PlanningContext,
    ) -> ProposeAzureDevOpsWorkItemArgs:
        allowed = context.allowed_work_item_types
        candidates = [
            extraction.suggested_work_item_type,
            context.category_work_item_type,
            context.default_work_item_type,
        ]
        work_item_type = next((c for c in candidates if c and (not allowed or c in allowed)), None)
        work_item_type = work_item_type or (allowed[0] if allowed else context.default_work_item_type)
        lines = [extraction.description]
        for label, value in (
            ("Impact", extraction.impact),
            ("Environment", extraction.environment),
            ("Product/system", extraction.product_or_system),
            ("Deadline", extraction.deadline),
        ):
            if value:
                lines.append(f"{label}: {value}")
        sender = email.sender_address or email.sender_name or "unknown sender"
        lines.append(f"Source: email from {sender} received {email.received_at.date().isoformat()}.")
        tags = ["nuvexa", classification.category.value.replace("_", "-")]
        if extraction.environment:
            tags.append(re.sub(r"[^A-Za-z0-9-]", "-", extraction.environment)[:30])
        return ProposeAzureDevOpsWorkItemArgs(
            title=_clip(extraction.summary or email.subject, 120),
            description="\n\n".join(lines),
            work_item_type=work_item_type,
            priority=classification.priority,
            tags=tags[:5],
            rationale=f"The email describes {_CATEGORY_LABELS[classification.category]} work that should be tracked.",
            expected_impact="The request is tracked in Azure DevOps after human approval.",
        )

    @staticmethod
    def _follow_up(
        classification: ClassificationOutput, extraction: ExtractionOutput, subject: str
    ) -> ProposeFollowUpTaskArgs:
        due = extraction.deadline[:10] if extraction.deadline else None
        return ProposeFollowUpTaskArgs(
            title=_clip(f"Follow up: {subject}", 120),
            description=extraction.description[:2000],
            due_date=due,
            rationale=f"The {_CATEGORY_LABELS[classification.category]} email needs a response from the team.",
            expected_impact="The sender receives an update and the item does not get lost.",
        )
