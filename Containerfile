# Pinned by digest (see below) so a rebuilt image is the same image.
#    podman pull docker.io/library/python:3.14-alpine
#    podman image inspect docker.io/library/python:3.14-alpine --format '{{.Digest}}'
FROM docker.io/library/python@sha256:7f2ec713f0436c59df2362d973b6a05af9e87ba78f0e94f5245c302984a7a1fd

WORKDIR /app

# The lock, not requirements.txt: deploy.sh rebuilds from scratch on
# every deploy, and an unpinned closure means a rebuild can silently
# change the transitive tree. Regenerate with scripts/lock.py.
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

COPY server.py .
COPY healthcheck.py .

USER nobody
EXPOSE 8000
CMD ["python", "server.py"]
