# --- catlogautomatcher: imagen para correr el API de FastAPI ---
#
# Corre lo mismo que hoy corre en Railway: uvicorn sirviendo main:app.
# Usamos python:3.11-slim (Debian) -- no una imagen "alpine" -- porque
# tesseract-ocr y poppler-utils (el OCR de /tarjeta-circulacion) se
# instalan via apt-get y necesitan una base Debian/Ubuntu.

FROM python:3.11-slim

# Paquetes de sistema: tesseract (motor OCR) + su paquete de idioma
# español + poppler-utils (pdf2image usa 'pdftoppm' de aqui para leer
# PDFs). Sin esto, pytesseract/pdf2image truenan al llamar al binario --
# esto es justo lo que Cloudflare Workers NO puede correr, y la razon por
# la que este endpoint necesita un contenedor real.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-spa \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiamos primero SOLO los requirements. Docker cachea cada instruccion
# por capas -- mientras no toques estos 3 archivos, un rebuild reusa la
# capa del pip install entero (no vuelve a bajar nada), aunque hayas
# cambiado main.py. Por eso el codigo (que cambia seguido) va DESPUES.
COPY requirements.txt requirements-ocr.txt requirements-ghl.txt requirements-mcp.txt ./
RUN pip install --no-cache-dir \
    -r requirements.txt \
    -r requirements-ocr.txt \
    -r requirements-ghl.txt \
    -r requirements-mcp.txt

COPY . .

# El CSV real (data/tablotas/default.csv) NO se hornea en la imagen --
# sigue igual que en Railway/git: es gitignored y se coloca en runtime.
# Ver .dockerignore.

ENV OCR_LANG=spa+eng
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
