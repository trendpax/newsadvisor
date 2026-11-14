# newsadvisor
News Advisor (TrendPax): instaladores y actualizaciones / installers and updates

- Descargas / downloads: [Releases](https://github.com/trendpax/newsadvisor/releases/latest) · https://trendpax.com
- Novedades / news: https://t.me/trendpax

## Registro compartido de «Reacciones pasadas» / shared “Past reactions” record
`colector/reacciones.py`, ejecutado por GitHub Actions cada hora (`.github/workflows/reacciones.yml`), mide cómo se movieron
los pares en la ventana de reacción histórica de cada noticia de alto impacto y publica en la rama `datos` el fichero
`reacciones.json`, que News Advisor descarga. Solo contiene resultados derivados por publicación (pips); cada serie va
como un hash corto de su título. También publica `equivalencias` (id de serie de biquote → hash), para reconocer la misma
serie cuando el calendario la titula de otra forma. Sin velas ni datos de calendario de terceros. Market data by biquote.

`colector/reacciones.py` (run by GitHub Actions every hour) measures how pairs moved in the historical reaction window of
each high-impact release and publishes `reacciones.json` on the `datos` branch, which News Advisor downloads. It only holds
derived per-release results (pips); each series is a short hash of its title. It also publishes `equivalencias` (biquote
series id → hash) to recognise the same series under a different calendar title. No candles or third-party calendar data.
Market data by biquote.

Estimación estadística y hechos pasados; no es asesoramiento ni una señal de inversión. / Statistical estimate and past
facts; not advice or an investment signal.
