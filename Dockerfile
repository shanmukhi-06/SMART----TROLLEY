# Smart Trolley cloud app.
#
# This image runs the web app + API ONLY. It never touches GPIO: the load cell
# lives on the Raspberry Pi and reaches this container through the device
# bridge (see pi_agent.py), so the image stays slim and architecture-neutral.
FROM python:3.12-slim

# uvicorn needs no build tools; psycopg ships manylinux wheels.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so layer caching survives code edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# StaticFiles and Jinja2 are mounted with relative paths ("app/static",
# "app/templates"), so the working directory must stay the repo root.
COPY app ./app
COPY run.py calibrate_scale.py pi_agent.py ./

# Receipts are written here. Mount a volume or switch to S3 for anything
# you need to keep — a container filesystem is ephemeral.
RUN mkdir -p data/receipts

# Run as a non-root user.
RUN useradd --create-home --uid 10001 trolley \
    && chown -R trolley:trolley /app
USER trolley

ENV HARDWARE_MODE=cloud \
    HOST=0.0.0.0 \
    PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4).status==200 else 1)"

# IMPORTANT: one worker only.
# The device bridge, the active-session pointer and the payment intents are all
# in-process state. A second worker would see a different load cell and a
# different cart. Scale by adding trolleys, not workers; if you ever need more
# replicas, move app/devices.py behind Redis or AWS IoT Core first.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
