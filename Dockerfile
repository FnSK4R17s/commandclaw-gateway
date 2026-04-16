FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml .
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /install /usr/local
COPY . .

EXPOSE 4000

# v1.1: Add non-root user and read-only filesystem
# RUN addgroup --system --gid 101 gateway && \
#     adduser --system --uid 101 --ingroup gateway gateway
# USER gateway

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "4000"]
