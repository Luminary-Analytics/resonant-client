# Lumi on Linux: packages, the desktop app and servers

Status: source only, not released. CI builds and checks every package on every
change (below); none has been published.

## Packages

| Package | For | Updates |
|---|---|---|
| `lumi_X.Y.Z_amd64.deb` | Ubuntu 22.04 and later, Debian 12 and later | Through apt. Lumi never checks for updates itself. |
| `lumi-X.Y.Z-1.x86_64.rpm` | Fedora 36 and later | Through dnf. Lumi never checks for updates itself. |
| `lumi-X.Y.Z-x86_64.AppImage` | Any recent x86_64 distribution, without installing | Download the next one |
| `lumi-X.Y.Z-linux-x86_64.tar.gz` | Installs without root, such as a service account on a server | Unpack the next one |

- **x86_64 only.** arm64 isn't built yet.
- **glibc 2.35 or later.** Every package is built on Ubuntu 22.04 and needs
  its C library or a newer one. Red Hat Enterprise Linux, Rocky and Alma 9
  have glibc 2.34, so they aren't supported yet.
- **Development builds sort first.** A development build's package version,
  such as `0.20.0~dev3`, sorts before the release.

```bash
sudo apt install ./lumi_X.Y.Z_amd64.deb
sudo dnf install ./lumi-X.Y.Z-1.x86_64.rpm
chmod +x lumi-X.Y.Z-x86_64.AppImage && ./lumi-X.Y.Z-x86_64.AppImage
tar -xzf lumi-X.Y.Z-linux-x86_64.tar.gz && ./lumi-X.Y.Z-linux-x86_64/lumi --version
```

The .deb and .rpm install Lumi in `/opt/lumi`, with `lumi` on the path and
**Lumi** in the applications menu. Remove it with `sudo apt remove lumi` or
`sudo dnf remove lumi`. Each person's settings and sessions in `~/.lumi` stay.

## The desktop app opens in your browser

The Linux packages don't include a native window. GTK or Qt would need the
system's libraries and copyleft Python bindings in the bundle.

- **Opening it.** `lumi gui`, the menu entry, and the AppImage opened
  without arguments all start Lumi's local server and open the page in your
  default browser. The page is the same app as the Windows and macOS window.
- **Without a display**, over SSH for example, Lumi doesn't open a browser. It
  prints the one-time link instead. Forward the port and open the link on
  your own computer:

  ```bash
  ssh -L 8765:127.0.0.1:8765 you@server
  lumi gui --browser --port 8765
  ```

- **Not in the Linux packages:**
  - Computer use. PyAutoGUI's X11 support comes from python3-xlib, which is
    GPL-2.0 and not shipped (`packaging/third-party-components.json`).
  - The native folder picker. Type the project's path instead.
- **Keys.** Lumi keeps them in the desktop's Secret Service (GNOME Keyring,
  for example) when there is one. Without one, for example on a server, keys stay in
  `~/.lumi/settings.json` as plain text: protect that account, or give
  `lumi run` its keys through the environment.

## Servers

Nothing on a server needs a display. Install the .deb or .rpm, or unpack the
tarball as the account that runs Lumi.

- **Unattended runs.** `lumi run` works the way it does in CI; see
  [Headless runs](headless.md).

  ```bash
  ANTHROPIC_API_KEY=... lumi run "Fix the failing test" --provider anthropic --model claude-sonnet-5 --mode bypass
  ```

- **Scheduled tasks** (`lumi schedule`) register with cron, in the crontab of
  the user who creates them. Install cron: `cron` on Debian and Ubuntu,
  `cronie` on Fedora. See [Scheduled tasks](scheduled-tasks.md).
- **The chat gateway as a service.** A systemd unit, for example
  `/etc/systemd/system/lumi-gateway.service`:

  ```ini
  [Unit]
  Description=Lumi chat gateway
  After=network-online.target
  Wants=network-online.target

  [Service]
  User=lumi
  WorkingDirectory=/srv/projects/web-app
  # TELEGRAM_BOT_TOKEN=... (or SLACK_BOT_TOKEN and SLACK_APP_TOKEN), readable only by root
  EnvironmentFile=/etc/lumi/gateway.env
  ExecStart=/usr/bin/lumi gateway --channel telegram --project /srv/projects/web-app --mode ask --allow 123456789
  Restart=on-failure
  # The gateway stops cleanly on Ctrl+C, so systemd sends that.
  KillSignal=SIGINT

  [Install]
  WantedBy=multi-user.target
  ```

  Then `sudo systemctl enable --now lumi-gateway`. See
  [the chat gateway](chat-gateway.md) for channels and allowlists.
- **Organization policy.** `/etc/lumi/policy.json`, with trusted signing keys
  in `/etc/lumi/policy-keys.json`. Make them readable by everyone and writable
  only by root. See [Organization policy](enterprise-policy.md).
- **Containers.** `packaging/docker/Dockerfile` is the other way to run Lumi
  unattended; see [Headless runs](headless.md).

## Updates

- **The .deb and .rpm.** `/opt/lumi/lumi-install.json` marks their copy.
  Lumi never checks for updates there. `lumi updates` reports
  `"installed_by": "deb"` or `"rpm"`, and Settings > Updates says the package
  manager updates it. Install new versions with apt or dnf, or your
  configuration management.
- **The AppImage and tarball** don't update themselves. There's no updater on
  Linux yet.

## Building

On an x86_64 Linux machine with Python 3.13, PowerShell 7 (`pwsh`), curl and
rpmbuild:

```bash
bash packaging/build_linux.sh
```

- **Like the macOS build.** It installs the hash-pinned
  `packaging/requirements-release.txt` in a fresh virtual environment, then
  fetches the pinned web assets and writes the third-party notices.
- **The bundle.** PyInstaller builds it, and
  `packaging/bundle-policy-linux.json` gates it.
- **The packages**, in `dist/installer`, from `packaging/linux_packages.py`:
  - the .deb, written in Python;
  - the .rpm, built by rpmbuild with stripping off, because strip would cut
    off the archive PyInstaller appends to its executable;
  - the AppImage, made by appimagetool 1.9.1 with the type2 runtime
    20251108, both checked against the SHA-256 digests GitHub published;
  - the tarball.
- **Reproducible.** Files get the time in `SOURCE_DATE_EPOCH` (the last
  commit's time by default) and root ownership, so the same bundle gives the
  same packages.
- **The maintainer field.** `LUMI_PACKAGE_MAINTAINER` sets it. The default
  address is a placeholder until Luminary names a packaging contact.

## What has been verified

On every change to the app or packaging, CI (`.github/workflows/build-linux.yml`,
Ubuntu 22.04):

- **The build.** It builds the bundle from the release lock, and the bundle
  policy gate passes.
- **The bundle itself:**
  - `--version` and `lumi updates` run;
  - the GUI server passes the same HTTP, launch-code and WebSocket checks as
    the Windows and macOS builds;
  - `lumi gui` without a native window falls back to the page, and prints the
    link rather than opening a browser without a display.
- **The .deb:**
  - `dpkg-deb --info` reads it, and apt installs it;
  - the installed `lumi` runs `--version` and `run --help`;
  - `lumi updates` reports the deb install with updates off;
  - `/etc/lumi/policy.json` applies;
  - `apt remove` removes `/opt/lumi` and `/usr/bin/lumi`.
- **The .rpm:** `rpm -qip` reads it. In a Fedora 41 container, dnf installs
  it, and `lumi updates` reports the rpm install.
- **The AppImage** runs `--version` and `updates` without FUSE.

`tests/test_linux_packages.py` checks:

- the .deb's structure: control fields, modes and ownership, the symlink,
  md5sums, and identical rebuilds;
- the .rpm spec, the AppImage folder and the tarball;
- the browser fallback.

The .deb writer was also checked with `dpkg-deb` on WSL's Ubuntu, from a
test bundle. dpkg read its fields, listed and extracted its contents, and
ordered its versions correctly.

Not verified yet:

- opening the page from the menu in a real GNOME or KDE session, or on
  Wayland;
- Red Hat Enterprise Linux 9, and arm64;
- the gateway's systemd unit on a real server;
- signed packages or a package repository. There's no apt or dnf repository
  to publish to.
