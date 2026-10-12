# uv is the only build tool end to end: it turns uv.lock into a
# hash-pinned requirements file, and it installs that file. No pip
# anywhere. The uv binary is copied into the runtime stage, used, and
# deleted, so nothing but Python and the app remain in the image.

# Pinned by digest like the runtime base below: a moved tag would change
# the build tool without a commit.
FROM ghcr.io/astral-sh/uv@sha256:20d3fe1308ffa40b51c5c20e002bab5fece40585edcee8ec99357309e8363424 AS lockfile
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
# and --require-hashes means uv actually enforces them. Both build tools
# leave the image after they are done with it.
COPY --from=lockfile /usr/local/bin/uv /usr/local/bin/uv
COPY --from=lockfile /requirements.txt .
RUN uv pip install --system --require-hashes -r requirements.txt \
    && uv cache clean \
    && rm /usr/local/bin/uv requirements.txt \
    && python -m pip uninstall -y pip

COPY server.py .
COPY readability.py .
COPY wayback.py .
COPY healthcheck.py .

USER nobody
EXPOSE 8000
CMD ["python", "server.py"]
