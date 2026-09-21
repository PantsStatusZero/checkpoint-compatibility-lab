FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
COPY src /app/src

RUN python -m pip install --upgrade pip setuptools wheel \
 && python -m pip install torch==2.10.0 --index-url https://download.pytorch.org/whl/cpu \
 && python -m pip install -e .

ENV PYTHONPATH=/app/src

ENTRYPOINT ["python", "-m", "checkpoint_compatibility_lab.qualify"]
