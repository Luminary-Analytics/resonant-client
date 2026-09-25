# Lumi for unattended runs on servers, in containers and in CI (`lumi run`).
#
# Build from the repository root:
#   docker build -f packaging/docker/Dockerfile -t lumi .
# Run a task against the current folder (see docs/headless.md):
#   docker run --rm -v "$PWD:/work" -e ANTHROPIC_API_KEY lumi \
#       run "Fix the failing test" --provider anthropic --model claude-sonnet-5 --mode bypass
#
# The image has no desktop app, browser or computer use. Keys come from the
# environment; there is no OS credential store in a container. State (usage
# records, the audit log) lives in /home/lumi/.lumi, so mount a volume there
# to keep it.

FROM python:3.12-slim

# git for the agent's git tools and checks; ripgrep for fast search.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 lumi

WORKDIR /opt/lumi
COPY pyproject.toml README.md LICENSE ./
COPY lumi ./lumi
RUN pip install --no-cache-dir . && rm -rf /opt/lumi/build

USER lumi
ENV LUMI_KEYCHAIN=off \
    PYTHONUNBUFFERED=1
# The mounted project is often owned by another user id.
RUN git config --global --add safe.directory '*'
WORKDIR /work

ENTRYPOINT ["lumi"]
CMD ["run", "--help"]
