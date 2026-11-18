FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AML_DB_PATH=/data/memoryshield-agent.db \
    AML_VERSION=v3.17.1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 memoryshield \
    && useradd --uid 10001 --gid 10001 --no-create-home memoryshield \
    && mkdir /data \
    && chown memoryshield:memoryshield /data /app

COPY app.py config.py store.py add_memory.py add_index.py facts.py governance.py evidence_links.py query_plan.py source_dependencies.py reranker.py state_evidence.py context_data.py LICENSE NOTICE.txt PROVENANCE.txt ./
RUN chown -R memoryshield:memoryshield /app
USER memoryshield
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).close()"

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
