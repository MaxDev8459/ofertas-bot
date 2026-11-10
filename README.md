# ofertas-bot

Vigilante de ofertas de cómputo para México. Cada 30 min lee [Promodescuentos](https://www.promodescuentos.com),
filtra por categorías y avisa por Telegram. No usa IA.

- `config.toml` — categorías, prioridades, palabras clave y umbrales.
- `vigilante.py` — el script (`python3 vigilante.py --prueba` para probar sin enviar nada).
- `estado/` — ofertas ya vistas e historial (lo actualiza el propio bot).

Prioridades:
- **alta** — avisa cada oferta nueva.
- **normal** — avisa cuando la oferta pasa de `umbral_hot` grados.
- **baja** — solo aparece en el resumen.
- Posible **error de precio** — avisa siempre.

Secrets necesarios en GitHub: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
