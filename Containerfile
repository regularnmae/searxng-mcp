# uv exists only to turn uv.lock into a hash-pinned requirements file.
# The installed image contains no uv, and pip installs with
# --require-hashes, so those hashes are enforced rather than decorative.

FROM docker.io/library/python:3.14-alpine AS lockfile
# uv publishes musllinux wheels, so pip installs it on Alpine as-is.
RUN pip install --no-cache-dir uv
WORKDIR /build
COPY pyproject.toml uv.lock ./
# --locked: fail rather than re-resolve, so a stale lock breaks the build
# instead of quietly shipping different versions.
RUN uv export --format requirements-txt --no-dev --no-emit-project --locked \
      -o /requirements.txt

FROM docker.io/library/python@sha256:7f2ec713f0436c59df2362d973b6a05af9e87ba78f0e94f5245c302984a7a1fd
WORKDIR /app

# The lock, not a bare dependency list: deploy.sh rebuilds from scratch
# on every deploy, and an unpinned closure means a rebuild can silently
# change the transitive tree. The hashes come from uv export for free,
# and --require-hashes means pip actually enforces them.
COPY --from=lockfile /requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && rm requirements.txt

COPY server.py .
COPY healthcheck.py .

USER nobody
EXPOSE 8000
CMD ["python", "server.py"]
