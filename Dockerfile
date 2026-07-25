FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY graveyard_shift/ graveyard_shift/
EXPOSE 8090
CMD ["python", "-m", "graveyard_shift"]
