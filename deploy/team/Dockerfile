FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --system biomni && useradd --system --gid biomni --home-dir /srv/biomni biomni
WORKDIR /srv/biomni
COPY pyproject.toml README.md ./
COPY biomni_dual ./biomni_dual
COPY scripts/team_server.py ./scripts/team_server.py
RUN pip install --no-cache-dir '.[server]' && mkdir -p /srv/biomni/state && chown -R biomni:biomni /srv/biomni/state
USER biomni
EXPOSE 8766
CMD ["python","scripts/team_server.py","--host","0.0.0.0","--port","8766"]
