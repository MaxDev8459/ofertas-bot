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
from email.utils import parsedate_to_datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

RAIZ = Path(__file__).parent
ESTADO = RAIZ / "estado"
VISTOS = ESTADO / "vistos.json"
OFERTAS = ESTADO / "ofertas.jsonl"      # historial: cada oferta nueva, una vez
RECIENTES = ESTADO / "recientes.json"   # ofertas recientes con vigencia y grados al día (lo lee el resumen)
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
            "publicada": None,
        }
        try:
            oferta["publicada"] = parsedate_to_datetime(item.findtext("pubDate")).astimezone(TZ).isoformat()
        except Exception:
            pass
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


def detalle(link):
    """Abre la página de la oferta: ¿ya venció? y grados actuales. None si no se pudo leer."""
    try:
        req = urllib.request.Request(link, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            p = r.read().decode("utf8", "replace")
    except Exception:
        return None
    # El primer bloque de datos de la página es el de la propia oferta.
    vencida = re.search(r'"isExpired":(true|false)', p)
    temp = re.search(r'"temperature":(-?[\d.]+)', p)
    return {"vencida": vencida is not None and vencida.group(1) == "true",
            "temp": round(float(temp.group(1))) if temp else None}


def pagina_vigentes(recientes, categorias, ahora):
    """Markdown con todas las ofertas vigentes, por categoría y de más a menos grados."""
    def hace(iso):
        if not iso:
            return "?"
        h = (ahora - datetime.fromisoformat(iso)).total_seconds() / 3600
        return f"{h:.0f} h" if h >= 1 else f"{h * 60:.0f} min"

    vigentes = [r for r in recientes.values() if not r["vencida"]]
    lineas = [
        "# 🛒 Ofertas vigentes",
        "",
        f"Actualizado: **{ahora:%d/%m/%Y %H:%M}** (hora de México) · {len(vigentes)} ofertas · "
        "publicadas en las últimas 48 h y confirmadas como vigentes en Promodescuentos.",
        "",
        "Se actualiza sola cada 30 min. Los grados (°) son los votos de la comunidad: más alto = mejor oferta.",
    ]
    for cat in categorias:
        grupo = sorted((r for r in vigentes if r["categoria"] == cat["nombre"]),
                       key=lambda r: r.get("temp") or 0, reverse=True)
        lineas += ["", f"## {cat['emoji']} {cat['nombre']} ({len(grupo)})", ""]
        if not grupo:
            lineas.append("_Sin ofertas vigentes por ahora._")
            continue
        lineas += ["| Precio | Tienda | Oferta | ° | Publicada |", "|---|---|---|---|---|"]
        for r in grupo:
            titulo = re.sub(r"^[^:]{1,30}:\s*", "", r["titulo"]).replace("|", "/")[:90]
            lineas.append(f"| **{r['precio'] or '?'}** | {r['tienda'] or '?'} | [{titulo}]({r['link']}) "
                          f"| {r.get('temp') if r.get('temp') is not None else '?'} | hace {hace(r.get('publicada'))} |")
    return "\n".join(lineas) + "\n"


def main():
    cfg = tomllib.loads((RAIZ / "config.toml").read_text())
    categorias = cfg["categoria"]
    ahora = datetime.now(TZ)
    inicio, fin = cfg["horas_silencio"]
    silencioso = ahora.hour >= inicio or ahora.hour < fin
    corte = ahora - timedelta(hours=cfg["max_horas_publicada"])

    primera_vez = not VISTOS.exists()
    vistos = {} if primera_vez else json.loads(VISTOS.read_text())
    sin_recientes = not RECIENTES.exists()  # p.ej. al estrenar esta versión: llena la lista sin avisar
    recientes = {} if sin_recientes else json.loads(RECIENTES.read_text())

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

    # 1) Ofertas de tus categorías publicadas hace poco → lista de recientes.
    nuevas = []
    for o in ofertas.values():
        if o["publicada"] and datetime.fromisoformat(o["publicada"]) < corte:
            continue  # vieja: ni se considera
        cat = clasificar(o["titulo"], categorias)
        if cat is None:
            continue
        if o["id"] not in vistos:
            vistos[o["id"]] = {"visto": ahora.isoformat(), "alertado": False}
            nuevas.append(o["id"])
        r = recientes.setdefault(o["id"], {**o, "categoria": cat["nombre"], "emoji": cat["emoji"],
                                           "prioridad": cat["prioridad"], "vencida": False})
        if o["temp"] is not None:
            r["temp"] = o["temp"]

    # 2) Revisa en su página cada reciente que siga vigente (vencida y grados al día).
    recientes = {k: v for k, v in recientes.items()
                 if not v.get("publicada") or datetime.fromisoformat(v["publicada"]) >= corte}
    revisar = [r for r in recientes.values() if not r["vencida"]][: cfg["max_paginas_por_corrida"]]
    for r in revisar:
        d = detalle(r["link"])
        if d:
            r["vencida"] = d["vencida"]
            if d["temp"] is not None:
                r["temp"] = d["temp"]
            r["revisada"] = ahora.isoformat()
        time.sleep(0.3)  # sin saturar a Promodescuentos

    # 3) Alertas (solo de ofertas vigentes).
    alertas = []
    if not (primera_vez or sin_recientes):
        for id_, r in recientes.items():
            if r["vencida"] or vistos.get(id_, {}).get("alertado"):
                continue
            cat = next(c for c in categorias if c["nombre"] == r["categoria"])
            motivo = None
            if re.search(cfg["error_precio"], r["titulo"].lower()):
                motivo = "🚨 <b>Posible error de precio</b>"
            elif r["prioridad"] == "alta" and id_ in nuevas:
                motivo = "⭐ <b>Oferta nueva (prioridad alta)</b>"
            elif r["prioridad"] != "baja" and (r.get("temp") or 0) >= cfg["umbral_hot"]:
                motivo = "🔥 <b>Oferta caliente</b>"
            if motivo:
                alertas.append((id_, r, cat, motivo))

    for id_, r, cat, motivo in alertas[: cfg["max_alertas_por_corrida"]]:
        enviar(mensaje(r, cat, motivo, cfg), silencioso)
        vistos.setdefault(id_, {"visto": ahora.isoformat()})["alertado"] = True
    sobrantes = len(alertas) - cfg["max_alertas_por_corrida"]
    if sobrantes > 0:
        enviar(f"➕ Hay {sobrantes} ofertas más; van en el próximo resumen.", silencioso)

    if primera_vez:
        vigentes = sum(not r["vencida"] for r in recientes.values())
        enviar(f"🤖 Vigilante activo. Hay {vigentes} ofertas vigentes de tus categorías publicadas en las "
               f"últimas {cfg['max_horas_publicada']} h; desde ahora te aviso solo de las nuevas.")

    # Olvida lo que ya es viejo para que el archivo no crezca sin límite.
    limite = (ahora - timedelta(days=cfg["dias_recordar"])).isoformat()
    vistos = {k: v for k, v in vistos.items() if v["visto"] >= limite}

    vigentes = [r for r in recientes.values() if not r["vencida"]]
    print(f"{len(ofertas)} leídas · {len(recientes)} recientes ({len(vigentes)} vigentes) · "
          f"{len(nuevas)} nuevas · {len(revisar)} páginas revisadas · {len(alertas)} alertas")
    if PRUEBA:
        for r in sorted(vigentes, key=lambda r: r.get("publicada") or "", reverse=True):
            print(f"  [{r['prioridad']:6}] {r['categoria']:24} {r['precio']:>9} {str(r.get('temp')):>5}°"
                  f"  {(r.get('publicada') or '')[:16]}  {r['titulo'][:55]}")
        return
    ESTADO.mkdir(exist_ok=True)
    VISTOS.write_text(json.dumps(vistos, ensure_ascii=False, indent=0))
    RECIENTES.write_text(json.dumps(recientes, ensure_ascii=False, indent=1))
    (RAIZ / "VIGENTES.md").write_text(pagina_vigentes(recientes, categorias, ahora))
    with OFERTAS.open("a") as f:  # historial completo (para comparar precios más adelante)
        for id_ in nuevas:
            f.write(json.dumps({"fecha": ahora.isoformat(), **recientes[id_]}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
