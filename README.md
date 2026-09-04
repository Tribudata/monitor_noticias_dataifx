# Monitor Macro y Empresas — dataiFX

Recoge cada 30 minutos los titulares de cuatro secciones de dataifx.com
—Macro Colombia, Macro Internacional, Empresas Colombia y Empresas
internacionales— y los publica en una portada de cuatro columnas con los
15 titulares más recientes de cada una.

```
.github/workflows/actualizar-noticias.yml   cron + commit automático
scraper/scrape.py                           extracción y fusión con el histórico
data/noticias.json                          archivo que consume la página
index.html                                  portada (GitHub Pages)
requirements.txt
```

## Montaje

1. Repositorio nuevo llamado `monitor_noticias_dataifx`, rama `main`.
   Cree las carpetas con **Add file → Create new file** escribiendo la ruta
   completa (`.github/workflows/actualizar-noticias.yml`, `scraper/scrape.py`,
   `data/noticias.json`), o clone el repo y empuje la estructura desde su equipo.
2. **Settings → Actions → General → Workflow permissions**: *Read and write permissions*.
3. **Settings → Pages**: *Deploy from a branch*, rama `main`, carpeta `/ (root)`.
4. **Actions → Actualizar dataiFX → Run workflow**.

Si le pone otro nombre al repositorio, cambie `FUENTE_JSON` en `index.html`.

## Detalles

- dataiFX es una aplicación Angular renderizada en el servidor. Cada nota es un
  `mat-card` con un `a.post-title` y uno o varios `mat-chip` con el nombre de la
  sección.
- El filtro se hace por el texto del chip, no por los parámetros de la URL,
  porque el listado se arma en el navegador y la página puede devolver
  tarjetas de otras secciones. Así el resultado es correcto aunque el filtrado
  del sitio cambie.
- Una nota con dos chips aparece en las dos columnas, igual que en el sitio.
- El scraper guarda hasta 45 titulares por sección (`MAX_POR_SECCION`);
  la página muestra 15 (`POR_COLUMNA` en `index.html`).
- Si una sección falla pero otra responde, la corrida guarda lo que consiguió.

## Prueba local

```bash
pip install -r requirements.txt
python scraper/scrape.py
python -m http.server 8000
```
