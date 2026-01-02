FROM python:3.12 AS builder

RUN curl -LsSf https://astral.sh/uv/0.9.21/install.sh | UV_INSTALL_DIR=/usr/bin sh

COPY . .

RUN uv export --no-emit-workspace --no-dev --no-annotate --no-header \
    --no-hashes --output-file /requirements.txt

RUN python -m venv /venv && \
    /venv/bin/python -m pip install -r /requirements.txt

FROM python:3.12-slim
ENV PATH="/venv/bin:$PATH"

COPY --from=builder /venv /venv
COPY . /app
WORKDIR /app

CMD ["/app/submission_checker.py"]
