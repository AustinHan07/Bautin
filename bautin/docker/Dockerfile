# Bautin sandbox: the Hermes docker terminal backend image for lanes that run Playwright scripts.
# Official Playwright Python image (Ubuntu noble, Chromium + system deps, multi-arch incl. arm64).
ARG PLAYWRIGHT_TAG=v1.63.0-noble
FROM mcr.microsoft.com/playwright/python:${PLAYWRIGHT_TAG}
# The base image ships the browsers (/ms-playwright) but not the Python package; pin the same version.
ARG PLAYWRIGHT_VERSION=1.63.0
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1
# ripgrep + jq for the agent's own shell work; PyYAML for scripts that read vault frontmatter.
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep jq git curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pyyaml playwright==${PLAYWRIGHT_VERSION}
WORKDIR /workspace
