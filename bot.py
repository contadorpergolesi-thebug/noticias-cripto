#!/usr/bin/env python3
"""
Lee feeds RSS de medios cripto y publica las novedades en un canal de Telegram.
Formato: titular + resumen corto + enlace (uso legal de titular + extracto breve).
"""

import html
import json
import os
import re
import sys
import time
from pathlib import Path

import feedparser
import requests

# ---------------------------------------------------------------------------
# CONFIGURACION — edita esta lista a tu gusto
# ---------------------------------------------------------------------------
FEEDS = {
   "Cointelegraph": "https://news.google.com/rss/search?q=site:es.cointelegraph.com&hl=es-419&gl=AR&ceid=AR:es",
    "CriptoNoticias": "https://news.google.com/rss/search?q=site:criptonoticias.com&hl=es-419&gl=AR&ceid=AR:es",
    "BeInCrypto": "https://es.beincrypto.com/feed/",
    "Bit2Me News": "https://news.bit2me.com/feed/",
    # "Observatorio Blockchain": "https://www.observatorioblockchain.com/feed/",
    # "DiarioBitcoin": "https://www.diariobitcoin.com/feed/",
}

MAX_POR_EJECUCION = 8      # tope de mensajes por ronda (evita inundar el canal)
LARGO_RESUMEN = 220        # caracteres del extracto
SEGUNDOS_ENTRE_MENSAJES = 4
ESTADO = Path("seen.json")  # historial de enlaces ya publicados

# Solo publica si el titular contiene alguna de estas palabras.
# Deja la lista vacia para publicar todo.
PALABRAS_CLAVE = []
# Ejemplo: PALABRAS_CLAVE = ["bitcoin", "ethereum", "sec", "etf"]

# ---------------------------------------------------------------------------

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
API = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


def limpiar(texto: str) -> str:
    """Quita etiquetas HTML y espacios sobrantes."""
    texto = re.sub(r"<[^>]+>", " ", texto or "")
    texto = html.unescape(texto)
    return re.sub(r"\s+", " ", texto).strip()


def recortar(texto: str, limite: int) -> str:
    if len(texto) <= limite:
        return texto
    corte = texto[:limite].rsplit(" ", 1)[0]
    return corte + "..."


def cargar_estado() -> dict:
    if ESTADO.exists():
        try:
            return json.loads(ESTADO.read_text())
        except json.JSONDecodeError:
            pass
    return {"vistos": [], "inicializado": False}


def guardar_estado(estado: dict) -> None:
    # Conserva solo los ultimos 800 enlaces para que el archivo no crezca
    estado["vistos"] = estado["vistos"][-800:]
    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=1))


def construir_mensaje(medio: str, entrada) -> str:
    titulo = limpiar(entrada.get("title", "Sin titulo"))
    resumen = limpiar(entrada.get("summary", ""))
    enlace = entrada.get("link", "")

    partes = [f"<b>{html.escape(titulo)}</b>"]
    if resumen and resumen.lower() != titulo.lower():
        partes.append(html.escape(recortar(resumen, LARGO_RESUMEN)))
    partes.append(f'{html.escape(enlace)}\n\n<i>Fuente: {html.escape(medio)}</i>')
    return "\n\n".join(partes)


def enviar(texto: str) -> bool:
    try:
        r = requests.post(
            API,
            json={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"  ! error de red: {e}")
        return False

    if r.status_code == 429:
        espera = r.json().get("parameters", {}).get("retry_after", 30)
        print(f"  ! limite de Telegram, esperando {espera}s")
        time.sleep(espera + 1)
        return enviar(texto)

    if not r.ok:
        print(f"  ! Telegram respondio {r.status_code}: {r.text[:200]}")
        return False
    return True


def interesa(entrada) -> bool:
    if not PALABRAS_CLAVE:
        return True
    titulo = limpiar(entrada.get("title", "")).lower()
    return any(p.lower() in titulo for p in PALABRAS_CLAVE)


def main() -> int:
    estado = cargar_estado()
    vistos = set(estado["vistos"])
    primera_vez = not estado["inicializado"]

    nuevas = []
    NAVEGADOR = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/rss+xml, application/xml, text/xml, text/html, */*",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    }

    for medio, url in FEEDS.items():
        print(f"Leyendo {medio}...")
        try:
            respuesta = requests.get(url, headers=NAVEGADOR, timeout=30)
            respuesta.raise_for_status()
            feed = feedparser.parse(respuesta.content)
        except requests.RequestException as e:
            print(f"  ! no se pudo descargar: {e}")
            continue
        if feed.bozo and not feed.entries:
            print(f"  ! no se pudo leer: {feed.get('bozo_exception')}")
            continue
        for entrada in feed.entries[:20]:
            clave = entrada.get("id") or entrada.get("link")
            if not clave or clave in vistos:
                continue
            vistos.add(clave)
            estado["vistos"].append(clave)
            if interesa(entrada):
                nuevas.append((medio, entrada))

    if primera_vez:
        # En el primer arranque no publicamos el historico entero.
        estado["inicializado"] = True
        guardar_estado(estado)
        print(f"Primera ejecucion: {len(nuevas)} noticias marcadas como vistas, sin publicar.")
        return 0

    print(f"{len(nuevas)} noticias nuevas")
    enviadas = 0
    for medio, entrada in nuevas[:MAX_POR_EJECUCION]:
        if enviar(construir_mensaje(medio, entrada)):
            enviadas += 1
            print(f"  -> {limpiar(entrada.get('title', ''))[:70]}")
        time.sleep(SEGUNDOS_ENTRE_MENSAJES)

    guardar_estado(estado)
    print(f"Publicadas {enviadas} noticias.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
