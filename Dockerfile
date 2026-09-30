FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY journey journey
COPY data/pended_cases.json data/
ENV JOURNEY_DB_URL=sqlite:////tmp/journeys.sqlite3
EXPOSE 8930
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8930/health')"
CMD ["uvicorn", "journey.api:app", "--host", "0.0.0.0", "--port", "8930"]
