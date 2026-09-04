#!/usr/bin/env python3
"""
Extrae los titulares de dataifx.com para cuatro secciones:
Macro Colombia, Macro Internacional, Empresas Colombia y Empresas
internacionales, y los guarda en data/noticias.json.

Pensado para ejecutarse desde GitHub Actions con un cron.
El JSON acumula histórico; la página muestra los 15 más recientes de cada una.
"""

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Error as PlaywrightError

BASE = "https://www.dataifx.com/"

# etiqueta: el texto del chip que dataiFX pone en cada tarjeta.
# El filtro se hace por ese chip, no por la URL, porque el listado
# se arma en el navegador y la página puede devolver más de lo pedido.
SECCIONES = {
    "Macro Colombia": {
        "url": "https://www.dataifx.com/?category=macroeconómicos&rel=macro-colombia",
        "etiqueta": "macro colombia",
    },
    "Macro Internacional": {
        "url": "https://www.dataifx.com/?category=macroeconómicos&rel=macro-internacional",
        "etiqueta": "macro internacional",
    },
    "Empresas Colombia": {
        "url": "https://www.dataifx.com/?category=empresas&rel=empresas-colombia",
        "etiqueta": "empresas colombia",
    },
    "Empresas Internacional": {
        "url": "https://www.dataifx.com/?category=empresas&rel=empresas-internacionales",
        "etiqueta": "empresas internacionales",
    },
}

MAX_POR_SECCION = 45     # histórico guardado; la página muestra 15
DIAS_RETENCION = 30

SALIDA = Path(__file__).resolve().parent.parent / "data" / "noticias.json"
BOGOTA = timezone(timedelta(hours=-5))

AGENTE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

ESPERA_MS = 20000     # espera máxima a que aparezcan las tarjetas
DESPLAZAMIENTOS = 4   # veces que se baja la página para cargar más notas
MINIMO_TARJETAS = 25  # deja de desplazarse al llegar a esta cantidad


def limpiar(texto: str) -> str:
    return re.sub(r"\s+", " ", (texto or "").replace("\xa0", " ")).strip()


def chips_de(tarjeta) -> list:
    return [limpiar(c.get_text()) for c in tarjeta.select("mat-chip h6, mat-chip")]


def render(pagina, url: str) -> str:
    """Abre la URL en un navegador real y devuelve el HTML ya renderizado.

    dataiFX es una aplicación Angular que arma el listado en el cliente: una
    descarga simple devuelve un cascarón sin tarjetas. Por eso se usa un
    navegador sin interfaz, que además completa la cadena TLS incompleta
    del sitio.
    """
    pagina.goto(url, wait_until="domcontentloaded", timeout=ESPERA_MS)

    try:
        pagina.wait_for_selector("a.post-title", timeout=ESPERA_MS)
    except PlaywrightError:
        print(f"  aviso: no aparecieron tarjetas en {url}", file=sys.stderr)
        return pagina.content()

    # El listado usa scroll infinito: hay que bajar para que cargue más.
    for _ in range(DESPLAZAMIENTOS):
        if pagina.locator("a.post-title").count() >= MINIMO_TARJETAS:
            break
        pagina.mouse.wheel(0, 4000)
        pagina.wait_for_timeout(1200)

    return pagina.content()


def extraer(html: str, etiqueta: str) -> list:
    """Recorre las tarjetas y se queda con las que llevan el chip de la sección."""
    sopa = BeautifulSoup(html, "lxml")
    items = []
    urls_vistas = set()

    for tarjeta in sopa.select("mat-card"):
        enlace = tarjeta.select_one("a.post-title")
        if not enlace:
            continue

        titulo = limpiar(enlace.get_text())
        href = enlace.get("href") or ""
        if not titulo or not href:
            continue

        etiquetas = [c.lower() for c in chips_de(tarjeta)]
        if etiqueta not in etiquetas:
            continue

        url = urljoin(BASE, href)
        if url in urls_vistas:
            continue
        urls_vistas.add(url)

        resumen = tarjeta.select_one(".post-descrip span")
        otras = [c for c in chips_de(tarjeta) if c.lower() != etiqueta]

        items.append({
            "titulo": titulo,
            "url": url,
            "resumen": limpiar(resumen.get_text()) if resumen else "",
            "autor": "",
            "subseccion": otras[0] if otras else "",
            "publicacion": "",
        })

    return items


def cargar_previo() -> dict:
    if not SALIDA.exists():
        return {}
    try:
        return json.loads(SALIDA.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def fusionar(previo: dict, nuevo: dict, ahora: str) -> dict:
    secciones_previas = previo.get("secciones", {})
    limite = datetime.fromisoformat(ahora) - timedelta(days=DIAS_RETENCION)
    salida = {}
    nuevos_totales = 0

    for seccion in SECCIONES:
        por_url = {}

        for item in secciones_previas.get(seccion, []):
            try:
                if datetime.fromisoformat(item["capturado"]) < limite:
                    continue
            except (KeyError, ValueError):
                pass
            por_url[item["url"]] = item

        for item in nuevo.get(seccion, []):
            if item["url"] in por_url:
                por_url[item["url"]]["titulo"] = item["titulo"]
            else:
                por_url[item["url"]] = {**item, "capturado": ahora}
                nuevos_totales += 1

        ordenados = sorted(
            por_url.values(),
            key=lambda i: i.get("capturado", ""),
            reverse=True,
        )
        salida[seccion] = ordenados[:MAX_POR_SECCION]

    return {
        "fuente": BASE,
        "actualizado": ahora,
        "nuevos_en_esta_corrida": nuevos_totales,
        "secciones": salida,
    }


def diagnostico(html: str, etiqueta: str) -> None:
    """Imprime qué trajo la descarga, para saber por qué no hubo coincidencias."""
    sopa = BeautifulSoup(html, "lxml")
    tarjetas = sopa.select("mat-card")
    titulos = sopa.select("a.post-title")
    chips = {limpiar(c.get_text()) for c in sopa.select("mat-chip h6")}
    print(f"  diagnóstico: {len(html)} bytes, {len(tarjetas)} mat-card, "
          f"{len(titulos)} a.post-title", file=sys.stderr)
    if chips:
        print(f"  chips presentes: {sorted(chips)[:12]}", file=sys.stderr)
    else:
        print(f"  sin chips; se buscaba '{etiqueta}'", file=sys.stderr)


def main() -> int:
    ahora = datetime.now(BOGOTA).isoformat(timespec="seconds")
    nuevo = {}
    fallos = 0

    with sync_playwright() as p:
        navegador = p.chromium.launch(args=["--disable-dev-shm-usage"])
        contexto = navegador.new_context(
            user_agent=AGENTE,
            locale="es-CO",
            ignore_https_errors=True,   # cadena TLS incompleta del sitio
            viewport={"width": 1400, "height": 1000},
        )
        pagina = contexto.new_page()

        for seccion, cfg in SECCIONES.items():
            try:
                html = render(pagina, cfg["url"])
            except PlaywrightError as e:
                print(f"{seccion}: no se pudo abrir ({e})", file=sys.stderr)
                nuevo[seccion] = []
                fallos += 1
                continue

            items = extraer(html, cfg["etiqueta"])
            if not items:
                diagnostico(html, cfg["etiqueta"])

            nuevo[seccion] = items
            print(f"{seccion}: {len(items)} titulares")

        contexto.close()
        navegador.close()

    if not any(nuevo.values()):
        print("Ninguna sección devolvió titulares: revise el diagnóstico de arriba.",
              file=sys.stderr)
        return 1

    datos = fusionar(cargar_previo(), nuevo, ahora)
    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_text(
        json.dumps(datos, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Guardado {SALIDA} · {datos['nuevos_en_esta_corrida']} titulares nuevos")
    return 1 if fallos == len(SECCIONES) else 0


if __name__ == "__main__":
    raise SystemExit(main())
