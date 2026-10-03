"""
Ejecución en la nube (GitHub Actions) del servicio de alertas de Stocks in Play.

Cada ejecución hace UNA revisión del mercado y manda por Telegram las alertas nuevas.
GitHub la lanza cada 10 minutos. Las claves llegan como "secrets" (variables de entorno)
y los ajustes desde ajustes_nube.json. El estado (alertas ya enviadas, caché de datos)
se guarda entre ejecuciones en la carpeta estado/ (con la caché de GitHub).
"""

import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "estado"
FILES = ["alertas_enviadas.json", "alertas_hoy.json", "alertas_estado.json"]

os.environ["STOCKS_CLOUD"] = "1"
STATE.mkdir(exist_ok=True)

# 1) Recuperar el estado de la ejecución anterior
for f in FILES:
    if (STATE / f).exists():
        shutil.copy(STATE / f, HERE / f)
if (STATE / "cache").exists():
    shutil.copytree(STATE / "cache", HERE / "cache", dirs_exist_ok=True)

# 2) Configuración: ajustes (sin claves) + claves desde los secrets de GitHub
try:
    cfg = json.loads((HERE / "ajustes_nube.json").read_text(encoding="utf-8"))
except Exception:
    cfg = {}
cfg.update(
    telegram_token=os.environ.get("TELEGRAM_TOKEN", ""),
    telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
    finnhub_key=os.environ.get("FINNHUB_KEY", ""),
)
if not cfg["telegram_token"] or not cfg["telegram_chat_id"]:
    print("Faltan los secrets TELEGRAM_TOKEN y/o TELEGRAM_CHAT_ID")
    sys.exit(1)
(HERE / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

sys.path.insert(0, str(HERE))
import alertas  # noqa: E402
import core  # noqa: E402

# Prueba manual: si se lanza a mano con "prueba", manda un mensaje de comprobación
if os.environ.get("PRUEBA") == "true":
    ok = core.send_telegram(cfg["telegram_token"], cfg["telegram_chat_id"],
                            "Stocks in Play (nube): conexión correcta.")
    print("Mensaje de prueba enviado" if ok else "No se pudo enviar el mensaje de prueba")

# 3) Una revisión
try:
    alertas.one_pass()
    print("Revisión completada")
finally:
    # 4) Guardar el estado para la próxima ejecución
    for f in FILES:
        if (HERE / f).exists():
            shutil.copy(HERE / f, STATE / f)
    if (HERE / "cache").exists():
        # Solo la caché de hoy, para que no crezca
        shutil.rmtree(STATE / "cache", ignore_errors=True)
        shutil.copytree(HERE / "cache", STATE / "cache")
