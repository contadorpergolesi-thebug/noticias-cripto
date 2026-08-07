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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
import socket
import urllib3.util.connection as urllib3_conexion

# Los servidores de GitHub no tienen IPv6: forzamos IPv4 para evitar
# el error "Network is unreachable" en webs que responden por IPv6.
urllib3_conexion.allowed_gai_family = lambda: socket.AF_INET

# ---------------------------------------------------------------------------
# CONFIGURACION — edita esta lista a tu gusto
# ---------------------------------------------------------------------------
FEEDS = {
    # Contenido propio: va primero para que tenga prioridad al publicar.
    "Cripto Contador · YouTube": "https://www.youtube.com/feeds/videos.xml?channel_id=UCCMRtM4Gx0QfrK7gPq0aeGA",
    "Cripto Contador · Blog": "https://cripto-contador.com/feed/",

    "Bitunix": "https://blog.bitunix.com/en/feed/",

    "BeInCrypto": "https://es.beincrypto.com/feed/",
    "Bit2Me News": "https://news.bit2me.com/feed/",
    "iProfesional": "https://www.iprofesional.com/rss/finanzas",
    # Retirados:
    # "DiarioBitcoin": "https://www.diariobitcoin.com/feed/",   # demasiada alerta de precio
    # "CriptoNoticias": "https://news.google.com/rss/search?q=site:criptonoticias.com&hl=es-419&gl=AR&ceid=AR:es",
    # Retirados por fallos persistentes en el origen (no se pueden leer):
    # "Cointelegraph": "https://news.google.com/rss/search?q=site:es.cointelegraph.com&hl=es-419&gl=AR&ceid=AR:es",
    # "iProUP": "https://www.iproup.com/rss/blockchain",
    # "Errepar": "https://blog.errepar.com/tag/criptomonedas/feed/",
}

# Medios generalistas: solo se publica si el titular menciona algo cripto.
FILTRO_POR_MEDIO = {
    "iProfesional": ["cripto", "bitcoin", "btc", "ethereum", "blockchain",
                     "stablecoin", "usdt", "billetera virtual", "tokeniz"],
}

# Los feeds cuyo nombre empieza asi se consideran contenido propio y pueden
# revisarse aparte, con mas frecuencia (ver el workflow "propios").
PREFIJO_PROPIO = "Cripto Contador"

ZONA = ZoneInfo("America/Argentina/Buenos_Aires")

# Horario de silencio: no se publica entre estas horas (acumula y sale despues).
SILENCIO_DESDE = 0         # 00:00
SILENCIO_HASTA = 8         # 08:00

HORA_PRECIOS = 9           # mensaje con cotizaciones (None para desactivar)
HORA_RESUMEN = 21          # resumen de titulares del dia (None para desactivar)

# Mensaje del sponsor: horas del dia en que se publica (lista vacia = desactivado)
HORAS_SPONSOR = [13, 19]
# Imagen del sponsor (dejar "" para enviarlo como texto simple).
# Podes subir el archivo a este mismo repositorio y usar la direccion "raw".
IMAGEN_SPONSOR = ""
MENSAJE_SPONSOR = (
    "<b>Operá en Bitunix</b>\n\n"
    "Es el exchange que uso a diario para operar futuros y spot. "
    "Si querés abrir cuenta, podés hacerlo desde acá con el código "
    "<b>CRIPTOCONTADOR</b>:\n\n"
    "https://www.bitunix.com/register?vipCode=CRIPTOCONTADOR\n\n"
    "<i>Enlace de referido. Operar con apalancamiento implica riesgo de "
    "perder el capital invertido.</i>"
)

MAX_POR_EJECUCION = 15     # tope de mensajes por ronda (evita inundar el canal)
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


def sanear_xml(respuesta) -> str:
    """Reintento para feeds mal formados: decodifica, quita la declaracion de
    codificacion (que suele venir mal) y borra caracteres invalidos."""
    texto = respuesta.text
    texto = re.sub(r"^\s*<\?xml[^>]*\?>", "", texto)
    texto = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", texto)
    texto = re.sub(r"&(?!(?:[a-zA-Z][a-zA-Z0-9]*|#[0-9]+|#x[0-9a-fA-F]+);)", "&amp;", texto)
    return texto.lstrip()


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


def hoy() -> str:
    return datetime.now(ZONA).strftime("%Y-%m-%d")


def en_silencio(ahora: datetime) -> bool:
    if SILENCIO_DESDE == SILENCIO_HASTA:
        return False
    if SILENCIO_DESDE < SILENCIO_HASTA:
        return SILENCIO_DESDE <= ahora.hour < SILENCIO_HASTA
    return ahora.hour >= SILENCIO_DESDE or ahora.hour < SILENCIO_HASTA


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


def interesa(medio: str, entrada) -> bool:
    titulo = limpiar(entrada.get("title", "")).lower()
    especifico = FILTRO_POR_MEDIO.get(medio)
    if especifico:
        return any(p.lower() in titulo for p in especifico)
    if not PALABRAS_CLAVE:
        return True
    return any(p.lower() in titulo for p in PALABRAS_CLAVE)


def enviar_foto(imagen: str, pie: str) -> bool:
    """Envia una imagen con texto al pie. Si falla, avisa y devuelve False."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
            json={"chat_id": CHAT_ID, "photo": imagen, "caption": pie,
                  "parse_mode": "HTML"},
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"  ! error de red al enviar la imagen: {e}")
        return False
    if not r.ok:
        print(f"  ! Telegram rechazo la imagen {r.status_code}: {r.text[:200]}")
        return False
    return True


def enviar_sponsor(estado: dict, ahora: datetime) -> None:
    """Mensaje del sponsor, una vez por cada hora configurada.
    Si una tanda falla, se recupera en la siguiente del mismo dia."""
    pendientes = [h for h in HORAS_SPONSOR if ahora.hour >= h]
    if not pendientes:
        return
    hora_objetivo = max(pendientes)
    marca = f"{hoy()}-{hora_objetivo}"
    if estado.get("sponsor_enviado") == marca:
        return
    if IMAGEN_SPONSOR:
        ok = enviar_foto(IMAGEN_SPONSOR, MENSAJE_SPONSOR)
        if not ok:                      # si la imagen falla, mandamos el texto
            ok = enviar(MENSAJE_SPONSOR)
    else:
        ok = enviar(MENSAJE_SPONSOR)

    if ok:
        estado["sponsor_enviado"] = marca
        print("Enviado el mensaje del sponsor.")


def enviar_precios(estado: dict) -> None:
    """Cotizaciones de BTC y ETH, una vez por dia."""
    if HORA_PRECIOS is None or estado.get("precios_enviado") == hoy():
        return
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin,ethereum", "vs_currencies": "usd",
                    "include_24hr_change": "true"},
            timeout=30,
        )
        r.raise_for_status()
        datos = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  ! no se pudieron obtener precios: {e}")
        return

    lineas = ["<b>Cotizaciones de hoy</b>", ""]
    for clave, nombre in (("bitcoin", "Bitcoin (BTC)"), ("ethereum", "Ethereum (ETH)")):
        info = datos.get(clave)
        if not info:
            continue
        precio = info.get("usd", 0)
        cambio = info.get("usd_24h_change", 0) or 0
        signo = "+" if cambio >= 0 else ""
        lineas.append(f"{nombre}: USD {precio:,.0f} ({signo}{cambio:.2f}% en 24h)")

    if len(lineas) <= 2:
        return
    lineas.append("")
    lineas.append("<i>Datos: CoinGecko</i>")
    if enviar("\n".join(lineas)):
        estado["precios_enviado"] = hoy()
        print("Enviadas las cotizaciones del dia.")


def enviar_resumen(estado: dict) -> None:
    """Resumen de titulares publicados durante el dia."""
    if HORA_RESUMEN is None or estado.get("resumen_enviado") == hoy():
        return
    titulos = estado.get("resumen", {}).get("titulos", [])
    if not titulos:
        estado["resumen_enviado"] = hoy()
        return

    lineas = [f"<b>Resumen del dia — {len(titulos)} noticias</b>", ""]
    for t in titulos[:25]:
        lineas.append(f"• {html.escape(t)}")
    if len(titulos) > 25:
        lineas.append(f"\n<i>y {len(titulos) - 25} mas</i>")

    if enviar("\n".join(lineas)):
        estado["resumen_enviado"] = hoy()
        print(f"Enviado el resumen con {len(titulos)} titulares.")


def anotar_en_resumen(estado: dict, titulo: str) -> None:
    resumen = estado.setdefault("resumen", {"fecha": hoy(), "titulos": []})
    if resumen.get("fecha") != hoy():
        resumen["fecha"] = hoy()
        resumen["titulos"] = []
    resumen["titulos"].append(titulo)


def main() -> int:
    ahora = datetime.now(ZONA)
    estado = cargar_estado()

    # Modo "solo propios": revisa unicamente el blog y YouTube, sin mensajes fijos.
    solo_propios = os.environ.get("SOLO_PROPIOS") == "1"
    if solo_propios:
        feeds = {k: v for k, v in FEEDS.items() if k.startswith(PREFIJO_PROPIO)}
        print("Modo contenido propio.")
    else:
        feeds = FEEDS
        # Tareas de horario fijo. Usamos ">=" para que, si una tanda falla,
        # el mensaje salga igual en la siguiente del mismo dia.
        if HORA_PRECIOS is not None and ahora.hour >= HORA_PRECIOS:
            enviar_precios(estado)
        if HORA_RESUMEN is not None and ahora.hour >= HORA_RESUMEN:
            enviar_resumen(estado)
        enviar_sponsor(estado, ahora)

    if en_silencio(ahora):
        guardar_estado(estado)
        print(f"Silencio nocturno ({ahora:%H:%M}): las noticias quedan en cola.")
        return 0

    vistos = set(estado["vistos"])
    primera_vez = not estado["inicializado"]

    nuevas = []
    NAVEGADOR = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/rss+xml, application/xml, text/xml, text/html, */*",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    }

    for medio, url in feeds.items():
        print(f"Leyendo {medio}...")
        try:
            respuesta = requests.get(url, headers=NAVEGADOR, timeout=30)
            respuesta.raise_for_status()
            feed = feedparser.parse(respuesta.content)
        except requests.RequestException as e:
            print(f"  ! no se pudo descargar: {e}")
            continue
        if feed.bozo and not feed.entries:
            # Segundo intento: algunos feeds traen caracteres que rompen el XML.
            feed = feedparser.parse(sanear_xml(respuesta))
        if not feed.entries:
            print(f"  ! no se pudo leer: {feed.get('bozo_exception')}")
            continue
        for entrada in feed.entries[:40]:
            clave = entrada.get("id") or entrada.get("link")
            if not clave or clave in vistos:
                continue
            vistos.add(clave)
            if interesa(medio, entrada):
                nuevas.append((medio, entrada, clave))
            else:
                # Descartada por el filtro: no volver a evaluarla.
                estado["vistos"].append(clave)

    if primera_vez:
        # En el primer arranque no publicamos el historico entero.
        estado["vistos"].extend(clave for _, _, clave in nuevas)
        estado["inicializado"] = True
        guardar_estado(estado)
        print(f"Primera ejecucion: {len(nuevas)} noticias marcadas como vistas, sin publicar.")
        return 0

    print(f"{len(nuevas)} noticias nuevas")
    enviadas = 0
    for medio, entrada, clave in nuevas[:MAX_POR_EJECUCION]:
        titulo = limpiar(entrada.get("title", ""))
        if enviar(construir_mensaje(medio, entrada)):
            enviadas += 1
            estado["vistos"].append(clave)
            anotar_en_resumen(estado, titulo)
            print(f"  -> {titulo[:70]}")
        time.sleep(SEGUNDOS_ENTRE_MENSAJES)

    guardar_estado(estado)
    pendientes = max(0, len(nuevas) - enviadas)
    print(f"Publicadas {enviadas} noticias. Quedan {pendientes} en cola.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
