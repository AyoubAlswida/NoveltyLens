"""Sanitize untrusted exception text at the **final** persistence and logging
boundary.

**This module assumes every provider gets it wrong.** That is the whole point.
`search/google.py`'s `_raise_for_status` already strips the SerpAPI key from its
own errors, and it should -- but a defence that exists only at the provider
boundary fails the moment a new provider is added, a library is upgraded, or an
exception takes a path nobody anticipated. `workflow/executor.py` persists a
failure's text into a terminal `error` event and into
`verification_runs.failure_detail`, both of which are read back by the API and
rendered to a browser. That is the last place anything can be stopped, so it is
checked here too.

The defect this closes was real and reproducible: an ordinary quota failure

    429 Client Error for url: https://serpapi.com/search?api_key=SECRET123&q=x

went through `_safe_detail` unchanged and would have been written to PostgreSQL
and served back out. CLAUDE.md already records that exactly this leaked a live
key into a devtools console once, through the SSE path.

**What is preserved.** A provider quota message is what a user actually needs --
"the provider returned HTTP 429" tells them which limit they hit. So the safe
*facts* survive: the HTTP status, a recognised provider name, the workflow step,
and a stable failure code. What does not survive is anything that could carry a
credential.

**Two layers, deliberately:**

1. :func:`redact` rewrites known credential shapes wherever they appear.
2. :func:`safe_detail` additionally refuses to pass through the message of an
   **untrusted** exception type at all, rebuilding it from allow-listed parts.
   Redaction is pattern-matching and patterns are never complete; an exception
   this codebase did not raise gets nothing but the facts we extracted on
   purpose.

Nothing here converts a failure into a success or a degraded result. A sanitized
failure is still a failure, with its code, and it still fails the run.
"""

import re
from typing import Optional

REDACTED = "[REDACTED]"

MAX_DETAIL_CHARS = 300
"""Longer than any useful failure sentence and far shorter than a provider
response body. A JSON or XML body that survived every pattern below is still cut
off here, because "we never persist a provider body" cannot depend on having
predicted its shape."""


CREDENTIAL_PARAMETERS = (
    "api_key",
    "apikey",
    "api-key",
    "key",
    "token",
    "access_token",
    "accesstoken",
    "refresh_token",
    "id_token",
    "secret",
    "client_secret",
    "clientsecret",
    "client_id",
    "password",
    "passwd",
    "pwd",
    "sslpassword",
    "authorization",
    "auth",
    "signature",
    "sig",
    "session",
    "cookie",
)
"""Parameter names whose value is a credential, matched case-insensitively.

Deliberately broad, and deliberately including `key` on its own: over-redacting a
harmless `?key=exhaust-heat` costs a reader one word of context, while
under-redacting a live `?key=sk-...` costs a credential. The asymmetry is not
close.
"""

_PARAM_NAMES = "|".join(sorted(CREDENTIAL_PARAMETERS, key=len, reverse=True))

_QUERY_CREDENTIAL = re.compile(
    # name = value, in a query string, a fragment, or bare prose. The value runs
    # to the next delimiter, so a percent-encoded secret (%2F, %2B) is consumed
    # whole rather than being split at a character the encoding introduced.
    #
    # The lookahead skips a value an earlier rule already replaced. Without it
    # this rule re-matched `[REDACTED]`, stopped at the `]` in its own exclusion
    # class, and left a stray bracket behind -- cosmetic on its own, but the same
    # double-processing is what let the Authorization case below leak.
    rf"(?i)\b({_PARAM_NAMES})(\s*[=:]\s*)(?!\[REDACTED\])([^&\s\"'<>,;)\]}}]+)"
)

_DSN_USERINFO = re.compile(
    # scheme://user:password@host -- the password half of a connection string.
    r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@"
)

_BEARER = re.compile(r"(?i)\b(bearer|basic|digest)\s+([A-Za-z0-9._\-~+/=]{4,})")

_AUTHORIZATION_HEADER = re.compile(
    # The optional auth scheme is consumed together with the credential after it.
    # Matching only as far as the first space replaced the word `Bearer` and left
    # the token sitting immediately after it -- which is worse than not matching
    # at all, because the result *looks* redacted.
    r"(?i)\b(authorization|proxy-authorization|x-api-key|api-key)(\s*:\s*)"
    r"(?:(?:bearer|basic|digest|token)\s+)?([^\s,;]+)"
)

_BODY_START = re.compile(
    # A body serialized as JSON, XML, HTML -- or as a **Python dict repr**, which
    # is how the `openai` SDK renders an error body into `str(error)`:
    #
    #     Error code: 429 - {'error': {'message': 'Rate limit reached ... in
    #     organization org-XXXX...'}}
    #
    # This pattern originally recognised only double-quoted JSON, so that body
    # was not cut, and `org-...` is not a credential shape any other rule matches
    # -- so an account identifier reached stdout through `safe_log`. Observed for
    # real on 2026-08-28 during the capability gate. A body is dropped whichever
    # dialect it arrives in.
    r"(\{\s*[\"']|<\?xml|<html|<!DOCTYPE|\[\s*\{)",
    re.IGNORECASE,
)
"""Where a provider response body begins. Everything from here on is dropped
rather than redacted: a body has no safe prefix worth keeping, and scanning one
for credentials is a losing game."""

_ACCOUNT_IDENTIFIER = re.compile(
    # Account-scoped identifiers a provider volunteers in an error: OpenAI's
    # `org-...` and `proj_...`, and the request/user ids several providers echo.
    # Not credentials -- they authenticate nothing -- but they identify the
    # account, they are never needed to act on a failure, and they end up in log
    # aggregators and pasted transcripts. Removed as defence in depth *behind*
    # the body cut above, so that a body arriving in a dialect nobody predicted
    # still cannot carry one through.
    r"(?i)\b((?:org|proj|project|user|req|acct|account|session)[-_][A-Za-z0-9]{6,})"
)

_HTTP_STATUS = re.compile(r"\b([1-5]\d{2})\b(?=\s*(client|server)?\s*error|\s|$|:)")

KNOWN_PROVIDERS = {
    "serpapi.com": "SerpAPI",
    "api.openai.com": "OpenAI",
    "api.lens.org": "Lens.org",
    "ops.epo.org": "EPO OPS",
    "router.huggingface.co": "the HuggingFace Router",
    "huggingface.co": "HuggingFace",
    "aramco.com.sa": "the Aramco gateway",
}
"""Hostnames safe to name in a failure message. A hostname is not a credential,
and knowing *which* provider failed is most of what makes a failure actionable.
Anything not on this list is not named at all -- an unrecognised host could be an
internal address."""


def redact(text: str) -> str:
    """Rewrite every known credential shape in one line of text.

    Order matters: the body is cut first (so nothing inside it is scanned or
    partially rewritten), then DSN userinfo, then headers and bearer schemes,
    then bare `name=value` pairs -- broadest last, so a more specific rule has
    already claimed what it should.
    """
    if not text:
        return ""

    # A provider body has no safe prefix worth keeping.
    body = _BODY_START.search(text)
    if body:
        text = text[: body.start()].rstrip() + " " + REDACTED

    text = _DSN_USERINFO.sub(rf"\1\2:{REDACTED}@", text)
    text = _AUTHORIZATION_HEADER.sub(rf"\1\2{REDACTED}", text)
    text = _BEARER.sub(rf"\1 {REDACTED}", text)
    text = _QUERY_CREDENTIAL.sub(rf"\1\2{REDACTED}", text)
    text = _ACCOUNT_IDENTIFIER.sub(REDACTED, text)
    return text


def first_line(text: str) -> str:
    """The first line only.

    Everything after it is a traceback, a plpgsql `CONTEXT:` stack, or a response
    body -- three different things that are all the same thing here: internal
    detail that must not be persisted or shown.
    """
    lines = str(text).strip().splitlines()
    return lines[0].strip() if lines else ""


def http_status(text: str) -> Optional[int]:
    """The HTTP status a message mentions, if any.

    Kept because it is the single most useful safe fact in a provider failure: a
    429 and a 402 send a user to two different places.
    """
    match = _HTTP_STATUS.search(text)
    if match is None:
        return None
    status = int(match.group(1))
    return status if 100 <= status <= 599 else None


def provider_name(text: str) -> Optional[str]:
    """A recognised provider named in the message, or None.

    Matched against an allow-list of hostnames **and product names** rather than
    parsed out of the URL, because an unrecognised host might be an internal
    address and naming it is itself a small disclosure. The product names are
    matched too because a provider wrapper often says "SerpAPI returned 429"
    without a URL at all, and losing the provider from that message would throw
    away the most useful half of it.
    """
    lowered = text.lower()
    for host, name in KNOWN_PROVIDERS.items():
        if host in lowered or name.lower().removeprefix("the ") in lowered:
            return name
    return None


PROVIDER_MODULES = {
    "openai": "OpenAI",
    "requests": None,
    "psycopg": None,
}
"""Root package names that identify the provider an exception came from.

A `None` means "this library is not a provider" -- `requests` raises for every
HTTP client in the project, so its name says nothing about who failed.

This is matched against the exception's **class hierarchy**, not its message, and
that makes it strictly better evidence than `provider_name`: an `openai` SDK error
is an OpenAI error whether or not its prose happens to contain the word. It was
added because the most useful provider message in the project -- a rate limit --
names the *model* and never the vendor, so a body-cut message correctly reduced to
"an upstream call returned HTTP 429" and lost the one fact that made it
actionable.
"""


def provider_from_exception(error: BaseException) -> Optional[str]:
    """The provider named by an exception's class hierarchy, or None.

    Walks the MRO rather than reading `type(error).__module__`, so a subclass
    defined elsewhere -- a wrapper, a test double, a vendored shim -- is still
    recognised by what it inherits from.
    """
    for klass in type(error).__mro__:
        root = (getattr(klass, "__module__", "") or "").split(".")[0]
        name = PROVIDER_MODULES.get(root)
        if name:
            return name
    return None


def safe_detail(
    error: BaseException,
    *,
    step: Optional[str] = None,
    trusted: bool = False,
) -> str:
    """One sanitized sentence, safe to persist, log, and serve to a browser.

    Args:
        error: the exception. Its text is treated as hostile.
        step: the workflow step or node, if known. Safe and useful context.
        trusted: whether this exception's message is known to be constructed by
            this codebase from safe parts. `WorkflowError` and its subclasses
            qualify -- the SQL functions interpolate run ids, statuses and
            tokens and nothing else. A trusted message is redacted and passed
            through; an untrusted one is **rebuilt** from the facts extracted
            above and nothing else, because redaction is pattern-matching and
            patterns are never complete.

    Returns:
        A single line, at most :data:`MAX_DETAIL_CHARS` long, with no traceback,
        no response body, and no credential.
    """
    raw = first_line(error)
    kind = type(error).__name__

    if trusted:
        detail = redact(raw) or kind
        return detail[:MAX_DETAIL_CHARS]

    # Untrusted: keep the facts, discard the prose.
    status = http_status(raw)
    # The class hierarchy is stronger evidence than the prose, so it is consulted
    # first; the message allow-list still covers wrappers that raise a plain
    # RuntimeError naming a host.
    provider = provider_from_exception(error) or provider_name(raw)

    parts: list[str] = []
    if provider and status:
        parts.append(f"{provider} returned HTTP {status}")
    elif provider:
        parts.append(f"{provider} failed")
    elif status:
        parts.append(f"an upstream call returned HTTP {status}")
    else:
        parts.append(f"an unexpected {kind} occurred")
    if step:
        parts.append(f"during {step}")
    return (" ".join(parts) + ".")[:MAX_DETAIL_CHARS]


_MACHINE_TOKEN = re.compile(r"^[a-z][a-z0-9_.\-]{0,63}$")
"""A provider's machine-readable error identifier -- `rate_limit_exceeded`,
`invalid_request_error`. Lower-case, no spaces, bounded. Free-form prose cannot
match it, which is the point: these fields are allow-listed by *shape* as well as
by name, so a provider that one day puts a sentence in `code` contributes
nothing instead of contributing a sentence."""

_SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")
"""A PostgreSQL SQLSTATE: exactly five upper-case alphanumerics, by definition."""


def error_facts(error: BaseException) -> dict[str, str]:
    """The allow-listed, machine-readable facts an exception carries.

    Named fields only, each additionally required to *look* like an identifier.
    Nothing here reads a provider's `message`, and nothing reads a response body
    -- those are the fields that carry account metadata, quota descriptions and
    request context, and no amount of pattern-matching makes them safe to keep.
    """
    facts: dict[str, str] = {}

    status = getattr(error, "status_code", None)
    if status is None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    if isinstance(status, int) and 100 <= status <= 599:
        facts["status"] = str(status)

    for name in ("code", "type"):
        value = getattr(error, name, None)
        if isinstance(value, str) and _MACHINE_TOKEN.match(value):
            facts[name] = value

    # PostgreSQL's SQLSTATE. Five characters, machine-readable, and the single
    # most useful fact in a driver failure: `08006` (connection failure) and
    # `28P01` (invalid password) send an operator to two completely different
    # places, and that is exactly the distinction that used to be carried by
    # passing the driver's prose through -- which is a body by another name.
    sqlstate = getattr(error, "sqlstate", None)
    if isinstance(sqlstate, str) and _SQLSTATE.match(sqlstate):
        facts["sqlstate"] = sqlstate

    return facts


def safe_log(error: BaseException) -> str:
    """A one-liner for an operator's log, built from allow-listed fields only.

    **It does not pass a provider message through, redacted or otherwise.** It
    used to: ``redact(first_line(error))``, which is pattern-matching over
    hostile text, and patterns are never complete. On 2026-08-28 the incomplete
    pattern was the body cut -- it recognised double-quoted JSON but not the
    Python dict repr the `openai` SDK produces -- so an HTTP 429 body printed the
    account's organization id to stdout. Both halves are fixed: the body cut
    handles that dialect now, *and* this function no longer relies on it.

    What an operator gets instead is the exception type plus the provider's own
    structured fields (`status`, `code`, `type`) and the recognised provider
    name. `RateLimitError status=429 code=rate_limit_exceeded` is the whole
    actionable content of that error, and it cannot carry anything else.
    """
    parts = [type(error).__name__]
    facts = error_facts(error)

    provider = provider_name(first_line(error))
    if provider:
        facts["provider"] = provider

    if facts:
        parts.append(" ".join(f"{key}={value}" for key, value in sorted(facts.items())))
    return " ".join(parts)[:MAX_DETAIL_CHARS]
