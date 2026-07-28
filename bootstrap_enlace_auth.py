"""bootstrap_enlace_auth.py
=====================================================================
Inserta el refresh_token INICIAL del Portal Enlace en Supabase.

Cómo obtener el refresh_token:
  1. Abre https://portalenlace.copec.cl en tu navegador y loguéate.
  2. Abre DevTools (F12) → pestaña Application → Local Storage
     → https://portalenlace.copec.cl
  3. Copia el valor de la clave 'refresh_token'.
  4. Pégalo cuando este script lo pida (o pásalo como argumento).

Uso:
    python bootstrap_enlace_auth.py <refresh_token>
o simplemente:
    python bootstrap_enlace_auth.py
    → te lo pide interactivo.

Solo hay que correr esto:
  - La primera vez (cuando enlace_auth está vacía)
  - Si el refresh_token expiró / se corrompió (last_error != NULL)
"""

import os
import sys
import requests
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]


def main():
    if len(sys.argv) > 1:
        rt = sys.argv[1].strip()
    else:
        print("Pega aquí el refresh_token (desde localStorage del navegador):")
        rt = input("> ").strip()

    if not rt or len(rt) < 20:
        print("refresh_token vacío o inválido.")
        sys.exit(1)

    body = {
        "id": 1,
        "refresh_token": rt,
        "access_token": None,
        "expires_at": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_error": None,
    }
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    r = requests.post(f"{SUPABASE_URL}/rest/v1/enlace_auth", headers=headers, json=body, timeout=20)
    if r.status_code >= 400:
        print(f"Error {r.status_code}: {r.text}")
        sys.exit(1)
    print("OK — refresh_token guardado en enlace_auth. Ya puedes correr sync_enlace.py")


if __name__ == "__main__":
    main()
