FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libgl1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 autostacgis && mkdir /data && chown autostacgis /data
USER autostacgis
ENV AUTOSTACGIS_DATA_DIR=/data
EXPOSE 8080
CMD ["autostacgis-server", "serve", "--host", "0.0.0.0"]
