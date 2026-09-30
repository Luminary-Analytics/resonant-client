"""Rehearse release.yml's gh-pages publishing for two releases in a row, on this computer. Nothing leaves it.

build-macos.yml runs this on windows-latest, where the release publishes,
after the same "Keep Git from changing the Pages site's bytes" step and
gh-pages checkout that release.yml's jobs have. A bare copy of that checkout
on the runner stands in for GitHub, and a throwaway key for
EDDSA_PRIVATE_KEY. For 99.0.0 and then 99.1.0-beta.1, as a release does:

1. the Windows job: a fresh checkout; publish_pages.py and update_appcast.py
   for a stand-in installer signed with the key; push_pages.py;
2. the publish-macos job: a fresh checkout; publish_macos.ps1 for a stand-in
   disk image; push_pages.py;

and after each push, ``push_pages.py --check --rev`` on the commit pushed:
the bytes Pages would serve, macOS feeds and disk images verified. The macOS
job must leave the Windows feeds as they were. Then:

* the checkout the release job made holds its files byte for byte, so the
  release's Git setting took (on the live branch, which has no
  .gitattributes before the first release with it);
* a feed changed after signing is refused, and nothing is pushed;
* a branch a bad publish already broke (a feed signed over "\\r\\n" and
  committed as "\\n", as Git for Windows once did) is repaired as
  docs/release-pipeline.md says: publish_macos.ps1 -CheckFeeds names the
  feed, -ResignFeeds signs it again, and push_pages.py publishes it;
* a checkout with Git's own defaults, core.autocrlf=true as on Windows, gets
  every file byte for byte, from the committed .gitattributes;
* what a Mac reads from the site: the feeds' items, the disk image's address
  and length, the download page.

    python scripts/rehearse_pages_publish.py --pages GH_PAGES_CHECKOUT [--work DIR] [--powershell pwsh]

Locally, add --isolate-git-config: it gives the rehearsal the release's Git
setting (core.autocrlf=false) without touching your own configuration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
TOOL = PACKAGING / "winsparkle" / "WinSparkle-0.9.2" / "bin" / "winsparkle-tool.exe"
PAGES_URL = "https://luminary-analytics.github.io/resonant-client"
SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
VERSIONS = ("99.0.0", "99.1.0-beta.1")
IMAGE_SIZE = 3_000_000


class RehearsalFailed(AssertionError):
    pass


def run(*command: object, env: dict[str, str], expect_failure: bool = False) -> subprocess.CompletedProcess:
    """Run a command, showing its output; it must succeed (or, with ``expect_failure``, fail)."""
    args = [str(part) for part in command]
    print("$ " + " ".join(args), flush=True)
    result = subprocess.run(args, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    for stream in (result.stdout, result.stderr):
        if stream.strip():
            print(stream.rstrip(), flush=True)
    if expect_failure and result.returncode == 0:
        raise RehearsalFailed(f"{args[0]} should have failed")
    if not expect_failure and result.returncode != 0:
        raise RehearsalFailed(f"{Path(args[0]).name} exited {result.returncode}")
    return result


def git_bytes(repo: Path, *args: str, env: dict[str, str]) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True)
    if result.returncode != 0:
        raise RehearsalFailed(f"git {' '.join(args)}: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def tracked(repo: Path, env: dict[str, str], rev: str = "HEAD") -> dict[str, str]:
    """Each file's blob id at ``rev``."""
    found = {}
    for record in filter(None, git_bytes(repo, "ls-tree", "-r", "-z", rev, env=env).split(b"\0")):
        meta, _, path = record.partition(b"\t")
        _mode, kind, blob = meta.split(b" ")
        if kind == b"blob":
            found[path.decode("utf-8")] = blob.decode("ascii")
    return found


def assert_checkout_exact(repo: Path, env: dict[str, str], what: str) -> None:
    """Every file in the working copy has the committed bytes, line ends and all."""
    files = tracked(repo, env)
    text = 0
    for name, blob in files.items():
        committed = git_bytes(repo, "cat-file", "blob", blob, env=env)
        if (repo / name).read_bytes() != committed:
            raise RehearsalFailed(f"{what}: {name} isn't what Git holds (line ends changed on checkout?)")
        text += b"\n" in committed and b"\0" not in committed
    print(f"OK {what}: all {len(files)} files byte for byte, {text} of them text", flush=True)


def show_autocrlf(env: dict[str, str], what: str) -> None:
    result = subprocess.run(["git", "config", "--show-origin", "--get-all", "core.autocrlf"], env=env,
                            capture_output=True, text=True)
    print(f"core.autocrlf {what}: {result.stdout.strip() or 'unset'}", flush=True)


def windows_feeds(origin: Path, env: dict[str, str]) -> dict[str, str]:
    return {name: blob for name, blob in tracked(origin, env, "gh-pages").items()
            if re.fullmatch(r"appcast[^/]*\.xml", name) and not name.startswith("appcast-macos")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pages", type=Path, required=True, help="A gh-pages checkout made as the release makes it")
    parser.add_argument("--work", type=Path, default=Path(os.environ.get("RUNNER_TEMP", ".")) / "pages-rehearsal")
    parser.add_argument("--powershell", default=shutil.which("pwsh") and "pwsh" or "powershell")
    parser.add_argument("--isolate-git-config", action="store_true",
                        help="Use a global Git configuration of the rehearsal's own: core.autocrlf=false")
    args = parser.parse_args(argv)
    if not TOOL.is_file():
        raise RehearsalFailed(f"{TOOL} is missing; the rehearsal runs on Windows, as the release does")
    work = args.work.resolve()
    shutil.rmtree(work, ignore_errors=True)
    (work / "release").mkdir(parents=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    if args.isolate_git_config:
        config = work / "release.gitconfig"
        config.write_text("[core]\n\tautocrlf = false\n", encoding="utf-8")
        env["GIT_CONFIG_GLOBAL"] = str(config)
    run("git", "--version", env=env)
    show_autocrlf(env, "as the release job configures Git")
    python = sys.executable

    # 0. The release job's own gh-pages checkout: byte for byte, or the setting didn't take.
    pages = args.pages.resolve()
    assert_checkout_exact(pages, env, "the gh-pages checkout, as the release job makes it")

    # GitHub, played by a bare copy of the branch on this computer.
    origin = work / "origin.git"
    run("git", "clone", "--quiet", "--bare", "--no-local", "--branch", "gh-pages", pages, origin, env=env)

    def checkout(name: str, *config: str) -> Path:
        folder = work / name
        options = [item for setting in config for item in ("-c", setting)]
        run("git", *options, "clone", "--quiet", "--branch", "gh-pages", origin, folder, env=env)
        return folder

    key = work / "throwaway.key"
    generated = run(TOOL, "generate-key", "--file", key, env=env).stdout
    public = re.search(r"Public key:\s*(\S+)", generated).group(1)
    push_pages = [python, PACKAGING / "push_pages.py"]
    verify = ["--public-key", public, "--tool", TOOL]

    def sign(path: Path) -> str:
        return run(TOOL, "sign", "--private-key-file", key, path, env=env).stdout.strip()

    try:
        # Since the first macOS release (0.20.0-alpha.1) the live branch has macOS
        # feeds whose signatures, and whose items' disk image signatures, were made
        # with the real key, which the throwaway key can't reproduce or verify. Start
        # from the branch without them, as it was before that release: the rehearsal's
        # own releases then make the feeds again, signed with its key. (Real releases
        # add to the live feeds, which each release's own push_pages.py --check covers.)
        live_feeds = sorted(name for name in tracked(origin, env, "gh-pages")
                            if re.fullmatch(r"appcast-macos[^/]*\.xml", name))
        if live_feeds:
            live = checkout("live-feeds")
            run("git", "-C", live, "rm", "--quiet", *live_feeds, env=env)
            run("git", "-C", live, "-c", "user.name=rehearsal", "-c", "user.email=rehearsal@example.invalid",
                "commit", "--quiet", "-m", "rehearsal: start without the live macOS feeds", env=env)
            run("git", "-C", live, "push", "--quiet", "origin", "HEAD:refs/heads/gh-pages", env=env)
            print(f"OK started from the live branch without its macOS feeds ({', '.join(live_feeds)})", flush=True)

        for version in VERSIONS:
            # The Windows job.
            windows = checkout(f"windows-{version}")
            installer = work / "installer" / f"lumi-setup-{version}.exe"
            installer.parent.mkdir(exist_ok=True)
            installer.write_bytes(b"MZ" + os.urandom(4096))
            # As sign_windows.ps1 records it in the release job (with no signer configured).
            record = work / "lumi-authenticode.jsonl"
            record.write_text(json.dumps({"path": str(installer), "signed": False,
                                          "sha256": hashlib.sha256(installer.read_bytes()).hexdigest()}) + "\n",
                              encoding="utf-8")
            run(python, PACKAGING / "publish_pages.py", "--site", windows, "--installer", installer,
                "--version", version, env=env)
            run(python, PACKAGING / "update_appcast.py", "--version", version, "--installer", installer,
                "--signature", sign(installer), "--notes", f"<p>Lumi {version} for Windows</p>",
                "--site", windows, "--download-base", f"{PAGES_URL}/downloads", env=env)
            run(*push_pages, windows, "--message", f"rehearsal: Lumi {version} installer and appcast", *verify,
                "--signed", record, env=env)
            run(*push_pages, origin, "--check", "--rev", "gh-pages", *verify, env=env)
            before = windows_feeds(origin, env)

            # The publish-macos job, in a checkout of what the Windows job pushed.
            mac = checkout(f"macos-{version}")
            (work / "release" / f"lumi-{version}.dmg").write_bytes(os.urandom(IMAGE_SIZE))
            (work / "release" / f"lumi-{version}.pkg").write_bytes(os.urandom(99))
            run(args.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", PACKAGING / "publish_macos.ps1",
                "-Site", mac, "-Release", work / "release", "-Version", version, "-PagesUrl", PAGES_URL,
                "-PrivateKeyFile", key, "-PublicKey", public, "-Python", python, env=env)
            run(*push_pages, mac, "--message", f"rehearsal: Lumi {version} for macOS", *verify, env=env)
            run(*push_pages, origin, "--check", "--rev", "gh-pages", *verify, env=env)
            if windows_feeds(origin, env) != before:
                raise RehearsalFailed(f"the macOS job for {version} changed the Windows feeds")
            print(f"OK {version}: both jobs pushed; the pushed macOS feeds and disk images verify, "
                  f"and the {len(before)} Windows feeds are as the Windows job left them", flush=True)

        # Feeds whose bytes changed after signing are refused, and nothing
        # reaches the branch: line ends, as Git for Windows would change them,
        # and one character of the disk image's address.
        pushed = git_bytes(origin, "rev-parse", "gh-pages", env=env)
        changes = {
            "line-ends": ("its line ends", lambda data: data.replace(b"\n", b"\r\n")),
            "address": ("its disk image's address", lambda data: data.replace(b"lumi-99.0.0", b"lumi-99.0.9", 1)),
        }
        for name, (what, change) in changes.items():
            changed = checkout(f"changed-{name}")
            feed = changed / "appcast-macos.xml"
            feed.write_bytes(change(feed.read_bytes()))
            refused = run(*push_pages, changed, "--message", "rehearsal: must not be pushed", *verify, env=env,
                          expect_failure=True)
            if "appcast-macos.xml" not in refused.stderr or \
                    git_bytes(origin, "rev-parse", "gh-pages", env=env) != pushed:
                raise RehearsalFailed(f"a feed with {what} changed after signing wasn't refused")
            print(f"OK a feed with {what} changed after signing is refused; nothing was pushed", flush=True)

        # Repairing a branch a bad publish already broke, as docs/release-pipeline.md says:
        # the broken feed goes out the old way (signed over "\r\n", committed as "\n").
        broken = checkout("broken")
        feed = broken / "appcast-macos.xml"
        content = feed.read_bytes()[:feed.read_bytes().rfind(b"<!-- sparkle-signatures:")]
        written = content.replace(b"\n", b"\r\n")
        (work / "written.xml").write_bytes(written)
        signature = sign(work / "written.xml")
        feed.write_bytes(content + (f"<!-- sparkle-signatures:\nedSignature: {signature}\nlength: {len(written)}\n"
                                    "-->\n").encode("ascii"))
        run("git", "-C", broken, "add", "-A", env=env)
        tree = git_bytes(broken, "write-tree", env=env).decode("ascii").strip()
        commit = git_bytes(broken, "-c", "user.name=rehearsal", "-c", "user.email=rehearsal@example.invalid",
                           "commit-tree", tree, "-m", "rehearsal: a broken publish", env=env).decode("ascii").strip()
        run("git", "-C", broken, "push", "--quiet", "--force", "origin", f"{commit}:refs/heads/gh-pages", env=env)
        run(*push_pages, origin, "--check", "--rev", "gh-pages", *verify, env=env, expect_failure=True)
        repair = checkout("repair")
        publish_macos = [args.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                         PACKAGING / "publish_macos.ps1", "-Site", repair, "-PublicKey", public, "-Python", python]
        checked = run(*publish_macos, "-CheckFeeds", env=env, expect_failure=True)
        if "appcast-macos.xml: invalid" not in checked.stdout:
            raise RehearsalFailed("-CheckFeeds didn't name the broken feed")
        run(*publish_macos, "-ResignFeeds", "-PrivateKeyFile", key, env=env)
        run(*publish_macos, "-CheckFeeds", env=env)
        run(*push_pages, repair, "--message", "rehearsal: sign the macOS feeds again", *verify, env=env)
        run(*push_pages, origin, "--check", "--rev", "gh-pages", *verify, env=env)
        print("OK a broken feed on the branch: -CheckFeeds names it, -ResignFeeds signs it again, "
              "push_pages.py publishes it, and the pushed commit verifies", flush=True)

        # Git's own defaults: no global setting, core.autocrlf=true (the Windows default).
        empty = work / "empty.gitconfig"
        empty.write_text("", encoding="utf-8")
        defaults = dict(env, GIT_CONFIG_GLOBAL=str(empty))
        show_autocrlf(defaults, "with Git's own defaults")
        plain = work / "defaults"
        run("git", "-c", "core.autocrlf=true", "clone", "--quiet", "--branch", "gh-pages", origin, plain,
            env=defaults)
        assert_checkout_exact(plain, defaults, "a checkout with core.autocrlf=true, after the release")

        # What a Mac reads.
        what_a_mac_reads(plain)
        return 0
    finally:
        key.unlink(missing_ok=True)


def what_a_mac_reads(site: Path) -> None:
    def items(name: str) -> list[ET.Element]:
        return ET.parse(site / name).getroot().find("channel").findall("item")

    stable, beta = items("appcast-macos.xml"), items("appcast-macos-beta.xml")
    enclosure = stable[0].find("enclosure")
    url = enclosure.get("url")
    checks = {
        "the stable feed offers 99.0.0's disk image": url == f"{PAGES_URL}/downloads/v99.0.0/lumi-99.0.0.dmg",
        "its enclosure says macOS and the image's length": (enclosure.get(f"{SPARKLE}os"), enclosure.get("length"))
        == ("macos", str(IMAGE_SIZE)),
        "the beta feed's newest is the beta, by the name people see":
            beta[0].findtext(f"{SPARKLE}shortVersionString") == "99.1.0-beta.1",
        "and without the dash for Sparkle": beta[0].findtext(f"{SPARKLE}version") == "99.1.0beta.1",
        "no Windows feed names an OS": all(b"sparkle:os" not in path.read_bytes() for path in site.glob("appcast*.xml")
                                           if not path.name.startswith("appcast-macos")),
        "the disk image isn't notarized, and says so":
            json.loads((site / "downloads/v99.0.0/macos.json").read_text(encoding="utf-8")) == {"notarized": False},
    }
    page = (site / "index.html").read_text(encoding="utf-8")
    checks["the page offers the Mac download, Open Anyway and the PKG"] = all(
        text in page for text in ("Download Lumi 99.0.0 for macOS", "Open Anyway", "lumi-99.0.0.pkg"))
    failed = [check for check, ok in checks.items() if not ok]
    if failed:
        raise RehearsalFailed("what a Mac reads: " + "; ".join(failed))
    print(f"OK what a Mac reads: {len(checks)} checks", flush=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RehearsalFailed as exc:
        print(f"REHEARSAL FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
