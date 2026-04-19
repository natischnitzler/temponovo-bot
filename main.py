import os
import xmlrpc.client
import json
import re
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ODOO_URL  = os.environ.get("ODOO_URL",  "https://temponovo.odoo.com")
ODOO_DB   = os.environ.get("ODOO_DB",   "cmcorpcl-temponovo-main-24490235")
ODOO_USER = os.environ.get("ODOO_USER", "")
ODOO_PASS = os.environ.get("ODOO_PASS", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WA_NUMBER   = os.environ.get("TWILIO_WA_NUMBER", "whatsapp:+14155238886")


def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def buscar_productos(termino: str) -> list:
    uid, models = odoo_connect()
    t = termino.strip()
    resultados = models.execute_kw(
        ODOO_DB, uid, ODOO_PASS,
        "product.template", "search_read",
        [[["active", "=", True], "|", ["name", "ilike", t], ["default_code", "ilike", t]]],
        {"fields": ["name", "default_code", "list_price", "qty_available"], "limit": 20}
    )
    return [
        {
            "nombre": p["name"],
            "codigo": p.get("default_code") or "—",
            "precio": p["list_price"],
            "stock": int(p.get("qty_available", 0)),
        }
        for p in resultados
    ]


RUIDO = {
    "casio","maxell","hay","tienen","quiero","ver","busco","buscar","necesito",
    "dame","muestrame","de","del","la","las","los","el","un","una","unos","unas",
    "que","con","para","por","en","y","o","a","me","stock","precio","disponible",
    "disponibles","cuantos","cuanto","cual","tienes","tiene","tenemos","modelo",
    "modelos","producto","productos","mostrar","puedes","puedo","saber","si","no",
    "reloj","relojes","pila","pilas","calculadora","calculadoras"
}

def limpiar_termino(texto: str) -> str:
    t = texto.lower()
    t = re.sub(r"[áàä]", "a", t)
    t = re.sub(r"[éèë]", "e", t)
    t = re.sub(r"[íìï]", "i", t)
    t = re.sub(r"[óòö]", "o", t)
    t = re.sub(r"[úùü]", "u", t)
    t = re.sub(r"[^a-z0-9\s\-]", " ", t)

    palabras = [p for p in t.split() if len(p) > 1 and p not in RUIDO]

    if not palabras:
        # Si todo era ruido, usar la primera palabra sustantiva del texto original
        todas = [p for p in t.split() if len(p) > 2]
        return todas[0] if todas else texto.strip()

    # Priorizar códigos/modelos con números o guiones
    codigos = [p for p in palabras if re.search(r"[\d\-]", p)]
    if codigos:
        return codigos[0]

    # Desplurizar
    palabra = palabras[0]
    if palabra.endswith("es") and len(palabra) > 4:
        palabra = palabra[:-2]
    elif palabra.endswith("s") and len(palabra) > 3:
        palabra = palabra[:-1]

    return palabra

def formatear_para_whatsapp(productos: list, termino: str) -> str:
    if not productos:
        return f"No encontré productos para *{termino}*. Intenta con otro término, por ejemplo: _F-91_, _MR-27_, _W-800_, _AA_."

    lineas = [f"Encontré *{len(productos)} producto{'s' if len(productos) > 1 else ''}* para _{termino}_:\n"]
    for p in productos[:10]:  # máx 10 en WhatsApp
        stock_txt = f"{p['stock']} en stock" if p["stock"] > 0 else "Sin stock"
        precio = "$" + f"{int(p['precio']):,}".replace(",", ".")
        lineas.append(f"• *{p['nombre']}*\n  Cód: {p['codigo']} | {precio} | {stock_txt}")

    if len(productos) > 10:
        lineas.append(f"\n_...y {len(productos) - 10} más. Refina tu búsqueda para ver menos resultados._")

    return "\n".join(lineas)


class StockRequest(BaseModel):
    producto: str


@app.post("/stock")
def consultar_stock(req: StockRequest):
    try:
        productos = buscar_productos(req.producto)
        return {"productos": productos}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/whatsapp")
async def whatsapp_webhook(
    Body: str = Form(...),
    From: str = Form(...),
):
    try:
        texto = Body.strip()
        termino = limpiar_termino(texto)
        productos = buscar_productos(termino)
        respuesta = formatear_para_whatsapp(productos, termino)
    except Exception as e:
        respuesta = "Hubo un error consultando el sistema. Intenta de nuevo en un momento."

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{respuesta}</Message>
</Response>"""
    return PlainTextResponse(content=twiml, media_type="application/xml")


@app.get("/health")
def health():
    return {"status": "ok", "odoo_url": ODOO_URL, "odoo_db": ODOO_DB, "odoo_user": ODOO_USER}
