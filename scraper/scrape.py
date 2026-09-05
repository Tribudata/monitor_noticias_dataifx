#!/usr/bin/env python3
"""
Extrae los titulares de dataifx.com para cuatro secciones:
Macro Colombia, Macro Internacional, Empresas Colombia y Empresas
internacionales, y los guarda en data/noticias.json.

Pensado para ejecutarse desde GitHub Actions con un cron.
El JSON acumula histórico; la página muestra los 15 más recientes de cada una.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
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

# --- Reescritura de titulares -------------------------------------------
# No se publica el titular literal de La República: se guarda como
# referencia y se muestra una redacción propia enlazada al original.
API_MODELO = "gemini-3.5-flash-lite"
API_URL = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{API_MODELO}:generateContent")
API_CLAVE = os.environ.get("GEMINI_API_KEY", "")
LOTE = 5         # titulares por llamada
REINTENTOS = 4   # ante 429/503, que son transitorios

INSTRUCCION = """Reformula cada titular de prensa económica colombiana con
palabras distintas, conservando exactamente el mismo significado.

Reglas estrictas:
- No agregues ningún dato, cifra, nombre o matiz que no esté en el original.
- No omitas cifras ni nombres que sí estén.
- Nada de adjetivos valorativos, interpretaciones ni opiniones.
- Máximo 110 caracteres. Español de Colombia. Sin comillas ni punto final.
- Si un titular no se puede reformular sin cambiar el sentido, devuélvelo igual.

Responde ÚNICAMENTE con un arreglo JSON de cadenas, en el mismo orden y con
la misma cantidad de elementos que recibiste. Sin explicaciones ni markdown."""

SALIDA = Path(__file__).resolve().parent.parent / "data" / "noticias.json"
BOGOTA = timezone(timedelta(hours=-5))

AGENTE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

ESPERA_MS = 20000      # espera máxima a que aparezcan las tarjetas
DESPLAZAMIENTOS = 20   # tope de desplazamientos por sección
MINIMO_TARJETAS = 20   # tarjetas DE LA SECCIÓN con las que se da por satisfecho


def limpiar(texto: str) -> str:
    return re.sub(r"\s+", " ", (texto or "").replace("\xa0", " ")).strip()


def chips_de(tarjeta) -> list:
    return [limpiar(c.get_text()) for c in tarjeta.select("mat-chip h6, mat-chip")]


# Cuenta, dentro de la página ya cargada, las tarjetas que llevan el chip
# de la sección. Se ejecuta en el navegador para no traerse todo el HTML.
CONTAR_JS = """
(etiqueta) => Array.from(document.querySelectorAll('mat-card')).filter(c =>
  Array.from(c.querySelectorAll('mat-chip h6, mat-chip'))
       .some(h => (h.textContent || '').trim().toLowerCase() === etiqueta)
).length
"""


def render(pagina, url: str, etiqueta: str) -> str:
    """Abre la URL, baja hasta juntar suficientes notas de la sección y
    devuelve el HTML renderizado.

    dataiFX es una aplicación Angular que arma el listado en el cliente: una
    descarga simple devuelve un cascarón sin tarjetas. Por eso se usa un
    navegador sin interfaz, que además completa la cadena TLS incompleta
    del sitio.

    El conteo es de las tarjetas de ESTA sección, no del total: el listado
    mezcla secciones, y las menos frecuentes —Macro Internacional -- quedan
    muy abajo. Contando el total, el scroll se detenía antes de alcanzarlas.
    """
    # Un parámetro extra evita que Angular reutilice la vista anterior
    # cuando solo cambian los parámetros de consulta.
    separador = "&" if "?" in url else "?"
    pagina.goto(f"{url}{separador}_t={int(time.time())}",
                wait_until="domcontentloaded", timeout=ESPERA_MS)
    pagina.wait_for_timeout(1500)

    try:
        pagina.wait_for_selector("a.post-title", timeout=ESPERA_MS)
    except PlaywrightError:
        print(f"  aviso: no aparecieron tarjetas en {url}", file=sys.stderr)
        return pagina.content()

    previas = 0
    estancado = 0
    for _ in range(DESPLAZAMIENTOS):
        propias = pagina.evaluate(CONTAR_JS, etiqueta)
        if propias >= MINIMO_TARJETAS:
            break

        totales = pagina.locator("a.post-title").count()
        # Si dos desplazamientos seguidos no cargan nada nuevo, se acabó el
        # listado y seguir bajando solo alarga la corrida.
        estancado = estancado + 1 if totales == previas else 0
        if estancado >= 2:
            break
        previas = totales

        pagina.mouse.wheel(0, 4000)
        pagina.wait_for_timeout(1200)

    print(f"  {etiqueta}: {pagina.evaluate(CONTAR_JS, etiqueta)} tarjetas "
          f"tras el desplazamiento", file=sys.stderr)
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


def adaptar(titulares: list) -> list:
    """Devuelve una redacción propia de cada titular, o [] si no se pudo.

    Se llama solo con los titulares nuevos de la corrida, así que son unos
    pocos por hora. Si falla, el llamador conserva los que ya tenía y vuelve
    a intentar en la siguiente ejecución.
    """
    if not titulares:
        return []
    if not API_CLAVE:
        print("  falta GEMINI_API_KEY: no se adaptan titulares", file=sys.stderr)
        return []

    salida = []
    for i in range(0, len(titulares), LOTE):
        trozo = titulares[i:i + LOTE]
        cuerpo = {
            "system_instruction": {"parts": [{"text": INSTRUCCION}]},
            "contents": [{
                "role": "user",
                "parts": [{"text": json.dumps(trozo, ensure_ascii=False)}],
            }],
            "generationConfig": {
                "temperature": 0.4,
                "responseMimeType": "application/json",
            },
        }
        adaptados = None
        for intento in range(1, REINTENTOS + 1):
            try:
                r = requests.post(
                    API_URL,
                    headers={
                        "x-goog-api-key": API_CLAVE,
                        "content-type": "application/json",
                    },
                    json=cuerpo,
                    timeout=90,
                )
                # 429 (cuota) y 5xx (saturación) son transitorios: se reintenta.
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
                r.raise_for_status()

                partes = r.json()["candidates"][0]["content"]["parts"]
                texto = "".join(x.get("text", "") for x in partes)
                texto = re.sub(r"^```(?:json)?|```$", "", texto.strip()).strip()
                adaptados = json.loads(texto)
                break

            except requests.HTTPError as e:
                codigo = e.response.status_code if e.response is not None else 0
                if codigo not in (429,) and codigo < 500:
                    print(f"  no se pudieron adaptar los titulares ({e})",
                          file=sys.stderr)
                    return []
                if intento == REINTENTOS:
                    print(f"  no se pudieron adaptar los titulares tras "
                          f"{REINTENTOS} intentos ({e})", file=sys.stderr)
                    return []
                espera = 2 ** intento          # 2, 4, 8 segundos
                print(f"  {e}; reintento {intento}/{REINTENTOS - 1} "
                      f"en {espera}s", file=sys.stderr)
                time.sleep(espera)

            except (requests.RequestException, json.JSONDecodeError,
                    KeyError, IndexError) as e:
                print(f"  no se pudieron adaptar los titulares ({e})",
                      file=sys.stderr)
                return []

        if adaptados is None:
            return []

        if not isinstance(adaptados, list) or len(adaptados) != len(trozo):
            print("  respuesta inesperada al adaptar titulares", file=sys.stderr)
            return []

        salida.extend(limpiar(str(a)) for a in adaptados)

    return salida


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

        # Solo entran los titulares que aún no están en el histórico.
        pendientes = [i for i in nuevo.get(seccion, []) if i["url"] not in por_url]
        adaptados = adaptar([i["titulo"] for i in pendientes])

        if pendientes and not adaptados:
            print(f"  {seccion}: {len(pendientes)} titulares quedan para la "
                  f"próxima corrida (sin adaptación)", file=sys.stderr)

        for item, propio in zip(pendientes, adaptados):
            por_url[item["url"]] = {
                **item,
                "titulo": propio,                 # redacción propia, es la que se publica
                "titulo_fuente": item["titulo"],  # original, solo como referencia
                "capturado": ahora,
            }
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
        for seccion, cfg in SECCIONES.items():
            # Pestaña nueva por sección: reutilizarla dejaba el listado de la
            # sección anterior, porque la aplicación no rehace la vista
            # cuando solo cambian los parámetros de la URL.
            pagina = contexto.new_page()
            try:
                html = render(pagina, cfg["url"], cfg["etiqueta"])
            except PlaywrightError as e:
                print(f"{seccion}: no se pudo abrir ({e})", file=sys.stderr)
                nuevo[seccion] = []
                fallos += 1
                continue
            finally:
                pagina.close()

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
