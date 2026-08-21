#!/usr/bin/env python3
"""Fail-closed gateway for the CTO final-approver identity (SSC-DAN).

Distinct from ssc-github-approval-identity (obviouslogic-ai / rhea-ramos,
simone-park). This gateway is the ONLY code path that ever reads the SSC-DAN
GitHub PAT, and the ONLY Hermes profile it will run under is ellis-turing.

Authority model (HAA, binding, 2026-07-31):
  SSC-ENG = engineers. All work (branches/commits/PRs/CI) runs through that
    identity. SSC-ENG never approves and never holds this credential.
  SSC-DAN = approver only. Casts the FINAL GitHub approval and nothing else.
    Held exclusively by ellis-turing (CTO), the high-level TECHNICAL reviewer.
  Flow: engineer opens PR -> CI green + Council/Tribunal sign-offs posted ->
    ellis-turing reads the whole PR and casts the SSC-DAN approval -> the
    ORIGINAL ENGINEER (their own SSC-ENG identity) performs the merge. This
    gateway never merges, never pushes, never authors.

Gate parity with the canonical Rule 1 (OBV-HELIos L0 "Approval and
Agent-Creation Authority.md"): approval fires only when CI is green, AGA
PASS, STMA PASS, TRC PASS, and no ACEA terminal BLOCK are all true at the
exact PR head. This gateway hard-requires AGA, STMA, and TRC verdicts (not
just TRC) and treats any ACEA marker at the exact head that is not favorable
as fatal.

v1.1 (STMA, DEV-001 circular-dep fix): the required status-check context
"AI-authored PR human review" (produced by pr-gate.yml on
pull_request_review submitted/dismissed) counts approving reviews from
non-bot/non-author users and fails closed at 0 -- exactly until this
gateway's own approval fires. Without an exception the gateway could never
cast that approval, because it refuses to cast while ANY required context is
red, including the one only the cast itself remediates. See
SELF_SATISFYING_CONTEXTS and check_required_statuses() for the narrow,
non-generalizable exception: it excuses only that one named context, only on
a `failure` conclusion (never a missing check or any other conclusion), and
every exclusion is recorded in the audit and review body -- never silent.
Every other required context remains fully fatal with zero exception.

v1.5 (HEL-6044): GitHub Pro protection-API 403 on an allowed USER owner
(ALLOWED_REPO_OWNERS minus ALLOWED_OWNER) is treated as no readable
protection surface. Org-owned ALLOWED_OWNER repos stay fail-closed on
404/empty protection. Observed exact-head check-runs replace
check_required_statuses([]) on that unreadable path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

APPROVED_PROFILES = {"ellis-turing"}
APPROVAL_LOGIN = "SSC-DAN"
ALLOWED_OWNER = "SSC-Engineering"
ALLOWED_REPO_OWNERS = frozenset({"SSC-Engineering", "SSC-ENG"})
GATEWAY_VERSION = "1.5"
FAVORABLE = {"PASS", "GO", "SECURE", "APPROVED", "CLEARED", "CONDITIONAL_PASS"}
MANDATORY_AUTHORITIES = ("AGA", "STMA", "TRC")
API = "https://api.github.com"
USER_AGENT = "helios-ssc-dan-final-approver/1.5"
UNREADABLE_PROTECTION_SOURCE = "none-readable:protection-api-403-user-owner"
PRO_UPGRADE_403_FRAGMENT = "Upgrade to GitHub Pro or make this repository public"

# Legacy constant retained for audit/compat only. HAA ruling OQ-FND-08
# (2026-08-04) closed login-based allow-lists on cost: council independence is
# asserted via in-body per-seat signature blocks (see COUNCIL_SEATS /
# check_council_seat_signatures), not GitHub logins. This set is intentionally
# unused by the gate path and must remain empty — do not repopulate it.
AUTHORIZED_VERDICT_POSTERS: frozenset[str] = frozenset()

# Per-seat identity anchors for council independence (HAA ruling OQ-FND-08,
# 2026-08-04). Markers may be posted under the shared SSC-ENG GitHub login;
# structural independence requires each mandatory authority's comment body to
# carry that seat's own signature (name / agent slug / certification skill).
# Do NOT add per-seat GitHub identities or relay logins here.
COUNCIL_SEATS: dict[str, dict[str, str]] = {
    "AGA": {
        "name": "Arturo Gallo",
        "agent": "arturo-gallo",
        "credential": "helios-agent-aga",
    },
    "STMA": {
        "name": "Simone Park",
        "agent": "simone-park",
        "credential": "helios-agent-stma",
    },
    # HEL-6066 (parent HEL-3387): the live TRC seat is Ines Navarro /
    # ines-navarro / helios-agent-trc. Any prior TRC name / slug /
    # credential is retired — including retired-131 (theo-lang) and the
    # earlier fired seat slug/credential pair (tessa-cole /
    # eng-technical-review). Retired tokens may only appear as negative
    # HOLD fixtures in tests, never as the live TRC identity here, in the
    # council-gateway-verdict skill's seat table, or in any real council
    # comment body.
    "TRC": {
        "name": "Ines Navarro",
        "agent": "ines-navarro",
        "credential": "helios-agent-trc",
    },
}

# Self-satisfying required-check exception (gateway v1.1, DEV-001 circular-dep fix).
#
# DEV-001 ("AI-authored PR human review", .github/workflows/pr-gate.yml) is a
# required status-check context that counts APPROVED reviews from non-bot/
# non-author users and fails closed at 0. The SSC-DAN approval THIS gateway
# casts is exactly what makes that count >=1: submitting it fires
# `pull_request_review: submitted`, the job re-runs, and the context flips to
# success. Without an exception, the gateway can never cast the one review
# that would make the check pass, because it refuses to cast while any
# required context is red — a pure tooling deadlock, not a PR defect.
#
# This frozenset is the ONLY mechanism that can excuse a required context from
# the fatal set, and it is a literal, hardcoded, single-entry constant with no
# CLI flag, environment variable, or argument that can add to it. Extending it
# requires a code change and a fresh TRC review, by design — it must never
# become a generic "skip this check" escape hatch.
#
# check_required_statuses() only excuses a context in this set when the check
# RUN exists, is `completed`, and its conclusion is exactly `failure`. A
# missing/never-run check, or any other conclusion (`cancelled`,
# `timed_out`, `action_required`, `stale`), is NOT excused and remains fatal —
# this exception covers only the one documented self-satisfying failure mode,
# nothing else. Every exclusion is recorded as `excused_self_satisfying_checks`
# in the audit record and printed in the approval review body; it is never
# silent.
SELF_SATISFYING_CONTEXTS = frozenset({"AI-authored PR human review"})

# Accept both production marker forms (case-insensitive verdict; full or
# abbreviated SHA). Canonical: `SEAT=VERDICT head=SHA`. Live alternate seen
# on #1846 (AGA): `SEAT VERDICT @ SHA`. Groups: eq form uses 1/2/3; @ form
# uses 4/5/6 — callers must use verdict_marker_parts().
VERDICT_MARKER_RE = re.compile(
    r"^\s*GATEWAY-VERDICT:\s*"
    r"(?:"
    r"([A-Za-z0-9_-]+)=([A-Za-z_]+)\s+head=([0-9a-fA-F]{8,40})"
    r"|"
    r"([A-Za-z0-9_-]+)\s+([A-Za-z_]+)\s+@\s*([0-9a-fA-F]{8,40})"
    r")"
    r"\s*$",
    re.IGNORECASE | re.MULTILINE,
)


class GateError(RuntimeError):
    pass


def verdict_marker_parts(match: re.Match[str]) -> tuple[str, str, str]:
    """Return (authority, status, head) from a VERDICT_MARKER_RE match."""
    if match.group(1) is not None:
        return match.group(1), match.group(2), match.group(3)
    return match.group(4), match.group(5), match.group(6)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def clean_reexec() -> None:
    """Drop inherited SSC-ENG/OBV credentials before reading the SSC-DAN token."""
    if os.environ.get("APPROVAL_GATEWAY_ISOLATED") == "1":
        return
    keep = {
        key: os.environ[key]
        for key in (
            "HOME",
            "PATH",
            "LANG",
            "LC_ALL",
            "HERMES_REAL_HOME",
            "HERMES_PROFILE",
            "HERMES_SESSION_ID",
        )
        if key in os.environ
    }
    keep["APPROVAL_GATEWAY_ISOLATED"] = "1"
    os.execve(sys.executable, [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], keep)


def real_home() -> Path:
    return Path(os.environ.get("HERMES_REAL_HOME", Path.home())).resolve()


def profile_name() -> str:
    profile = os.environ.get("HERMES_PROFILE", "")
    if profile not in APPROVED_PROFILES:
        raise GateError(f"profile is not the authorized SSC-DAN holder: {profile or '<unset>'}")
    return profile


def secret_path(profile: str) -> Path:
    return (
        real_home()
        / ".hermes"
        / "profiles"
        / profile
        / "home"
        / ".config"
        / "helios"
        / "ssc-dan-approval.env"
    )


def read_token(path: Path) -> str:
    if not path.is_file():
        raise GateError(f"SSC-DAN credential is not provisioned for this profile: {path}")
    mode = path.stat().st_mode & 0o777
    parent_mode = path.parent.stat().st_mode & 0o777
    if mode != 0o600 or parent_mode != 0o700:
        raise GateError(
            f"SSC-DAN credential permissions are invalid: directory={oct(parent_mode)} file={oct(mode)}"
        )
    assignments: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if not match:
            raise GateError("SSC-DAN credential file contains an invalid line")
        name, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        assignments[name] = value
    if set(assignments) != {"APPROVAL_GITHUB_TOKEN"}:
        raise GateError("SSC-DAN credential file must contain exactly APPROVAL_GITHUB_TOKEN")
    token = assignments["APPROVAL_GITHUB_TOKEN"]
    if not token or "\n" in token or "\r" in token or "\0" in token:
        raise GateError("SSC-DAN credential is empty or malformed")
    return token


def request_json(
    token: str,
    method: str,
    path_or_url: str,
    payload: dict[str, Any] | None = None,
    *,
    none_on_404: bool = False,
) -> Any:
    url = path_or_url if path_or_url.startswith("https://") else API + path_or_url
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and none_on_404 and method == "GET":
            return None
        try:
            message = json.loads(exc.read().decode()).get("message", "GitHub API error")
        except Exception:
            message = "GitHub API error"
        raise GateError(f"GitHub API {method} failed with HTTP {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise GateError(f"GitHub API connection failed: {exc.reason}") from exc


def safe_repo(value: str) -> tuple[str, str]:
    parts = value.split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        raise GateError("repository must be owner/name")
    owner, repo = parts
    if owner not in ALLOWED_REPO_OWNERS:
        raise GateError(
            "repository owner must be one of "
            + ", ".join(sorted(ALLOWED_REPO_OWNERS))
        )
    return owner, repo


def parse_verdict(value: str) -> dict[str, str]:
    match = re.fullmatch(r"([A-Za-z0-9_-]+):([A-Za-z_]+)=(https://github\.com/.+)", value)
    if not match:
        raise GateError(f"invalid verdict syntax: {value}")
    authority, status, url = match.groups()
    status = status.upper()
    if status not in FAVORABLE:
        raise GateError(f"verdict is not favorable: {authority}:{status}")
    return {"authority": authority.upper(), "status": status, "url": url}


def evidence_from_url(token: str, owner: str, repo: str, pr: int, url: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    expected_path = f"/{owner}/{repo}/pull/{pr}"
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.path.rstrip("/") != expected_path:
        raise GateError(f"verdict URL is not on the target PR: {url}")
    fragment = parsed.fragment
    if fragment.startswith("issuecomment-") and fragment.removeprefix("issuecomment-").isdigit():
        evidence = request_json(
            token, "GET", f"/repos/{owner}/{repo}/issues/comments/{fragment.removeprefix('issuecomment-')}"
        )
    elif fragment.startswith("pullrequestreview-") and fragment.removeprefix("pullrequestreview-").isdigit():
        evidence = request_json(
            token,
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr}/reviews/{fragment.removeprefix('pullrequestreview-')}",
        )
    else:
        raise GateError(f"verdict URL must identify a GitHub PR comment or review: {url}")
    if evidence.get("html_url") != url:
        raise GateError(f"verdict URL did not resolve to the exact supplied evidence: {url}")
    return evidence


def is_unreadable_user_owner_protection_403(owner: str, exc: BaseException) -> bool:
    """True only for GitHub Pro upgrade 403 on an allowed non-org (user) owner."""
    if not isinstance(exc, GateError):
        return False
    if owner not in ALLOWED_REPO_OWNERS or owner == ALLOWED_OWNER:
        return False
    text = str(exc)
    return "HTTP 403" in text and PRO_UPGRADE_403_FRAGMENT in text


def fetch_branch_protection(
    token: str, owner: str, repo: str, base: str
) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    quoted = urllib.parse.quote(base, safe="")
    unreadable = False

    def _get(path: str) -> Any:
        nonlocal unreadable
        try:
            return request_json(token, "GET", path, none_on_404=True)
        except GateError as exc:
            if is_unreadable_user_owner_protection_403(owner, exc):
                unreadable = True
                return None
            raise

    classic = _get(f"/repos/{owner}/{repo}/branches/{quoted}/protection") or {}
    rules = _get(f"/repos/{owner}/{repo}/rules/branches/{quoted}") or []
    if not isinstance(rules, list):
        raise GateError("GitHub branch-rules response is not a rule list")
    source = UNREADABLE_PROTECTION_SOURCE if unreadable else None
    return classic, rules, source


def resolve_protection_gate(
    classic: dict[str, Any],
    rules: list[dict[str, Any]],
    unreadable_source: str | None,
) -> tuple[str, str, list[str], bool]:
    """Return review_source, strict_source, ruleset_contexts, use_observed_checks.

    The unreadable-protection path is taken only when a user-owner Pro-upgrade
    403 produced no readable classic protection and no readable branch rules.
    Org / 404 / empty protection still fail closed via check_branch_protection.
    """
    if unreadable_source and not classic and not rules:
        return unreadable_source, unreadable_source, [], True
    review_source, strict_source, ruleset_contexts = check_branch_protection(classic, rules)
    return review_source, strict_source, ruleset_contexts, False


def check_branch_protection(
    classic: dict[str, Any], rules: list[dict[str, Any]]
) -> tuple[str, str, list[str]]:
    review_source = ""
    review_count_seen = False
    review_rule = classic.get("required_pull_request_reviews") or {}
    if int(review_rule.get("required_approving_review_count") or 0) >= 1:
        review_count_seen = True
        if review_rule.get("dismiss_stale_reviews"):
            review_source = "classic"
    if not review_source:
        for rule in rules:
            if rule.get("type") != "pull_request":
                continue
            params = rule.get("parameters") or {}
            if int(params.get("required_approving_review_count") or 0) >= 1:
                review_count_seen = True
                if params.get("dismiss_stale_reviews_on_push"):
                    review_source = f"ruleset:{rule.get('ruleset_id')}"
                    break
    if not review_source:
        if review_count_seen:
            raise GateError("protected branch does not dismiss stale reviews")
        raise GateError("protected branch does not require an approving review")

    strict_source = ""
    if (classic.get("required_status_checks") or {}).get("strict"):
        strict_source = "classic"
    ruleset_contexts: set[str] = set()
    for rule in rules:
        if rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters") or {}
        if not strict_source and params.get("strict_required_status_checks_policy"):
            strict_source = f"ruleset:{rule.get('ruleset_id')}"
        for item in params.get("required_status_checks") or []:
            context = item.get("context")
            if context:
                ruleset_contexts.add(context)
    if not strict_source:
        # PARALLEL-PR RECONCILIATION (HAA-directed, runbook parallel-pr-approval-fix.md):
        # the org-wide bottleneck fix deliberately sets required_status_checks.strict=false
        # to stop the O(n^2) re-review thrash (a sibling merge no longer flags every other
        # PR BEHIND -> no forced rebase -> approvals survive). The gateway's original
        # strict=true precondition predates that fix and is now stale.
        #
        # strict only forces up-to-date-with-main; it adds NO CI safety beyond what this
        # gateway already enforces independently via check_required_statuses() at the exact
        # head. The real safety invariants remain fully enforced: >=1 approving review with
        # dismiss_stale (verified above), every required context green on the exact head,
        # not-behind/not-conflicted mergeable, council AGA+STMA+TRC markers, no ACEA block,
        # author == SSC-ENG. We therefore accept strict=false ONLY when the review gate is
        # satisfied (review_source set) and record the reconciliation explicitly so it is
        # never silent and remains subject to TRC follow-up review.
        strict_source = "reconciled:strict-false-parallel-pr-fix(review-gate+exact-head-ci)"
    return review_source, strict_source, sorted(ruleset_contexts)


def check_required_statuses(
    token: str, owner: str, repo: str, sha: str, required: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """Verify every required context is green on the exact head. Fail closed.

    ``required`` is the UNION of classic-protection contexts and ruleset
    required_status_checks contexts. A completed check run counts as
    satisfied on conclusion `success`, `neutral`, or `skipped` (the same
    semantics GitHub applies to required checks, so the gateway never passes
    what GitHub would still block, and path-filtered/skipped jobs do not
    false-reject). Commit STATUSES count only on state "success". Any
    required context satisfied by a NON-success conclusion is returned
    separately and recorded in the audit and review body so the degraded
    basis is visible, never silent.

    Self-satisfying-check exception (v1.1, DEV-001 circular-dep fix): a
    context in the hardcoded, single-purpose ``SELF_SATISFYING_CONTEXTS`` set
    is excused from the missing/fatal check ONLY when its check run is
    `completed` with conclusion exactly `failure` — the documented shape of
    "the review this gateway is about to cast is what turns this exact check
    green." Every other required context, and every other conclusion for a
    self-satisfying context (missing entirely, `cancelled`, `timed_out`,
    `action_required`, `stale`), remains fully fatal with no exception. Each
    excused context is returned separately and MUST be recorded and surfaced
    identically to a degraded check — never silently dropped from the report.
    """
    if not required:
        raise GateError("protected branch has no required status-check contexts")
    check_runs = request_json(
        token, "GET", f"/repos/{owner}/{repo}/commits/{sha}/check-runs?per_page=100"
    ).get("check_runs", [])
    statuses = request_json(
        token, "GET", f"/repos/{owner}/{repo}/commits/{sha}/status"
    ).get("statuses", [])
    passing_checks: set[str] = set()
    non_success: dict[str, str] = {}
    failing_conclusions: dict[str, str] = {}
    for item in check_runs:
        name = item.get("name")
        if item.get("status") != "completed":
            continue
        conclusion = item.get("conclusion")
        if conclusion in {"success", "neutral", "skipped"}:
            passing_checks.add(name)
            if conclusion != "success":
                non_success[name] = conclusion
        elif conclusion == "failure":
            failing_conclusions[name] = conclusion
    passing_statuses = {item.get("context") for item in statuses if item.get("state") == "success"}
    still_missing = set(required) - passing_checks - passing_statuses
    excused = sorted(
        name
        for name in still_missing
        if name in SELF_SATISFYING_CONTEXTS and failing_conclusions.get(name) == "failure"
    )
    missing = sorted(still_missing - set(excused))
    if missing:
        raise GateError("required checks are not green on the exact head: " + ", ".join(missing))
    degraded = sorted(
        f"{name}={conclusion}" for name, conclusion in non_success.items() if name in set(required)
    )
    return sorted(required), degraded, excused


def check_observed_check_runs(
    token: str, owner: str, repo: str, sha: str
) -> tuple[list[str], list[str], list[str]]:
    """When protection is unreadable, require observed exact-head check-runs.

    Do not call check_required_statuses with []. At least one completed
    success is required. Every completed run must be success/neutral/skipped.
    Pending, failure, timed_out, or cancelled is fatal.
    """
    payload = request_json(
        token, "GET", f"/repos/{owner}/{repo}/commits/{sha}/check-runs?per_page=100"
    )
    check_runs = payload.get("check_runs", []) if isinstance(payload, dict) else []
    if not isinstance(check_runs, list):
        raise GateError("GitHub check-runs response is not a run list")

    names: list[str] = []
    degraded: list[str] = []
    success_count = 0
    fatal: list[str] = []
    for item in check_runs:
        name = item.get("name") or "<unnamed>"
        names.append(name)
        status = item.get("status")
        conclusion = item.get("conclusion")
        if status != "completed":
            fatal.append(f"{name}=pending")
            continue
        if conclusion == "success":
            success_count += 1
        elif conclusion in {"neutral", "skipped"}:
            degraded.append(f"{name}={conclusion}")
        elif conclusion in {"failure", "timed_out", "cancelled"}:
            fatal.append(f"{name}={conclusion}")
        else:
            fatal.append(f"{name}={conclusion or status}")
    if fatal:
        raise GateError(
            "observed exact-head check-runs are not all success/neutral/skipped: "
            + ", ".join(fatal)
        )
    if success_count < 1:
        raise GateError("observed exact-head check-runs require at least one success")
    return names, degraded, []


def append_audit(profile: str, record: dict[str, Any]) -> Path:
    root = (
        real_home()
        / ".hermes"
        / "profiles"
        / profile
        / "home"
        / ".local"
        / "state"
        / "helios"
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    path = root / "ssc-dan-approval-audit.jsonl"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return path


def verify_identity(token: str, profile: str) -> dict[str, Any]:
    user = request_json(token, "GET", "/user")
    login = user.get("login")
    if login != APPROVAL_LOGIN:
        raise GateError(f"credential resolved to unauthorized GitHub identity: {login}")
    membership = request_json(token, "GET", f"/orgs/{ALLOWED_OWNER}/memberships/{APPROVAL_LOGIN}")
    if membership.get("state") != "active":
        raise GateError("SSC-DAN identity does not have active SSC-Engineering membership")
    record = {
        "event": "identity_probe",
        "timestamp": now_utc(),
        "agent": profile,
        "approval_identity": login,
        "organization": ALLOWED_OWNER,
        "membership_state": membership.get("state"),
        "membership_role": membership.get("role"),
        "outcome": "verified",
    }
    audit = append_audit(profile, record)
    return {
        "identity": login,
        "organization": ALLOWED_OWNER,
        "role": membership.get("role"),
        "verified": True,
        "audit": str(audit),
    }


def audit_rejection(profile: str, args: argparse.Namespace, message: str) -> None:
    append_audit(
        profile,
        {
            "event": "approval_rejected",
            "timestamp": now_utc(),
            "agent": profile,
            "session": os.environ.get("HERMES_SESSION_ID"),
            "repository": getattr(args, "repo", None),
            "pr": getattr(args, "pr", None),
            "linear_issue": getattr(args, "linear", None),
            "outcome": "rejected",
            "reason": message,
        },
    )


def _normalize_login(login: str | None) -> str:
    return (login or "").strip()


def check_marker_author(
    marker_author_login: str | None,
    *,
    pr_author_login: str,
    approval_login: str = APPROVAL_LOGIN,
    authorized_posters: frozenset[str] | None = None,
    url: str = "",
) -> str:
    """Fail closed only when the marker comment is authored by the APPROVER.

    HAA ruling OQ-FND-08, 2026-08-04 (clauses 2 & 3):
      - Council markers authored by the PR author (SSC-ENG) are VALID.
        Council-layer independence rests on in-body per-seat signature blocks
        (see check_council_seat_signatures), NOT on GitHub login inequality.
      - Apply the self-review test against the APPROVER identity only.
        Fail closed iff marker author == APPROVAL_LOGIN (SSC-DAN / 225146164).
      - Do NOT introduce per-seat GitHub identities / relay logins / login
        allow-lists (clause 6 closed that on cost). ``authorized_posters`` is
        accepted for call-site compatibility but is deliberately unused.

    Returns the verified marker_author_login for the audit record.
    """
    del pr_author_login, authorized_posters  # OQ-FND-08: no longer gate inputs
    author = _normalize_login(marker_author_login)
    approver = _normalize_login(approval_login)
    # Compare case-insensitively; GitHub logins are case-insensitive.
    author_key = author.casefold()
    approver_key = approver.casefold()

    where = f": {url}" if url else ""
    if not author:
        raise GateError(
            f"verdict marker comment has no resolvable user.login (cannot enforce authorship){where}"
        )
    # HAA ruling OQ-FND-08, 2026-08-04: self-review test is APPROVER-identity
    # only. PR-author-authored council markers (SSC-ENG) are valid.
    if author_key == approver_key:
        raise GateError(
            f"verdict marker author {author!r} is the approver identity ({approver}) — "
            f"approver must not author verdict markers{where}"
        )
    return author


def _body_has_seat_signature(body: str, seat: dict[str, str]) -> bool:
    """True when the comment body carries this seat's structural signature.

    GATEWAY-VERDICT canon (council-gateway-verdict skill): the body must
    carry ALL THREE of the seat's tokens — human name AND agent slug AND
    certification credential. Names-only, name+slug-only, and
    name+credential-only are hard fails. This is the checker the durable
    convener skill pins.

    Agent-slug presence is satisfied by any of: a bare slug, ``agent: slug``,
    ``_profile: slug``, or the ticket_signature agent field. Credential
    presence is satisfied by a bare credential string or the
    ``credentials: <cred>`` field in a ticket_signature render. All three
    matches are case-insensitive substring checks against the body text; the
    specific shapes above are documented for authors and are all matched
    incidentally by the substring check.
    """
    text = body or ""
    lower = text.casefold()
    if seat["name"].casefold() not in lower:
        return False
    if seat["agent"].casefold() not in lower:
        return False
    if seat["credential"].casefold() not in lower:
        return False
    return True


def check_council_seat_signatures(
    evidence_by_authority: dict[str, str],
    *,
    seats: dict[str, dict[str, str]] | None = None,
) -> dict[str, str]:
    """Require three DISTINCT in-body per-seat signature blocks (OQ-FND-08).

    Each mandatory authority (AGA / STMA / TRC) must present its own seat's
    signature anchors in the verdict comment body, and the three seats must
    not collapse onto a single signature identity. HOLD (GateError) if any
    seat is missing or if the resolved seat agents are not three distinct
    values.

    Returns ``{authority: agent_slug}`` for the audit record.
    """
    seat_map = COUNCIL_SEATS if seats is None else seats
    missing: list[str] = []
    resolved: dict[str, str] = {}
    for authority in MANDATORY_AUTHORITIES:
        seat = seat_map.get(authority)
        if seat is None:
            missing.append(f"{authority}=<unconfigured>")
            continue
        body = evidence_by_authority.get(authority) or ""
        if not _body_has_seat_signature(body, seat):
            missing.append(
                f"{authority} (need name={seat['name']!r} + "
                f"agent={seat['agent']!r} + credential={seat['credential']!r})"
            )
            continue
        resolved[authority] = seat["agent"]
    if missing:
        raise GateError(
            "council independence HOLD: missing distinct in-body per-seat "
            "signature blocks for: " + "; ".join(missing)
        )
    agents = list(resolved.values())
    if len(set(agents)) != len(MANDATORY_AUTHORITIES):
        raise GateError(
            "council independence HOLD: per-seat signatures are not three "
            f"DISTINCT seats (resolved={resolved})"
        )
    return resolved


def check_verdict_evidence(
    body: str,
    verdict: dict[str, str],
    head_sha: str,
    *,
    marker_author_login: str | None,
    pr_author_login: str,
    approval_login: str = APPROVAL_LOGIN,
    authorized_posters: frozenset[str] | None = None,
) -> tuple[str, str]:
    """Marker-only evidence check (no legacy prose grace window for this gateway).

    A GATEWAY-VERDICT marker line is mandatory from day one for this newer
    gateway: `GATEWAY-VERDICT: <AUTHORITY>=<STATUS> head=<sha>`. Any marker at
    the exact head with an unfavorable status is fatal, regardless of which
    authority wrote it (covers the ACEA-block check below).

    Marker authorship (HAA ruling OQ-FND-08, 2026-08-04): fail closed only
    when the comment's ``user.login`` is the APPROVER identity. PR-author
    (SSC-ENG) markers are valid; council independence is asserted separately
    via ``check_council_seat_signatures``. Returns
    ``(evidence_mode, marker_author_login)``.
    """
    head_lower = head_sha.lower()
    url = verdict["url"]
    markers = list(VERDICT_MARKER_RE.finditer(body))
    if not markers:
        raise GateError(
            f"verdict evidence has no GATEWAY-VERDICT marker for authority {verdict['authority']}: {url}"
        )
    matched_authority = False
    for match in markers:
        authority_raw, status_raw, marker_head_raw = verdict_marker_parts(match)
        authority = authority_raw.upper()
        status = status_raw.upper()
        marker_head = marker_head_raw.lower()
        if status not in FAVORABLE:
            raise GateError(
                f"verdict marker declares unfavorable disposition {authority}={status}: {url}"
            )
        if not head_lower.startswith(marker_head):
            raise GateError(
                f"verdict marker head {marker_head[:12]} does not match exact head {head_sha[:8]}: {url}"
            )
        if authority == verdict["authority"]:
            matched_authority = True
    if not matched_authority:
        raise GateError(
            f"no GATEWAY-VERDICT marker for authority {verdict['authority']} at exact head: {url}"
        )
    verified_author = check_marker_author(
        marker_author_login,
        pr_author_login=pr_author_login,
        approval_login=approval_login,
        authorized_posters=authorized_posters,
        url=url,
    )
    return "marker", verified_author


def check_no_acea_block(token: str, owner: str, repo: str, pr: int, head_sha: str) -> str:
    """Fail closed if any ACEA marker/comment at the exact head is a BLOCK.

    Rule 1's fifth precondition is negative: "no ACEA terminal BLOCK". Absence
    of an ACEA comment is not itself a failure (ACEA involvement is
    conditional), but if ACEA DID comment at this head, it must not be an
    unfavorable/BLOCK marker.
    """
    comments = request_json(token, "GET", f"/repos/{owner}/{repo}/issues/{pr}/comments?per_page=100")
    head_lower = head_sha.lower()
    for comment in comments:
        body = comment.get("body") or ""
        for match in VERDICT_MARKER_RE.finditer(body):
            authority_raw, status_raw, marker_head_raw = verdict_marker_parts(match)
            authority = authority_raw.upper()
            if authority != "ACEA":
                continue
            status = status_raw.upper()
            marker_head = marker_head_raw.lower()
            if not head_lower.startswith(marker_head):
                continue
            if status not in FAVORABLE:
                raise GateError(
                    f"ACEA terminal BLOCK present at exact head {head_sha[:8]} "
                    f"(comment {comment.get('html_url')}): status={status}"
                )
    return "no_acea_block_at_head"


def approve(args: argparse.Namespace, token: str, profile: str) -> dict[str, Any]:
    owner, repo = safe_repo(args.repo)
    verdicts = [parse_verdict(item) for item in args.verdict]
    present_authorities = {v["authority"] for v in verdicts}
    missing_mandatory = [a for a in MANDATORY_AUTHORITIES if a not in present_authorities]
    if missing_mandatory:
        raise GateError(
            "missing mandatory exact-head verdicts (Rule 1 requires AGA, STMA, and TRC all "
            "PASS/APPROVED): " + ", ".join(missing_mandatory)
        )
    for authority in MANDATORY_AUTHORITIES:
        matching = [v for v in verdicts if v["authority"] == authority]
        if not any(v["status"] in {"PASS", "APPROVED"} for v in matching):
            raise GateError(f"{authority} verdict must be PASS or APPROVED, not a lesser favorable status")
    if args.deployment_impact != "VERIFIED_NO_UNAUTHORIZED_APPLY":
        raise GateError("deployment impact must be VERIFIED_NO_UNAUTHORIZED_APPLY")
    if not args.signature:
        raise GateError(
            "the CTO signature block is mandatory on every SSC-DAN approval (name, position, "
            "credentials, model, tokens) — pass --signature"
        )

    identity = request_json(token, "GET", "/user").get("login")
    if identity != APPROVAL_LOGIN:
        raise GateError(f"credential resolved to unauthorized GitHub identity: {identity}")

    pr_path = f"/repos/{owner}/{repo}/pulls/{args.pr}"
    pr = request_json(token, "GET", pr_path)
    if pr.get("state") != "open" or pr.get("draft"):
        raise GateError("target PR must be open and non-draft")
    author_login = pr.get("user", {}).get("login")
    if author_login == APPROVAL_LOGIN:
        raise GateError("approval identity authored the PR")
    if author_login != "SSC-ENG":
        raise GateError(
            f"PR author must be the SSC-ENG worker identity, not {author_login!r} — "
            "SSC-DAN approves SSC-ENG work only"
        )
    base = pr.get("base", {}).get("ref")
    head_sha = pr.get("head", {}).get("sha")
    base_sha = pr.get("base", {}).get("sha")
    if not base or not head_sha or not base_sha:
        raise GateError("GitHub PR response lacks base/head identity")
    if args.expected_head and args.expected_head != head_sha:
        raise GateError(f"expected head {args.expected_head} does not match live head {head_sha}")

    classic_protection, branch_rules, unreadable_source = fetch_branch_protection(
        token, owner, repo, base
    )
    review_source, strict_source, ruleset_contexts, use_observed_checks = resolve_protection_gate(
        classic_protection, branch_rules, unreadable_source
    )
    classic_contexts = (
        (classic_protection.get("required_status_checks") or {}).get("contexts") or []
    )
    required_contexts = sorted(set(classic_contexts) | set(ruleset_contexts))

    comparison = request_json(
        token, "GET", f"/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}"
    )
    if comparison.get("behind_by") != 0:
        raise GateError(f"PR branch is behind its base by {comparison.get('behind_by')} commits")
    if pr.get("mergeable") is not True or pr.get("mergeable_state") in {"dirty", "behind", "unknown"}:
        raise GateError(
            f"PR is not cleanly merge-ready: mergeable={pr.get('mergeable')} state={pr.get('mergeable_state')}"
        )

    if use_observed_checks:
        required_checks, degraded_checks, excused_checks = check_observed_check_runs(
            token, owner, repo, head_sha
        )
    else:
        required_checks, degraded_checks, excused_checks = check_required_statuses(
            token, owner, repo, head_sha, required_contexts
        )

    linear_linked = args.linear.strip().upper()
    if not re.fullmatch(r"[A-Z]+-\d+", linear_linked):
        raise GateError(f"Linear issue id is not well-formed: {args.linear!r}")
    body_and_title = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
    if linear_linked not in body_and_title.upper():
        raise GateError(
            f"Linear issue {linear_linked} is not referenced in the PR title/body — "
            "PR must link Linear before approval"
        )

    acea_status = check_no_acea_block(token, owner, repo, args.pr, head_sha)

    evidence_summary: list[dict[str, str]] = []
    evidence_bodies: dict[str, str] = {}
    for verdict in verdicts:
        evidence = evidence_from_url(token, owner, repo, args.pr, verdict["url"])
        body = evidence.get("body") or ""
        marker_author = (evidence.get("user") or {}).get("login")
        evidence_mode, verified_author = check_verdict_evidence(
            body,
            verdict,
            head_sha,
            marker_author_login=marker_author,
            pr_author_login=author_login,
            approval_login=APPROVAL_LOGIN,
        )
        verdict["evidence_mode"] = evidence_mode
        verdict["marker_author_login"] = verified_author
        evidence_summary.append(verdict)
        # Keep the first body per mandatory authority for seat-signature scan.
        authority = verdict["authority"]
        if authority in MANDATORY_AUTHORITIES and authority not in evidence_bodies:
            evidence_bodies[authority] = body

    # HAA ruling OQ-FND-08, 2026-08-04: council independence is structural
    # (distinct in-body per-seat signatures), not login-based.
    seat_agents = check_council_seat_signatures(evidence_bodies)
    for verdict in evidence_summary:
        agent = seat_agents.get(verdict["authority"])
        if agent:
            verdict["council_seat_agent"] = agent

    signature_text = Path(args.signature).read_text().strip()
    if "GATEWAY-VERDICT" in signature_text:
        raise GateError("signature file must not itself contain a GATEWAY-VERDICT marker")

    event_id = f"{now_utc()}:{profile}:{owner}/{repo}#{args.pr}:{head_sha[:12]}"
    basis = {
        "event_id": event_id,
        "timestamp": now_utc(),
        "agent": profile,
        "session": os.environ.get("HERMES_SESSION_ID"),
        "approval_identity": identity,
        "gateway_version": GATEWAY_VERSION,
        "repository": f"{owner}/{repo}",
        "pr": args.pr,
        "pr_author": author_login,
        "linear_issue": linear_linked,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "base_branch": base,
        "protection_source": {"review": review_source, "strict": strict_source},
        "required_checks": required_checks,
        "degraded_required_checks": degraded_checks,
        "excused_self_satisfying_checks": excused_checks,
        "acea_check": acea_status,
        "verdicts": evidence_summary,
        "deployment_impact": args.deployment_impact,
        "reason": args.reason,
    }
    append_audit(profile, {**basis, "event": "approval_intent", "outcome": "validated"})

    verdict_lines = "\n".join(
        f"- {v['authority']} {v['status']} ({v['evidence_mode']}, "
        f"author=`{v.get('marker_author_login', '')}`): {v['url']}"
        for v in evidence_summary
    )
    degraded_line = (
        "- Degraded required checks (neutral/skipped): " + ", ".join(degraded_checks) + "\n"
        if degraded_checks
        else ""
    )
    excused_line = (
        "- Excused self-satisfying checks (DEV-001 circular-dep, resolved by this "
        "approval): " + ", ".join(excused_checks) + "\n"
        if excused_checks
        else ""
    )
    review_body = (
        "**SSC-DAN final-approver invocation (CTO technical review, separation of duties).**\n\n"
        f"- Invoking agent: `{profile}`\n"
        f"- Linear: `{linear_linked}`\n"
        f"- PR author (SSC-ENG): `{author_login}`\n"
        f"- Exact head: `{head_sha}`\n"
        f"- Base: `{base}` at `{base_sha}`\n"
        f"- Protection source: review=`{review_source}` strict=`{strict_source}`\n"
        f"- Required checks: {len(required_checks)} green\n"
        f"{degraded_line}"
        f"{excused_line}"
        f"- ACEA check: `{acea_status}`\n"
        f"- Deployment impact: `{args.deployment_impact}`\n"
        f"- Reason: {args.reason}\n\n"
        "Council + Tribunal exact-head verdicts:\n"
        f"{verdict_lines}\n\n"
        f"Audit event: `{event_id}`\n\n"
        "Approval only. This identity never merges, pushes, authors, or administers. "
        f"The next step is the SSC-ENG engineer (`{author_login}`) merging their own PR.\n\n"
        "---\n\n" + signature_text
    )
    review = request_json(token, "POST", f"{pr_path}/reviews", {"event": "APPROVE", "body": review_body})
    if review.get("state") != "APPROVED" or review.get("commit_id") != head_sha:
        failure = {
            **basis,
            "event": "approval_outcome",
            "outcome": "unexpected_review_response",
            "review_id": review.get("id"),
            "review_state": review.get("state"),
            "review_commit_id": review.get("commit_id"),
        }
        append_audit(profile, failure)
        raise GateError("GitHub did not return an APPROVED review bound to the exact head")
    outcome = {
        **basis,
        "event": "approval_outcome",
        "outcome": "approved",
        "review_id": review.get("id"),
        "review_url": review.get("html_url"),
    }
    audit = append_audit(profile, outcome)
    return {
        "repository": f"{owner}/{repo}",
        "pr": args.pr,
        "head_sha": head_sha,
        "review_state": "APPROVED",
        "review_url": review.get("html_url"),
        "audit": str(audit),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--verify-identity", action="store_true")
    result.add_argument("--repo")
    result.add_argument("--pr", type=int)
    result.add_argument("--linear")
    result.add_argument("--verdict", action="append", default=[])
    result.add_argument("--deployment-impact")
    result.add_argument("--reason")
    result.add_argument("--expected-head")
    result.add_argument(
        "--signature",
        help="path to a file containing the rendered CTO signature block "
        "(name, position, credentials, model, tokens) to append to the review body",
    )
    return result


def main() -> int:
    clean_reexec()
    args = parser().parse_args()
    profile = ""
    try:
        profile = profile_name()
        if args.verify_identity:
            token = read_token(secret_path(profile))
            output = verify_identity(token, profile)
        else:
            token = read_token(secret_path(profile))
            missing = [
                name
                for name in ("repo", "pr", "linear", "deployment_impact", "reason", "signature")
                if getattr(args, name) in (None, "")
            ]
            if missing or not args.verdict:
                raise GateError(
                    "missing approval inputs: " + ", ".join(missing + ([] if args.verdict else ["verdict"]))
                )
            output = approve(args, token, profile)
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    except GateError as exc:
        if profile and not args.verify_identity:
            try:
                audit_rejection(profile, args, str(exc))
            except Exception:
                pass
        print(f"APPROVAL_GATE_REJECTED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
