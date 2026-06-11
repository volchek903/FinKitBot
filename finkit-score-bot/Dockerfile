FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=20

WORKDIR /app

COPY requirements.txt .
COPY vendor/wheels /tmp/wheels
RUN --mount=type=cache,target=/root/.cache/pip \
    for attempt in 1 2 3 4 5; do \
        python -m pip install --retries 20 --timeout 120 /tmp/wheels/playwright-1.60.0-py3-none-manylinux1_x86_64.whl && \
        python -m pip install --retries 20 --timeout 120 -r requirements.txt && exit 0; \
        echo "pip install failed, retrying in 10 seconds..."; \
        sleep 10; \
    done; \
    exit 1

COPY . .

CMD ["python", "-m", "app.main"]
