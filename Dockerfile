# Single image, three roles. The trader, supervisor and dashboard run the same
# code with different entrypoints, so there is no chance of the guard process
# running a different version of the risk logic than the trader it guards.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code changes do not invalidate the layer.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
      "alpaca-py>=0.33" "pydantic>=2.9" "pydantic-settings>=2.6" "PyYAML>=6.0" \
      "python-dotenv>=1.0" "httpx>=0.27" "openai>=1.50" "numpy>=2.0" \
      "scikit-learn>=1.5" "fastapi>=0.115" "uvicorn>=0.32"

COPY glassbox/ ./glassbox/
COPY config/ ./config/
COPY models/ ./models/

# Never run as root: a container that can rewrite its own code is a larger
# blast radius than this workload needs.
RUN useradd --create-home --uid 10001 glassbox \
    && mkdir -p /app/data /app/audit \
    && chown -R glassbox:glassbox /app
USER glassbox

# Default role; compose overrides per service.
CMD ["python", "-m", "glassbox.runner", "--dry-run"]
