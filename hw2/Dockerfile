FROM openapitools/openapi-generator-cli:v7.12.0 AS codegen

WORKDIR /workspace

COPY openapi/ ./openapi/

RUN java -jar /opt/openapi-generator/modules/openapi-generator-cli/target/openapi-generator-cli.jar \
    generate \
    -i /workspace/openapi/marketplace.yaml \
    -g python-fastapi \
    -c /workspace/openapi/generator-config.yaml \
    -o /out


FROM python:3.12-slim AS runtime

WORKDIR /app

COPY requirements.txt ./

RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --from=codegen /out/src/ ./generated/src/
COPY app/ ./app/

ENV PYTHONPATH=/app:/app/generated/src
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]


FROM runtime AS test

COPY requirements-test.txt ./
RUN python -m pip install --no-cache-dir -r requirements-test.txt
COPY pytest.ini ./
COPY tests/ ./tests/
COPY openapi/marketplace.yaml ./openapi/marketplace.yaml

CMD ["python", "-m", "pytest", "-q"]


FROM runtime AS application
