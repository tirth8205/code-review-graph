# syntax=docker/dockerfile:1

FROM python:3.12-slim

ARG CRG_VERSION=2.3.7

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir "code-review-graph==${CRG_VERSION}"

RUN groupadd --gid 1001 crg \
    && useradd --uid 1001 --gid crg --create-home --shell /usr/sbin/nologin crg

WORKDIR /workspace

RUN chown crg:crg /workspace

USER crg

ENTRYPOINT ["code-review-graph"]
CMD ["--help"]
