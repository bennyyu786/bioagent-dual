FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY biomni_dual ./biomni_dual
COPY scripts ./scripts
RUN pip install --no-cache-dir . && groupadd -r app && useradd -r -u 10001 -g app -d /home/app app && mkdir -p /state && chown app:app /state
USER app
ENV PYTHONPATH=/app
ENTRYPOINT ["python", "scripts/runtime_daemon.py"]
CMD ["--bootstrap", "config.runtime_factory:create_runtime", "--observability-db", "/state/operations.sqlite", "--reconcile-startup"]
