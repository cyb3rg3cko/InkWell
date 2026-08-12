FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt requirements-stt.txt ./
# Only needed if INKWELL_ENCRYPTION_KEY is set at runtime, but included
# unconditionally so that feature is always available without a rebuild.
RUN pip install --no-cache-dir -r requirements.txt

# Local speech-to-text (INKWELL_STT_PROVIDER=local) is NOT installed by
# default -- it's a much heavier dependency than anything else this
# image needs. Opt in at build time if you want it baked in:
#   docker build --build-arg INSTALL_STT=1 -t inkwell:latest .
# (If you'd rather use a remote provider -- openai/groq/google -- or
# not use speech-to-text at all, leave this unset; the default build
# skips it entirely and stays lean.)
ARG INSTALL_STT=0
RUN if [ "$INSTALL_STT" = "1" ]; then pip install --no-cache-dir -r requirements-stt.txt; fi

COPY index.html Python_HTTPS_Server.py README.md ./

# Never baked into the image -- these are runtime secrets, set them
# via `docker run -e` / an env file / Docker secrets (the _FILE
# variants), or your orchestrator's secret mechanism. See README.md
# for the full list of INKWELL_* variables.
ENV INKWELL_HEADLESS=1 \
    INKWELL_WORKSPACE_DIR=/data \
    INKWELL_HOST=0.0.0.0 \
    INKWELL_PORT=8060 \
    INKWELL_TLS=0

VOLUME ["/data"]
EXPOSE 8060

CMD ["python", "Python_HTTPS_Server.py"]
