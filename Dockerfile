FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/OrganoidSchnitzel/Immich-age-guesser" \
      org.opencontainers.image.description="Date scanned photos in Immich from the ages of the people in them" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    HF_HOME=/data/hf-cache

# CPU-only PyTorch by default (small, runs everywhere). For an AMD GPU with ROCm, build with e.g.
#   --build-arg TORCH_INDEX=https://download.pytorch.org/whl/rocm6.2
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
# torch and torchvision (needed by timm) must come from the same index; the constraints file keeps the
# following install from replacing them with mismatching builds from PyPI.
RUN pip install torch torchvision --index-url ${TORCH_INDEX} \
 && pip freeze | grep -iE "^(torch|torchvision)==" > /tmp/torch-constraints.txt

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[mivolo]" -c /tmp/torch-constraints.txt

# MiVOLO's model code (Apache-2.0) is not on PyPI, and its setup.py would pull in old pinned
# dependencies (timm 0.8, ultralytics) that the age model does not need. Copy just the package.
ARG MIVOLO_REF=37475e3f8818b5f22448003feec3e64b01bfb188
ADD https://github.com/WildChlamydia/MiVOLO/archive/${MIVOLO_REF}.tar.gz /tmp/mivolo.tar.gz
RUN tar -xzf /tmp/mivolo.tar.gz -C /tmp \
 && cp -r /tmp/MiVOLO-${MIVOLO_REF}/mivolo "$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/" \
 && rm -rf /tmp/mivolo.tar.gz /tmp/MiVOLO-${MIVOLO_REF}

VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["immich-age-guesser", "serve", "--port", "8080"]
