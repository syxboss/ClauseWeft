FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY LICENSE ./
COPY src/clauseweft/ ./clauseweft/
COPY examples/route-demo.json ./examples/route-demo.json
USER 65532:65532

ENTRYPOINT ["python", "-m", "clauseweft"]
CMD ["doctor"]
