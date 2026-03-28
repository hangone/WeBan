FROM python:3.12-slim

# Copy uv from the official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Copy dependency definition files
COPY pyproject.toml uv.lock ./

# Install Python dependencies using uv (this uses opencv-python-headless from pyproject.toml)
RUN uv sync --frozen --no-dev

# Install Playwright Chromium browser and its system dependencies
RUN uv run playwright install --with-deps chromium

# Copy the rest of the application code
COPY . .

# Run the application
CMD ["uv", "run", "main.py"]