FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY graveyard_shift/ graveyard_shift/
# scripts and fixtures carry the credential-free simulation, which is the way
# to inspect the workflow without an API key:
#   docker compose run --rm orchestrator python scripts/simulate.py
COPY scripts/ scripts/
COPY fixtures/ fixtures/
EXPOSE 8090
CMD ["python", "-m", "graveyard_shift"]
