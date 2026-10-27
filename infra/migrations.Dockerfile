FROM python:3.12-slim-bookworm@sha256:72c1d56207fb14ca388f9b3855ac0e55ab557703928180ec40aa9d4c4f5d5f80
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY api/requirements.txt api/runtime-requirements.txt /app/api/
COPY contracts/requirements.txt /app/contracts/
COPY db/requirements.txt /app/db/
RUN python -m pip install --disable-pip-version-check --no-cache-dir --only-binary=:all: -r db/requirements.txt && python -m pip check
COPY api/__init__.py api/database.py /app/api/
COPY contracts/__init__.py contracts/canonical.py /app/contracts/
COPY db/*.py /app/db/
COPY db/migrations /app/db/migrations/
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "db.worker_http"]
