"""
tests/conftest.py — Variables de entorno mínimas para todos los tests.

Se ejecuta antes de cualquier módulo de test, garantizando que
`Settings()` no falle por falta de API_KEY o GEMINI_API_KEY.
"""

import os

# Valores de prueba — nunca usados en producción
os.environ.setdefault("API_KEY", "test-api-key-for-unit-tests-only")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key-not-used-in-unit-tests")
os.environ.setdefault("ENVIRONMENT", "development")
