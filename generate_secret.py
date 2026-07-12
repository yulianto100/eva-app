#!/usr/bin/env python3
"""Generate secure SECRET_KEY untuk EVA Converter."""

from config import generate_secret_key

print(f"SECRET_KEY={generate_secret_key()}")
