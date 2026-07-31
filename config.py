import os
from dotenv import load_dotenv

load_dotenv()

# "desktop" (default, no auth, ws://) or "enterprise" (certificate auth, wss://)
QLIK_MODE = os.getenv("QLIK_MODE", "desktop").lower()

_default_port = "4747" if QLIK_MODE == "enterprise" else "4848"
QLIK_HOST = os.getenv("QLIK_HOST", "localhost")
QLIK_PORT = int(os.getenv("QLIK_PORT", _default_port))
APP_NAME = os.getenv("APP_NAME", "data")

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "phi4:14b")

# Only used when QLIK_MODE=enterprise: a direct, certificate-authenticated
# connection to the Engine API, bypassing the proxy. Export the three certs
# (client.pem, client_key.pem, root.pem) from the QMC's Certificates section
# and point QLIK_CERT_DIR at the folder containing them.
QLIK_CERT_DIR = os.getenv("QLIK_CERT_DIR", "")
QLIK_USER_DIRECTORY = os.getenv("QLIK_USER_DIRECTORY", "")
QLIK_USER_ID = os.getenv("QLIK_USER_ID", "")

