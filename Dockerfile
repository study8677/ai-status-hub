FROM python:3.12-slim

WORKDIR /app

COPY monitor.py services.json README.md ./
COPY docs/schema ./docs/schema

RUN mkdir -p data output reports public

ENTRYPOINT ["python3", "monitor.py"]
CMD ["loop", "--interval", "300"]
