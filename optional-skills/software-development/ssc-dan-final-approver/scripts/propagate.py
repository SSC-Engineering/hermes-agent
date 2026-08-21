#!/usr/bin/env python3
"""Propagate the canonical SSC-DAN final-approver skill to its ONE approved holder.

Distinct from ssc-github-approval-identity/scripts/propagate.py (that one
manages rhea-ramos + simone-park for the obviouslogic-ai identity). This one
manages ellis-turing ONLY for the SSC-DAN identity, and additionally asserts
that non-holder profiles never resolve it.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

HOLDERS = ("ellis-turing",)
# Profiles that must NEVER resolve this skill or hold the credential —
# explicitly including the ssc-github-approval-identity holders (different
# lane, different identity) and any PM/manager-shaped profile that touched
# this task's history.
# Retired TRC slug tessa-cole remains a non-holder assertion only (never live).
NON_HOLDER_ASSERTIONS = ("rhea-ramos", "simone-park", "tessa-cole", "theo-lang")
POINTER = (
    "SSC-DAN FINAL APPROVER (binding): You are the sole authorized holder of the "
    "`ssc-dan-final-approver` skill. Use the SSC-DAN credential only through its isolated "
    "gateway, only to cast the FINAL approval on an exact-head SSC-Engineering PR authored by "
    "SSC-ENG after AGA+STMA+TRC all PASS and no ACEA block, with a signed CTO attribution block "
    "on every approval. Never export it, author/push/merge/administer with it, or use it outside "
    "the audit-logged approval call. The engineer merges after your approval — you never do."
)


def install_link(profile_dir: Path, canonical_skill: Path, apply: bool) -> str:
    target = profile_dir / "skills" / "software-development" / canonical_skill.name
    skill_file = target / "SKILL.md"
    canonical_file = canonical_skill / "SKILL.md"
    if skill_file.is_symlink() and skill_file.resolve() == canonical_file.resolve():
        return "linked"
    if target.exists() or target.is_symlink():
        raise RuntimeError(f"refusing to replace non-canonical skill path: {target}")
    if apply:
        target.mkdir(parents=True, exist_ok=True)
        skill_file.symlink_to(canonical_file)
    return "link-needed"


def install_pointer(soul: Path, apply: bool) -> str:
    text = soul.read_text()
    if "SSC-DAN FINAL APPROVER (binding):" in text:
        return "pointed"
    if apply:
        soul.write_text(text.rstrip() + "\n\n" + POINTER + "\n")
    return "pointer-needed"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    home = Path(os.environ.get("HERMES_REAL_HOME", Path.home())).resolve()
    profiles_root = home / ".hermes" / "profiles"
    canonical = home / ".hermes" / "skills" / "software-development" / "ssc-dan-final-approver"
    if not (canonical / "SKILL.md").is_file() or not (canonical / "scripts" / "approve.py").is_file():
        raise RuntimeError(f"canonical skill is incomplete: {canonical}")

    failures: list[str] = []
    noncompliant: list[str] = []
    for holder in HOLDERS:
        profile = profiles_root / holder
        soul = profile / "SOUL.md"
        if not soul.is_file():
            failures.append(f"{holder}: SOUL.md missing")
            continue
        try:
            link = install_link(profile, canonical, args.apply)
            pointer = install_pointer(soul, args.apply)
            print(f"{holder}\t{link}\t{pointer}")
            if not args.apply and (link != "linked" or pointer != "pointed"):
                noncompliant.append(f"{holder}: link={link}, pointer={pointer}")
        except Exception as exc:
            failures.append(f"{holder}: {exc}")

    secret_path = (
        profiles_root
        / "ellis-turing"
        / "home"
        / ".config"
        / "helios"
        / "ssc-dan-approval.env"
    )
    if not secret_path.is_file():
        failures.append(f"ellis-turing: secret file missing at {secret_path}")
    else:
        mode = secret_path.stat().st_mode & 0o777
        parent_mode = secret_path.parent.stat().st_mode & 0o777
        if mode != 0o600:
            failures.append(f"ellis-turing: secret file mode is {oct(mode)}, want 0600")
        if parent_mode != 0o700:
            failures.append(f"ellis-turing: secret dir mode is {oct(parent_mode)}, want 0700")

    for profile in NON_HOLDER_ASSERTIONS:
        root = profiles_root / profile
        link = root / "skills" / "software-development" / canonical.name
        soul = root / "SOUL.md"
        other_secret = root / "home" / ".config" / "helios" / "ssc-dan-approval.env"
        if link.exists() or link.is_symlink():
            failures.append(f"{profile}: non-holder must not resolve ssc-dan-final-approver")
        if soul.is_file() and "SSC-DAN FINAL APPROVER (binding):" in soul.read_text():
            failures.append(f"{profile}: non-holder must not have the SSC-DAN pointer")
        if other_secret.exists():
            failures.append(f"{profile}: non-holder must not hold ssc-dan-approval.env")

    print(
        f"SUMMARY holders={len(HOLDERS)} failures={len(failures)} "
        f"noncompliant={len(noncompliant)} mode={'apply' if args.apply else 'check'}"
    )
    for item in failures:
        print(f"FAIL {item}")
    for item in noncompliant:
        print(f"NONCOMPLIANT {item}")
    return 1 if failures or noncompliant else 0


if __name__ == "__main__":
    raise SystemExit(main())
