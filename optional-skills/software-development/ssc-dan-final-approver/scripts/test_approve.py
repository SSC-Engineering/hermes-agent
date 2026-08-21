#!/usr/bin/env python3
"""Offline unit tests for ssc-dan-final-approver/scripts/approve.py.

No network calls, no credential reads. Exercises the pure gate-logic
functions directly so CI/verification can run without GitHub access.

Policy baseline: HAA ruling OQ-FND-08 (2026-08-04) — PR-author council
markers are VALID; self-review is APPROVER-identity only; council
independence is structural via distinct in-body per-seat signatures.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import approve as gw  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"{status} {label}")
    if not condition:
        FAILURES.append(label)


FAILURES: list[str] = []

# Fixture identities. RELAY retained only as a non-author/non-approver login
# shape for negative cases; OQ-FND-08 removed the login allow-list gate.
PR_AUTHOR = "SSC-ENG"
APPROVER = "SSC-DAN"
RELAY = "helios-verdict-relay"
HEAD = "abc123def4567890abc123def4567890abc123de"


def _ok_body(authority: str = "AGA", head: str = HEAD) -> str:
    return f"Review complete.\nGATEWAY-VERDICT: {authority}=PASS head={head[:8]}\nMore notes."


def _seat_body(authority: str, head: str = HEAD) -> str:
    """Realistic council verdict body with GATEWAY-VERDICT + seat signature."""
    seat = gw.COUNCIL_SEATS[authority]
    return (
        f"GATEWAY-VERDICT: {authority}=PASS head={head[:8]}\n\n"
        f"## {authority} review\n"
        f"**Reviewer:** {seat['name']} — Cortex Limited LLC.\n\n"
        f"Verdict: PASS at exact head.\n\n"
        f"---\n"
        f"— {seat['name']} · credentials: {seat['credential']} · agent: {seat['agent']}\n"
        f"\n"
        f"_profile: {seat['agent']} · cost estimated_\n"
        f"\n"
        f"_CPTC actual: compare these real tokens with the predicted "
        f"Complexity Points on the technical-scope sub-issue._\n"
    )


def _call_evidence(
    body: str,
    *,
    authority: str = "AGA",
    head: str = HEAD,
    marker_author: str | None = PR_AUTHOR,
    pr_author: str = PR_AUTHOR,
    approval_login: str = APPROVER,
    authorized_posters: frozenset[str] | None = None,
):
    verdict = {"authority": authority, "status": "PASS", "url": "https://example/x"}
    return gw.check_verdict_evidence(
        body,
        verdict,
        head,
        marker_author_login=marker_author,
        pr_author_login=pr_author,
        approval_login=approval_login,
        authorized_posters=authorized_posters,
    )


def test_safe_repo() -> None:
    owner, repo = gw.safe_repo("SSC-Engineering/nexus")
    check("safe_repo accepts SSC-Engineering", (owner, repo) == ("SSC-Engineering", "nexus"))
    owner, repo = gw.safe_repo("SSC-ENG/office-imac-dashboard")
    check("safe_repo accepts SSC-ENG", (owner, repo) == ("SSC-ENG", "office-imac-dashboard"))
    try:
        gw.safe_repo("some-other-org/nexus")
        check("safe_repo rejects foreign owner", False)
    except gw.GateError:
        check("safe_repo rejects foreign owner", True)


def test_parse_verdict() -> None:
    v = gw.parse_verdict("AGA:PASS=https://github.com/SSC-Engineering/nexus/pull/1#issuecomment-5")
    check("parse_verdict parses favorable", v["authority"] == "AGA" and v["status"] == "PASS")
    try:
        gw.parse_verdict("AGA:HOLD=https://github.com/SSC-Engineering/nexus/pull/1#issuecomment-5")
        check("parse_verdict rejects unfavorable status", False)
    except gw.GateError:
        check("parse_verdict rejects unfavorable status", True)


def test_check_verdict_evidence() -> None:
    # OQ-FND-08: PR-author markers are the production path and must pass.
    mode, author = _call_evidence(_ok_body(), marker_author=PR_AUTHOR)
    check(
        "check_verdict_evidence accepts matching marker from PR author (SSC-ENG)",
        mode == "marker" and author == PR_AUTHOR,
    )

    try:
        _call_evidence("GATEWAY-VERDICT: AGA=PASS head=ffffffff")
        check("check_verdict_evidence rejects mismatched head", False)
    except gw.GateError:
        check("check_verdict_evidence rejects mismatched head", True)

    try:
        _call_evidence(f"GATEWAY-VERDICT: AGA=HOLD head={HEAD[:8]}")
        check("check_verdict_evidence rejects unfavorable marker", False)
    except gw.GateError:
        check("check_verdict_evidence rejects unfavorable marker", True)

    try:
        _call_evidence("Looks fine to me, ship it.")
        check("check_verdict_evidence rejects prose-only (no grace window)", False)
    except gw.GateError:
        check("check_verdict_evidence rejects prose-only (no grace window)", True)

    try:
        _call_evidence(f"GATEWAY-VERDICT: STMA=PASS head={HEAD[:8]}")
        check("check_verdict_evidence rejects marker for a different authority", False)
    except gw.GateError:
        check("check_verdict_evidence rejects marker for a different authority", True)


def test_marker_author_enforcement() -> None:
    """OQ-FND-08 acceptance:
    (a) PR-author/SSC-ENG marker ACCEPTED
    (b) approver marker REJECTED
    (c) non-approver off-list login ACCEPTED (no login allow-list)
    (d) empty AUTHORIZED_VERDICT_POSTERS constant retained empty (unused)
    (e) missing login fatal
    (f) case-insensitive approver compare
    """

    # (a) PR-author-posted marker ACCEPTED (OQ-FND-08 clause 2)
    try:
        mode, author = _call_evidence(_ok_body(), marker_author=PR_AUTHOR)
        check(
            "marker author=PR author (SSC-ENG) is accepted under OQ-FND-08",
            mode == "marker" and author == PR_AUTHOR,
        )
    except gw.GateError as exc:
        check(
            "marker author=PR author (SSC-ENG) is accepted under OQ-FND-08",
            False,
        )
        print(f"  unexpected: {exc}")

    # (b) approver-posted marker rejected (OQ-FND-08 clause 3 — APPROVER only)
    try:
        _call_evidence(_ok_body(), marker_author=APPROVER)
        check("marker author=approver is rejected", False)
    except gw.GateError as exc:
        check(
            "marker author=approver is rejected",
            "approver" in str(exc).lower(),
        )

    # (c) non-approver login accepted even when not on the legacy empty set
    mode, author = _call_evidence(
        _ok_body(),
        marker_author="random-outsider",
        authorized_posters=frozenset(),
    )
    check(
        "marker author non-approver off legacy allow-list is accepted",
        mode == "marker" and author == "random-outsider",
    )

    # (d) allow-listed shape still accepted (compat) — not required
    mode, author = _call_evidence(
        _ok_body(),
        marker_author=RELAY,
        authorized_posters=frozenset({RELAY}),
    )
    check(
        "non-approver marker author is accepted regardless of allow-list arg",
        mode == "marker" and author == RELAY,
    )

    # (e) empty AUTHORIZED_VERDICT_POSTERS constant remains empty (no login gate)
    check(
        "AUTHORIZED_VERDICT_POSTERS default is empty frozenset (legacy unused)",
        gw.AUTHORIZED_VERDICT_POSTERS == frozenset()
        and isinstance(gw.AUTHORIZED_VERDICT_POSTERS, frozenset),
    )

    # Missing login is fatal (cannot enforce approver-identity self-review)
    try:
        _call_evidence(_ok_body(), marker_author=None)
        check("missing marker author login is rejected", False)
    except gw.GateError as exc:
        check(
            "missing marker author login is rejected",
            "user.login" in str(exc) or "no resolvable" in str(exc),
        )

    # Case-insensitive approver compare
    try:
        _call_evidence(_ok_body(), marker_author="ssc-dan")
        check("marker author=approver compare is case-insensitive", False)
    except gw.GateError as exc:
        check(
            "marker author=approver compare is case-insensitive",
            "approver" in str(exc).lower(),
        )

    # Direct unit of check_marker_author audit return (PR author path)
    verified = gw.check_marker_author(
        PR_AUTHOR,
        pr_author_login=PR_AUTHOR,
        approval_login=APPROVER,
        authorized_posters=frozenset(),
    )
    check("check_marker_author returns verified PR-author login for audit", verified == PR_AUTHOR)


def test_council_seat_signatures() -> None:
    """OQ-FND-08 clause 2/3 structural independence: three DISTINCT seats."""
    bodies = {auth: _seat_body(auth) for auth in gw.MANDATORY_AUTHORITIES}
    resolved = gw.check_council_seat_signatures(bodies)
    check(
        "three distinct per-seat signatures pass",
        resolved
        == {auth: gw.COUNCIL_SEATS[auth]["agent"] for auth in gw.MANDATORY_AUTHORITIES},
    )

    # Missing one seat HOLD
    incomplete = dict(bodies)
    incomplete["TRC"] = f"GATEWAY-VERDICT: TRC=PASS head={HEAD[:8]}\nunsigned review\n"
    try:
        gw.check_council_seat_signatures(incomplete)
        check("missing TRC seat signature is HOLD", False)
    except gw.GateError as exc:
        check(
            "missing TRC seat signature is HOLD",
            "council independence HOLD" in str(exc) and "TRC" in str(exc),
        )

    # Name present but no agent/credential anchor HOLD
    name_only = dict(bodies)
    name_only["AGA"] = (
        f"GATEWAY-VERDICT: AGA=PASS head={HEAD[:8]}\n"
        "Reviewed by someone claiming Arturo Gallo without seat anchors.\n"
    )
    try:
        gw.check_council_seat_signatures(name_only)
        check("name-only without agent/credential is HOLD", False)
    except gw.GateError as exc:
        check(
            "name-only without agent/credential is HOLD",
            "council independence HOLD" in str(exc) and "AGA" in str(exc),
        )

    # _profile: footer form accepted ONLY when the credential is also in the
    # body (GATEWAY-VERDICT canon: name AND agent slug AND credential).
    profile_form_with_credential = {
        auth: (
            f"GATEWAY-VERDICT: {auth}=PASS head={HEAD[:8]}\n"
            f"**Reviewer:** {gw.COUNCIL_SEATS[auth]['name']}\n"
            f"_profile: {gw.COUNCIL_SEATS[auth]['agent']} · "
            f"credentials: {gw.COUNCIL_SEATS[auth]['credential']} · cost estimated_\n"
        )
        for auth in gw.MANDATORY_AUTHORITIES
    }
    resolved_pf = gw.check_council_seat_signatures(profile_form_with_credential)
    check(
        "_profile footer form is accepted as seat signature when credential is also present",
        resolved_pf["AGA"] == gw.COUNCIL_SEATS["AGA"]["agent"]
        and resolved_pf["TRC"] == gw.COUNCIL_SEATS["TRC"]["agent"],
    )

    # _profile: <slug> WITHOUT the credential is a hard fail (AND-all-three).
    profile_form_no_credential = {
        auth: (
            f"GATEWAY-VERDICT: {auth}=PASS head={HEAD[:8]}\n"
            f"**Reviewer:** {gw.COUNCIL_SEATS[auth]['name']}\n"
            f"_profile: {gw.COUNCIL_SEATS[auth]['agent']} · cost estimated_\n"
        )
        for auth in gw.MANDATORY_AUTHORITIES
    }
    try:
        gw.check_council_seat_signatures(profile_form_no_credential)
        check("_profile footer without credential is HOLD", False)
    except gw.GateError as exc:
        check(
            "_profile footer without credential is HOLD",
            "council independence HOLD" in str(exc)
            and "credential=" in str(exc),
        )

    # name + agent slug, NO credential -> HOLD.
    slug_no_credential = {
        auth: (
            f"GATEWAY-VERDICT: {auth}=PASS head={HEAD[:8]}\n"
            f"Reviewer: {gw.COUNCIL_SEATS[auth]['name']}\n"
            f"agent: {gw.COUNCIL_SEATS[auth]['agent']}\n"
        )
        for auth in gw.MANDATORY_AUTHORITIES
    }
    try:
        gw.check_council_seat_signatures(slug_no_credential)
        check("name + slug without credential is HOLD", False)
    except gw.GateError as exc:
        check(
            "name + slug without credential is HOLD",
            "council independence HOLD" in str(exc)
            and "credential=" in str(exc),
        )

    # name + credential, NO agent slug -> HOLD.
    credential_no_slug = {
        auth: (
            f"GATEWAY-VERDICT: {auth}=PASS head={HEAD[:8]}\n"
            f"Reviewer: {gw.COUNCIL_SEATS[auth]['name']}\n"
            f"credentials: {gw.COUNCIL_SEATS[auth]['credential']}\n"
        )
        for auth in gw.MANDATORY_AUTHORITIES
    }
    try:
        gw.check_council_seat_signatures(credential_no_slug)
        check("name + credential without agent slug is HOLD", False)
    except gw.GateError as exc:
        check(
            "name + credential without agent slug is HOLD",
            "council independence HOLD" in str(exc)
            and "agent=" in str(exc),
        )

    # Full three-token template body (council-gateway-verdict skill canon):
    # GATEWAY-VERDICT header plus Name / agent-slug / credential on separate
    # lines. Must pass for all three seats.
    template_bodies = {
        auth: (
            f"GATEWAY-VERDICT: {auth}=GO head={HEAD[:8]}\n\n"
            f"{gw.COUNCIL_SEATS[auth]['name']}\n"
            f"{gw.COUNCIL_SEATS[auth]['agent']}\n"
            f"{gw.COUNCIL_SEATS[auth]['credential']}\n"
        )
        for auth in gw.MANDATORY_AUTHORITIES
    }
    resolved_template = gw.check_council_seat_signatures(template_bodies)
    check(
        "three-token template body passes for AGA/STMA/TRC",
        resolved_template
        == {auth: gw.COUNCIL_SEATS[auth]["agent"] for auth in gw.MANDATORY_AUTHORITIES},
    )

    # HEL-6066: retired TRC tokens must never satisfy the live TRC seat.
    # Even if a comment body carries all three retired tokens together,
    # CAST must HOLD because none of them match the live TRC identity in
    # COUNCIL_SEATS. Two distinct retired identities are exercised here —
    # the earlier fired seat slug/credential (tessa-cole /
    # eng-technical-review; given name omitted per HEL-6066) and the
    # retired-131 slug (Theo Lang / theo-lang) — both must HOLD under
    # AND-all-three against the live TRC identity. The AGA and STMA bodies
    # are valid; TRC is the one under test.
    for retired_label, retired_body in (
        (
            "fired-tokens (tessa-cole / eng-technical-review)",
            (
                f"GATEWAY-VERDICT: TRC=PASS head={HEAD[:8]}\n\n"
                "Retired Fired TRC\n"
                "tessa-cole\n"
                "eng-technical-review\n"
            ),
        ),
        (
            "retired-131 (Theo Lang / theo-lang)",
            (
                f"GATEWAY-VERDICT: TRC=PASS head={HEAD[:8]}\n\n"
                "Theo Lang\n"
                "theo-lang\n"
                "helios-agent-trc\n"
            ),
        ),
    ):
        retired_trc = {
            "AGA": _seat_body("AGA"),
            "STMA": _seat_body("STMA"),
            "TRC": retired_body,
        }
        try:
            gw.check_council_seat_signatures(retired_trc)
            check(
                f"retired TRC tokens do not satisfy the live TRC seat: {retired_label}",
                False,
            )
        except gw.GateError as exc:
            check(
                f"retired TRC tokens do not satisfy the live TRC seat: {retired_label}",
                "council independence HOLD" in str(exc) and "TRC" in str(exc),
            )

    # Live TRC identity is not any known retired token (fired-tokens or
    # retired-131). The credential helios-agent-trc travels with the live
    # seat and is intentionally not part of the retired-tokens list; the
    # retired-131 body above still HOLDs because its name/slug do not match
    # the live seat under AND-all-three.
    retired_names = {"Retired Fired TRC", "Theo Lang"}
    retired_agents = {"tessa-cole", "theo-lang"}
    retired_credentials = {"eng-technical-review"}
    check(
        "COUNCIL_SEATS['TRC'] is not any known retired identity",
        gw.COUNCIL_SEATS["TRC"]["name"] not in retired_names
        and gw.COUNCIL_SEATS["TRC"]["agent"] not in retired_agents
        and gw.COUNCIL_SEATS["TRC"]["credential"] not in retired_credentials,
    )

    # COUNCIL_SEATS constant is the three expected seats
    check(
        "COUNCIL_SEATS covers AGA/STMA/TRC only",
        set(gw.COUNCIL_SEATS) == {"AGA", "STMA", "TRC"},
    )


def test_profile_gating() -> None:
    import os

    old = os.environ.get("HERMES_PROFILE")
    try:
        os.environ["HERMES_PROFILE"] = "rhea-ramos"
        try:
            gw.profile_name()
            check("profile_name rejects rhea-ramos", False)
        except gw.GateError:
            check("profile_name rejects rhea-ramos", True)

        os.environ["HERMES_PROFILE"] = "ellis-turing"
        check("profile_name accepts ellis-turing", gw.profile_name() == "ellis-turing")

        os.environ["HERMES_PROFILE"] = ""
        try:
            gw.profile_name()
            check("profile_name rejects unset profile", False)
        except gw.GateError:
            check("profile_name rejects unset profile", True)
    finally:
        if old is None:
            os.environ.pop("HERMES_PROFILE", None)
        else:
            os.environ["HERMES_PROFILE"] = old


def test_check_no_acea_block_marker_scan() -> None:
    head = "abcdef1234567890abcdef1234567890abcdef12"
    matches = list(gw.VERDICT_MARKER_RE.finditer(f"GATEWAY-VERDICT: ACEA=BLOCK head={head[:8]}"))
    check("ACEA marker regex matches unfavorable status", len(matches) == 1)
    if matches:
        _a, status, _h = gw.verdict_marker_parts(matches[0])
        check("ACEA marker regex captures BLOCK status", status.upper() == "BLOCK")


def test_verdict_marker_dual_forms() -> None:
    """Both marker shapes must parse (canonical head= and live @ form from #1846)."""
    head_full = HEAD
    head_abbr = HEAD[:8]

    eq_full = f"GATEWAY-VERDICT: AGA=PASS head={head_full}"
    eq_abbr = f"GATEWAY-VERDICT: AGA=PASS head={head_abbr}"
    at_full = f"GATEWAY-VERDICT: AGA PASS @ {head_full}"
    at_abbr = f"GATEWAY-VERDICT: AGA PASS @ {head_abbr}"
    at_lower = f"GATEWAY-VERDICT: stma pass @ {head_abbr}"

    for label, sample, exp_auth, exp_status, exp_head in (
        ("eq full SHA", eq_full, "AGA", "PASS", head_full),
        ("eq abbreviated SHA", eq_abbr, "AGA", "PASS", head_abbr),
        ("at full SHA (#1846 shape)", at_full, "AGA", "PASS", head_full),
        ("at abbreviated SHA", at_abbr, "AGA", "PASS", head_abbr),
        ("at lowercase verdict", at_lower, "stma", "pass", head_abbr),
    ):
        matches = list(gw.VERDICT_MARKER_RE.finditer(sample))
        if len(matches) != 1:
            check(f"VERDICT_MARKER_RE matches {label}", False)
            continue
        auth, status, marker_head = gw.verdict_marker_parts(matches[0])
        check(
            f"VERDICT_MARKER_RE parses {label}",
            auth == exp_auth and status == exp_status and marker_head == exp_head,
        )

    # Evidence path accepts the @ form at exact head from PR author.
    mode, author = _call_evidence(
        f"Review notes.\nGATEWAY-VERDICT: AGA PASS @ {head_abbr}\n",
        marker_author=PR_AUTHOR,
    )
    check(
        "check_verdict_evidence accepts @ form marker from PR author",
        mode == "marker" and author == PR_AUTHOR,
    )

    # Unfavorable @ form still fatal.
    try:
        _call_evidence(f"GATEWAY-VERDICT: AGA HOLD @ {head_abbr}")
        check("check_verdict_evidence rejects unfavorable @ form marker", False)
    except gw.GateError:
        check("check_verdict_evidence rejects unfavorable @ form marker", True)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def test_check_required_statuses_self_satisfying_exception() -> None:
    """DEV-001 circular-dep fix (v1.1): the named context excuses ONLY on a
    completed `failure` conclusion; every other required context and every
    other conclusion for the same context stays fully fatal."""
    required = ["ci-build", "AI-authored PR human review"]

    def make_fake_urlopen(check_runs, statuses=None):
        def fake_urlopen(req, timeout=30):  # noqa: ARG001
            if "check-runs" in req.full_url:
                return _FakeResponse({"check_runs": check_runs})
            return _FakeResponse({"statuses": statuses or []})

        return fake_urlopen

    old_urlopen = gw.urllib.request.urlopen

    # Case 1: DEV-001 is a completed `failure` -> excused; other context green -> approves.
    gw.urllib.request.urlopen = make_fake_urlopen(
        [
            {"name": "ci-build", "status": "completed", "conclusion": "success"},
            {"name": "AI-authored PR human review", "status": "completed", "conclusion": "failure"},
        ]
    )
    try:
        checks, degraded, excused = gw.check_required_statuses("tok", "o", "r", "sha", required)
        check(
            "self-satisfying context excused only on completed failure",
            excused == ["AI-authored PR human review"] and degraded == [],
        )
    finally:
        gw.urllib.request.urlopen = old_urlopen

    # Case 2: DEV-001 missing entirely (never ran) -> NOT excused, stays fatal.
    gw.urllib.request.urlopen = make_fake_urlopen(
        [{"name": "ci-build", "status": "completed", "conclusion": "success"}]
    )
    try:
        try:
            gw.check_required_statuses("tok", "o", "r", "sha", required)
            check("self-satisfying context with no run at all is NOT excused", False)
        except gw.GateError as exc:
            check(
                "self-satisfying context with no run at all is NOT excused",
                "AI-authored PR human review" in str(exc),
            )
    finally:
        gw.urllib.request.urlopen = old_urlopen

    # Case 3: DEV-001 completed with `cancelled` (not `failure`) -> NOT excused.
    gw.urllib.request.urlopen = make_fake_urlopen(
        [
            {"name": "ci-build", "status": "completed", "conclusion": "success"},
            {"name": "AI-authored PR human review", "status": "completed", "conclusion": "cancelled"},
        ]
    )
    try:
        try:
            gw.check_required_statuses("tok", "o", "r", "sha", required)
            check("self-satisfying context is NOT excused on a non-failure conclusion", False)
        except gw.GateError:
            check("self-satisfying context is NOT excused on a non-failure conclusion", True)
    finally:
        gw.urllib.request.urlopen = old_urlopen

    # Case 4: a DIFFERENT required context is red -> stays fully fatal, no excuse applies.
    gw.urllib.request.urlopen = make_fake_urlopen(
        [
            {"name": "ci-build", "status": "completed", "conclusion": "failure"},
            {"name": "AI-authored PR human review", "status": "completed", "conclusion": "failure"},
        ]
    )
    try:
        try:
            gw.check_required_statuses("tok", "o", "r", "sha", required)
            check("a real red required check outside the excused set is still fatal", False)
        except gw.GateError as exc:
            check(
                "a real red required check outside the excused set is still fatal",
                "ci-build" in str(exc) and "AI-authored PR human review" not in str(exc),
            )
    finally:
        gw.urllib.request.urlopen = old_urlopen

    # Case 5: exception set cannot be generalized via any external input —
    # confirm it is a plain frozenset constant, not derived from args/env.
    check(
        "SELF_SATISFYING_CONTEXTS is a hardcoded single-entry frozenset",
        gw.SELF_SATISFYING_CONTEXTS == frozenset({"AI-authored PR human review"})
        and isinstance(gw.SELF_SATISFYING_CONTEXTS, frozenset),
    )


def test_gateway_version_bump() -> None:
    check("GATEWAY_VERSION is 1.5 for HEL-6044 protection-API 403", gw.GATEWAY_VERSION == "1.5")
    check("USER_AGENT reflects 1.5", gw.USER_AGENT.endswith("/1.5"))
    check(
        "ALLOWED_REPO_OWNERS is the locked frozenset",
        gw.ALLOWED_REPO_OWNERS == frozenset({"SSC-Engineering", "SSC-ENG"})
        and isinstance(gw.ALLOWED_REPO_OWNERS, frozenset),
    )
    check("ALLOWED_OWNER remains SSC-Engineering for identity membership", gw.ALLOWED_OWNER == "SSC-Engineering")
    check(
        "unreadable source string is locked",
        gw.UNREADABLE_PROTECTION_SOURCE == "none-readable:protection-api-403-user-owner",
    )


PRO_UPGRADE_403 = (
    "GitHub API GET failed with HTTP 403: Upgrade to GitHub Pro or make this "
    "repository public to enable this feature."
)
FOREIGN_403 = "GitHub API GET failed with HTTP 403: Resource not accessible by integration"


def test_protection_api_403_user_owner_unreadable() -> None:
    """HEL-6044: Pro-upgrade 403 on SSC-ENG is no readable protection surface."""
    old = gw.request_json

    def pro_upgrade(_token, _method, _path, payload=None, *, none_on_404=False):  # noqa: ARG001
        raise gw.GateError(PRO_UPGRADE_403)

    gw.request_json = pro_upgrade
    try:
        classic, rules, source = gw.fetch_branch_protection(
            "tok", "SSC-ENG", "office-imac-dashboard", "main"
        )
        check("Pro-upgrade 403 on SSC-ENG returns empty classic (same as 404)", classic == {})
        check("Pro-upgrade 403 on SSC-ENG returns empty rules (same as 404)", rules == [])
        check(
            "Pro-upgrade 403 on SSC-ENG records none-readable source",
            source == "none-readable:protection-api-403-user-owner",
        )
        review, strict, contexts, observed = gw.resolve_protection_gate(classic, rules, source)
        check(
            "unreadable path does not raise approving-review reject",
            review == gw.UNREADABLE_PROTECTION_SOURCE
            and strict == gw.UNREADABLE_PROTECTION_SOURCE
            and contexts == []
            and observed is True,
        )
    except gw.GateError as exc:
        check("Pro-upgrade 403 on SSC-ENG is unreadable/none", False)
        print(f"  unexpected: {exc}")
    finally:
        gw.request_json = old


def test_protection_api_403_org_owner_still_fatal() -> None:
    """Same Pro-upgrade 403 on SSC-Engineering stays GateError."""
    old = gw.request_json

    def pro_upgrade(_token, _method, _path, payload=None, *, none_on_404=False):  # noqa: ARG001
        raise gw.GateError(PRO_UPGRADE_403)

    gw.request_json = pro_upgrade
    try:
        try:
            gw.fetch_branch_protection("tok", "SSC-Engineering", "HELIOS-AGENTIC-OS", "main")
            check("same Pro-upgrade 403 on SSC-Engineering is still fatal", False)
        except gw.GateError as exc:
            check(
                "same Pro-upgrade 403 on SSC-Engineering is still fatal",
                "HTTP 403" in str(exc) and gw.PRO_UPGRADE_403_FRAGMENT in str(exc),
            )
    finally:
        gw.request_json = old


def test_protection_api_foreign_403_on_user_owner_still_fatal() -> None:
    """Arbitrary 403 on SSC-ENG is not treated as unreadable."""
    old = gw.request_json

    def foreign(_token, _method, _path, payload=None, *, none_on_404=False):  # noqa: ARG001
        raise gw.GateError(FOREIGN_403)

    gw.request_json = foreign
    try:
        try:
            gw.fetch_branch_protection("tok", "SSC-ENG", "office-imac-dashboard", "main")
            check("foreign 403 on SSC-ENG is still fatal", False)
        except gw.GateError as exc:
            check(
                "foreign 403 on SSC-ENG is still fatal",
                "HTTP 403" in str(exc) and gw.PRO_UPGRADE_403_FRAGMENT not in str(exc),
            )
    finally:
        gw.request_json = old


def test_protection_api_404_still_soft_handled() -> None:
    """404 still returns None via request_json; fetch treats it as empty, not unreadable."""
    import email.message
    import io
    import urllib.error

    class _Err(urllib.error.HTTPError):
        def __init__(self, code: int, message: str):
            fp = io.BytesIO(json.dumps({"message": message}).encode())
            hdrs = email.message.Message()
            super().__init__("https://api.github.com/x", code, message, hdrs, fp)

    old_urlopen = gw.urllib.request.urlopen

    def fake_404(req, timeout=30):  # noqa: ARG001
        raise _Err(404, "Branch not protected")

    gw.urllib.request.urlopen = fake_404
    try:
        result = gw.request_json("tok", "GET", "/repos/SSC-ENG/x/branches/main/protection", none_on_404=True)
        check("404 is still soft-handled to None", result is None)
    finally:
        gw.urllib.request.urlopen = old_urlopen

    old = gw.request_json

    def none_404(_token, _method, _path, payload=None, *, none_on_404=False):  # noqa: ARG001
        return None

    gw.request_json = none_404
    try:
        classic, rules, source = gw.fetch_branch_protection(
            "tok", "SSC-ENG", "office-imac-dashboard", "main"
        )
        check("404 fetch returns empty classic", classic == {})
        check("404 fetch returns empty rules", rules == [])
        check("404 fetch is not the unreadable-403 source", source is None)
    finally:
        gw.request_json = old


def test_org_empty_protection_still_fail_closed() -> None:
    """Org-owned empty/404 protection still requires an approving review."""
    try:
        gw.check_branch_protection({}, [])
        check("org empty-protection still fail-closed", False)
    except gw.GateError as exc:
        check(
            "org empty-protection still fail-closed",
            "does not require an approving review" in str(exc),
        )
    try:
        gw.resolve_protection_gate({}, [], None)
        check("org empty-protection via resolve_protection_gate still fail-closed", False)
    except gw.GateError as exc:
        check(
            "org empty-protection via resolve_protection_gate still fail-closed",
            "does not require an approving review" in str(exc),
        )
    try:
        gw.check_required_statuses("tok", "SSC-Engineering", "r", "sha", [])
        check("empty required contexts still raise without observed-check path", False)
    except gw.GateError as exc:
        check(
            "empty required contexts still raise without observed-check path",
            "no required status-check contexts" in str(exc),
        )


def test_check_observed_check_runs() -> None:
    """Unreadable-protection CI: observed exact-head check-runs, never []."""
    old_urlopen = gw.urllib.request.urlopen

    def make_fake(check_runs):
        def fake_urlopen(req, timeout=30):  # noqa: ARG001
            return _FakeResponse({"check_runs": check_runs})

        return fake_urlopen

    gw.urllib.request.urlopen = make_fake(
        [
            {"name": "pytest", "status": "completed", "conclusion": "success"},
            {"name": "node-dom", "status": "completed", "conclusion": "skipped"},
        ]
    )
    try:
        names, degraded, excused = gw.check_observed_check_runs("tok", "SSC-ENG", "r", "sha")
        check(
            "observed checks accept success + skipped with at least one success",
            "pytest" in names and degraded == ["node-dom=skipped"] and excused == [],
        )
    finally:
        gw.urllib.request.urlopen = old_urlopen

    gw.urllib.request.urlopen = make_fake(
        [{"name": "pytest", "status": "completed", "conclusion": "failure"}]
    )
    try:
        try:
            gw.check_observed_check_runs("tok", "SSC-ENG", "r", "sha")
            check("observed checks treat failure as fatal", False)
        except gw.GateError as exc:
            check("observed checks treat failure as fatal", "pytest=failure" in str(exc))
    finally:
        gw.urllib.request.urlopen = old_urlopen

    gw.urllib.request.urlopen = make_fake(
        [{"name": "pytest", "status": "in_progress", "conclusion": None}]
    )
    try:
        try:
            gw.check_observed_check_runs("tok", "SSC-ENG", "r", "sha")
            check("observed checks treat pending as fatal", False)
        except gw.GateError as exc:
            check("observed checks treat pending as fatal", "pytest=pending" in str(exc))
    finally:
        gw.urllib.request.urlopen = old_urlopen

    gw.urllib.request.urlopen = make_fake(
        [{"name": "lint", "status": "completed", "conclusion": "cancelled"}]
    )
    try:
        try:
            gw.check_observed_check_runs("tok", "SSC-ENG", "r", "sha")
            check("observed checks treat cancelled as fatal", False)
        except gw.GateError as exc:
            check("observed checks treat cancelled as fatal", "lint=cancelled" in str(exc))
    finally:
        gw.urllib.request.urlopen = old_urlopen

    gw.urllib.request.urlopen = make_fake(
        [{"name": "slow", "status": "completed", "conclusion": "timed_out"}]
    )
    try:
        try:
            gw.check_observed_check_runs("tok", "SSC-ENG", "r", "sha")
            check("observed checks treat timed_out as fatal", False)
        except gw.GateError as exc:
            check("observed checks treat timed_out as fatal", "slow=timed_out" in str(exc))
    finally:
        gw.urllib.request.urlopen = old_urlopen

    gw.urllib.request.urlopen = make_fake([])
    try:
        try:
            gw.check_observed_check_runs("tok", "SSC-ENG", "r", "sha")
            check("observed checks require at least one success", False)
        except gw.GateError as exc:
            check(
                "observed checks require at least one success",
                "at least one success" in str(exc),
            )
    finally:
        gw.urllib.request.urlopen = old_urlopen


def main() -> int:
    test_safe_repo()
    test_parse_verdict()
    test_check_verdict_evidence()
    test_marker_author_enforcement()
    test_council_seat_signatures()
    test_profile_gating()
    test_check_no_acea_block_marker_scan()
    test_verdict_marker_dual_forms()
    test_check_required_statuses_self_satisfying_exception()
    test_gateway_version_bump()
    test_protection_api_403_user_owner_unreadable()
    test_protection_api_403_org_owner_still_fatal()
    test_protection_api_foreign_403_on_user_owner_still_fatal()
    test_protection_api_404_still_soft_handled()
    test_org_empty_protection_still_fail_closed()
    test_check_observed_check_runs()
    print(f"\n{len(FAILURES)} failure(s)" if FAILURES else "\nall tests passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
