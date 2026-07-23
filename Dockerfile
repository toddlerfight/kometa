FROM python:3.12-slim

WORKDIR /app

# libarchive-tools = bsdtar (the sequential-extract workhorse for solid RARs).
# unar = a real RAR backend for the rarfile lib: libarchive's RAR5 reader is
# partial (STORE-method archives verified working here, but real-world
# compressed/solid v5 rips are exactly where it falls over), and The
# Unarchiver speaks the whole format. rarfile auto-detects unar once present.
RUN apt-get update && apt-get install -y --no-install-recommends libarchive-tools unar && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY kometa/ kometa/

EXPOSE 6969

CMD ["uvicorn", "kometa.main:app", "--host", "0.0.0.0", "--port", "6969"]
