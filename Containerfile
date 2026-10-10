FROM docker.io/library/python:3.14-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY healthcheck.py .
USER nobody
EXPOSE 8000
CMD ["python", "server.py"]
