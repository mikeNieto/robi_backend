#!/usr/bin/env bash
# scripts/generate_certs.sh
#
# Genera un certificado TLS autofirmado con SAN,
# listo para certificate pinning en Android.
#
# La IP/hostname se resuelve en este orden de prioridad:
#   1. Argumento: bash scripts/generate_certs.sh <ip>
#   2. Variable de entorno: SERVER_IP=1.2.3.4 bash scripts/generate_certs.sh
#   3. Valor de SERVER_IP en .env
#   4. Fallback: 192.168.2.200
#
# Salida:
#   nginx/certs/server.key  — clave privada RSA 4096 bits
#   nginx/certs/server.crt  — certificado X.509 autofirmado (10 años)
#
# Requiere: openssl

set -euo pipefail

# ── Configuración ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CERTS_DIR="$ROOT_DIR/nginx/certs"

# Leer SERVER_IP en orden de prioridad:
#   1. Argumento posicional  bash scripts/generate_certs.sh <ip>
#   2. Variable de entorno   SERVER_IP=1.2.3.4 bash scripts/generate_certs.sh
#   3. Valor en .env         SERVER_IP=...
#   4. Fallback              192.168.2.200
if [[ -f "$ROOT_DIR/.env" ]]; then
    ENV_IP=$(grep -E '^SERVER_IP=' "$ROOT_DIR/.env" | head -1 | cut -d'=' -f2 | tr -d '[:space:]' || true)
else
    ENV_IP=""
fi
SERVER_IP="${1:-${SERVER_IP:-${ENV_IP:-192.168.2.200}}}"
DAYS=3650   # ~10 años
KEY_BITS=4096

# ── Crear directorio si no existe ─────────────────────────────────────────────
mkdir -p "$CERTS_DIR"

echo "========================================================"
echo " Robi — Generador de certificado TLS autofirmado"
echo "========================================================"
echo " IP/SAN  : $SERVER_IP"
echo " Validez : $DAYS días (~10 años)"
echo " Destino : $CERTS_DIR"
echo "========================================================"
echo ""

# ── Generar clave + certificado con SAN ───────────────────────────────────────
# La extensión subjectAltName es obligatoria en Android 7+ para certificate pinning.

SAN_TYPE="IP"
# Si el argumento parece un hostname (contiene letras) usar DNS en lugar de IP
if [[ "$SERVER_IP" =~ [a-zA-Z] ]]; then
    SAN_TYPE="DNS"
fi

openssl req -x509 \
    -newkey rsa:${KEY_BITS} \
    -keyout "$CERTS_DIR/server.key" \
    -out    "$CERTS_DIR/server.crt" \
    -days   "$DAYS" \
    -nodes \
    -subj   "/CN=$SERVER_IP/O=Robi Robot/OU=Dev" \
    -addext "subjectAltName=${SAN_TYPE}:${SERVER_IP}"

echo "✅  Certificado generado correctamente."
echo ""

# ── Mostrar resumen del certificado ──────────────────────────────────────────
echo "── Información del certificado ──────────────────────────"
openssl x509 -in "$CERTS_DIR/server.crt" -noout \
    -subject -issuer -dates -ext subjectAltName 2>/dev/null || true
echo ""

# ── Fingerprint SHA-256 para certificate pinning en Android ──────────────────
echo "── SHA-256 Fingerprint (para AndroidManifest / network_security_config.xml) ──"
FINGERPRINT=$(openssl x509 -in "$CERTS_DIR/server.crt" -noout -fingerprint -sha256 \
    | sed 's/.*Fingerprint=//' \
    | tr -d ':' \
    | tr '[:upper:]' '[:lower:]')

echo ""
echo "  Hex bruto  : $(openssl x509 -in "$CERTS_DIR/server.crt" -noout -fingerprint -sha256 | sed 's/.*Fingerprint=//')"
echo ""
echo "  Base64     : $(openssl x509 -in "$CERTS_DIR/server.crt" -outform DER \
    | openssl dgst -sha256 -binary \
    | openssl base64)"
echo ""
echo "  Para res/xml/network_security_config.xml:"
echo ""
echo '  <network-security-config>'
echo '    <domain-config cleartextTrafficPermitted="false">'
echo "      <domain includeSubdomains=\"false\">$SERVER_IP</domain>"
echo '      <pin-set>'
echo "        <pin digest=\"SHA-256\">$(openssl x509 -in "$CERTS_DIR/server.crt" -outform DER \
    | openssl dgst -sha256 -binary \
    | openssl base64)</pin>"
echo '      </pin-set>'
echo '    </domain-config>'
echo '  </network-security-config>'
echo ""
echo "========================================================"
echo " Archivos generados:"
echo "   $CERTS_DIR/server.key"
echo "   $CERTS_DIR/server.crt"
echo "========================================================"
