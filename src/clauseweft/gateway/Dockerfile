FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY LICENSE ./LICENSE
COPY *.py ./clauseweft/gateway/
COPY container.example.json /etc/clauseweft/gateway.json
USER 65532:65532
EXPOSE 8765
ENTRYPOINT ["python", "-m", "clauseweft.gateway"]
CMD ["--config", "/etc/clauseweft/gateway.json", "--host", "0.0.0.0", "--port", "8765"]
