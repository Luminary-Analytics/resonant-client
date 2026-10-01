"""Write lumi/_build_config.py: what this build of Lumi is made with (RELEASING.md).

    python packaging/build_config.py            # from LUMI_BUILD_FEEDBACK_URL
    python packaging/build_config.py --clean    # remove it again

Today that is the feedback address (``lumi.feedback.BUILD_DESTINATION``):
where Send feedback sends reports when neither Settings nor an organization's
policy names one (``privacy.feedback_url``, which wins). The builds
(scripts/build_clean.ps1, packaging/build_macos.sh, packaging/build_linux.sh)
run this before they install Lumi and run PyInstaller, and remove the file when
they finish; release.yml passes the repository variable LUMI_FEEDBACK_URL as
LUMI_BUILD_FEEDBACK_URL, so setting the address needs no code change. Without
it the build has no address of its own, as a source checkout has none.

The address must be https with a host, in ASCII, and without a user name,
password, query or fragment: anything else stops the build rather than ship an
address reports can't go to, or one that would carry credentials. Standard
library only, since it runs before the build environment exists.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "lumi" / "_build_config.py"
ENVIRONMENT = "LUMI_BUILD_FEEDBACK_URL"
MAX_LENGTH = 2048


def feedback_url(value: str) -> str:
    """The feedback address as the build keeps it ("" for none); ValueError says what's wrong."""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > MAX_LENGTH or not text.isascii() or any(ch.isspace() or not ch.isprintable() or ch in "\"'\\"
                                                           for ch in text):
        raise ValueError("isn't a plain address (ASCII, no spaces, quotes or backslashes; write an "
                         "internationalized host in its xn-- form)")
    parts = urlsplit(text)
    if parts.scheme.lower() != "https":
        raise ValueError("must use https")
    if "@" in parts.netloc:
        raise ValueError("must not hold a user name or password")
    if not parts.hostname:
        raise ValueError("needs a host, as in https://cloud.example.com")
    if parts.query or parts.fragment:
        raise ValueError("must not have a ? query or a # fragment")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("has a port that isn't a number") from exc
    return text.rstrip("/")


def render(url: str) -> str:
    """The module's text. ``url`` passed ``feedback_url``, so its repr is a plain string literal."""
    return ('"""What this build of Lumi was made with (packaging/build_config.py). Written by the build; never '
            'committed."""\n\n'
            "# Where Send feedback sends reports when Settings and the organization name none (lumi/feedback.py).\n"
            f"FEEDBACK_URL = {url!r}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--clean", action="store_true", help="remove the file instead of writing it")
    parser.add_argument("--out", type=Path, default=TARGET, help=argparse.SUPPRESS)  # tests write elsewhere
    args = parser.parse_args(argv)
    if args.clean:
        args.out.unlink(missing_ok=True)
        return 0
    try:
        url = feedback_url(os.environ.get(ENVIRONMENT, ""))
    except ValueError as exc:
        print(f"{ENVIRONMENT} {exc}; the build stops rather than ship it.", file=sys.stderr)
        return 2
    args.out.write_text(render(url), encoding="utf-8", newline="\n")
    print(f"This build's feedback address: {url or 'none (only Settings, a policy or a Lumi Cloud in use name one)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
