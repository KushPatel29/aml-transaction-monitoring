# One image for every streaming workload: topics, produce, process, verify.
# Build from the repository root:  docker build -f streaming/Dockerfile -t aml-stream:ci .
FROM python:3.12-slim

RUN useradd --uid 10001 --no-create-home monitor
WORKDIR /app
COPY streaming/requirements.txt streaming/requirements.txt
RUN pip install --no-cache-dir -r streaming/requirements.txt

# Only what the stream and its batch reference read: the rules, their
# thresholds and the synthetic transactions.
COPY config.py ./
COPY src/__init__.py src/rules_engine.py src/
COPY data/generated/transactions.csv.gz data/generated/
COPY streaming/ streaming/

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001
ENTRYPOINT ["python", "-m", "streaming"]
