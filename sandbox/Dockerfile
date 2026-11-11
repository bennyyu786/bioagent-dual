FROM python:3.11-slim
ARG PYTEST_VERSION=8.3.5
ARG MCP_VERSION=1.12.4
RUN pip install --no-cache-dir "pytest==${PYTEST_VERSION}" "mcp==${MCP_VERSION}" \
    && groupadd -g 10001 sandbox && useradd -u 10001 -g sandbox -m sandbox
COPY harness/mcp_probe.py /harness/mcp_probe.py
USER 10001:10001
WORKDIR /workspace
