#!/usr/bin/env python3
"""Vigilante de ofertas: lee Promodescuentos, filtra por categorías y avisa por Telegram.

No usa IA (0 tokens). Lo corre GitHub Actions cada 30 min.
Uso local:  python3 vigilante.py --prueba   (no manda nada ni guarda estado)
"""
import json
import os
import re
import sys
import time
import tomllib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

RAIZ = Path(__file__).parent
ESTADO = RAIZ / "estado"
VISTOS = ESTADO / "vistos.json"
OFERTAS = ESTADO / "ofertas.jsonl"
TZ = ZoneInfo("America/Mexico_City")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
PRUEBA = "--prueba" in sys.argv


def leer_feed(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        raiz = ET.fromstring(r.read())
    for item in raiz.iter("item"):
        titulo = (item.findtext("title") or "").strip()
        temp = None
        m = re.match(r"^(-?\d+)°\s*-\s*", titulo)
        if m:  # el feed "hot" antepone los grados al título
            temp = int(m.group(1))
            titulo = titulo[m.end():]
        oferta = {
            "id": item.findtext("guid") or item.findtext("link") or titulo,
            "titulo": titulo,
            "link": (item.findtext("link") or "").strip(),
            "tienda": "",
            "precio": "",
            "temp": temp,
        }
        for el in item:
            if el.tag.endswith("merchant"):
                oferta["tienda"] = el.get("name", "")
                oferta["precio"] = el.get("price", "")
        yield oferta


def clasificar(titulo, categorias):
    t = titulo.lower()
    for cat in categorias:
        if re.search(cat["incluye"], t) and not (cat.get("excluye") and re.search(cat["excluye"], t)):
            return cat
    return None


def enviar(texto, silencioso=False):
    if PRUEBA:
        print("---- TELEGRAM", "(silencioso)" if silencioso else "", "\n" + texto)
        return
    datos = urllib.parse.urlencode({
        "chat_id": os.environ["TELEGRAM_CHAT_ID"],
        "text": texto,
        "parse_mode": "HTML",
        "disable_notification": "true" if silencioso else "false",
    }).encode()
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage"
    urllib.request.urlopen(url, datos, timeout=30)
    time.sleep(1)  # Telegram limita mensajes por segundo


def mensaje(oferta, cat, motivo, cfg):
    lineas = [f"{motivo}", f"{cat['emoji']} <b>{escape(cat['nombre'])}</b>"]
    precio = " · ".join(x for x in [oferta["precio"], oferta["tienda"]] if x)
    if precio:
        lineas.append(f"💲 <b>{escape(precio)}</b>")
    lineas.append(escape(oferta["titulo"]))
    if oferta.get("temp") is not None:
        lineas.append(f"🌡️ {oferta['temp']}° en Promodescuentos")
    if cat["nombre"] == "Laptop" and re.search(cfg["reacondicionado"], oferta["titulo"].lower()):
        lineas.append("♻️ Reacondicionada: revisa garantía y reputación del vendedor")
    lineas.append(oferta["link"])
    return "\n".join(lineas)


def main():
    cfg = tomllib.loads((RAIZ / "config.toml").read_text())
    categorias = cfg["categoria"]
    ahora = datetime.now(TZ)
    inicio, fin = cfg["horas_silencio"]
    silencioso = ahora.hour >= inicio or ahora.hour < fin

    primera_vez = not VISTOS.exists()
    vistos = {} if primera_vez else json.loads(VISTOS.read_text())

    # Junta las ofertas de todos los feeds; si una aparece en varios, se queda con los grados.
    ofertas = {}
    for url in cfg["feeds"]:
        try:
            for o in leer_feed(url):
                previa = ofertas.get(o["id"])
                if previa is None or (o["temp"] is not None and previa["temp"] is None):
                    ofertas[o["id"]] = o
        except Exception as e:  # un feed caído no detiene a los demás
            print(f"⚠️ No se pudo leer {url}: {e}", file=sys.stderr)

    alertas, nuevas = [], []
    for o in ofertas.values():
        cat = clasificar(o["titulo"], categorias)
        if cat is None:
            continue
        estado = vistos.get(o["id"])
        es_nueva = estado is None
        if es_nueva:
            estado = {"visto": ahora.isoformat(), "alertado": False}
            vistos[o["id"]] = estado
            nuevas.append({"fecha": ahora.isoformat(), "categoria": cat["nombre"], "prioridad": cat["prioridad"], **o})
        if estado["alertado"] or primera_vez:
            continue

        motivo = None
        if re.search(cfg["error_precio"], o["titulo"].lower()):
            motivo = "🚨 <b>Posible error de precio</b>"
        elif cat["prioridad"] == "alta" and es_nueva:
            motivo = "⭐ <b>Oferta nueva (prioridad alta)</b>"
        elif cat["prioridad"] != "baja" and (o["temp"] or 0) >= cfg["umbral_hot"]:
            motivo = "🔥 <b>Oferta caliente</b>"
        if motivo:
            alertas.append((o, cat, motivo))

    for o, cat, motivo in alertas[: cfg["max_alertas_por_corrida"]]:
        enviar(mensaje(o, cat, motivo, cfg), silencioso)
        vistos[o["id"]]["alertado"] = True
    sobrantes = len(alertas) - cfg["max_alertas_por_corrida"]
    if sobrantes > 0:
        enviar(f"➕ Hay {sobrantes} ofertas más; van en el próximo resumen.", silencioso)

    if primera_vez:
        enviar(f"🤖 Vigilante activo. Encontré {len(nuevas)} ofertas recientes en tus categorías; "
               "desde ahora te aviso solo de las nuevas.")

    # Olvida lo que ya es viejo para que el archivo no crezca sin límite.
    limite = (ahora - timedelta(days=cfg["dias_recordar"])).isoformat()
    vistos = {k: v for k, v in vistos.items() if v["visto"] >= limite}

    print(f"{len(ofertas)} ofertas leídas · {len(nuevas)} nuevas en tus categorías · {len(alertas)} alertas")
    if PRUEBA:
        for n in nuevas:
            print(f"  [{n['prioridad']:6}] {n['categoria']:24} {n['precio']:>9}  {n['titulo'][:70]}")
        return
    ESTADO.mkdir(exist_ok=True)
    VISTOS.write_text(json.dumps(vistos, ensure_ascii=False, indent=0))
    with OFERTAS.open("a") as f:
        for n in nuevas:
            f.write(json.dumps(n, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
