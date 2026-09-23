FROM python:3.12-slim AS builder

WORKDIR /build

COPY pyproject.toml README.md LICENSE ./
COPY code_review_graph/ ./code_review_graph/
COPY skills/ ./skills/

RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1 \
  HOME=/home/crg

RUN apt-get update \
  && apt-get install -y --no-install-recommends git ca-certificates \
  && rm -rf /var/lib/apt/lists/* \
  && groupadd --gid 10001 crg \
  && useradd --uid 10001 --gid crg --create-home crg \
  && mkdir /workspace \
  && chown crg:crg /workspace

COPY --from=builder /wheels /wheels

RUN python -m pip install --no-cache-dir --no-index \
  --find-links=/wheels code-review-graph \
  && rm -rf /wheels

USER crg:crg
WORKDIR /workspace

ENTRYPOINT ["code-review-graph"]
CMD ["--help"]
