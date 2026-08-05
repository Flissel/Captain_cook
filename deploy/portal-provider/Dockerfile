FROM python:3.11.13-slim-bookworm@sha256:cec9aa7aa96eea4fa036e9b82be1e6b325f2e3707f462d885868df51ec0a4b47

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 captain \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin captain \
    && install -d -o 10001 -g 10001 -m 0700 /data /app

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --requirement /app/requirements.txt
COPY --chown=10001:10001 portal_provider /app/portal_provider

USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "portal_provider.server:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

