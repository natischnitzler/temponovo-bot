import os
import xmlrpc.client
import re
from fastapi import FastAPI, HTTPException, Request
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

sesiones = {}


def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def normalizar_rut(rut: str) -> str:
    r = re.sub(r"[.\s]", "", rut.strip()).upper().lstrip("0") or "0"
    if "-" in r:
        return r
    return r[:-1] + "-" + r[-1] if len(r) >= 2 else r

def es_rut(texto: str) -> bool:
    t = texto.strip().replace(" ", "").replace(".", "")
    return bool(re.match(r"^0?\d{6,8}[-]?[\dkK]$", t))

def buscar_cliente_por_rut(rut_normalizado: str) -> dict:
    uid, models = odoo_connect()
    digitos = re.sub(r"[^0-9kK]", "", rut_normalizado).upper()
    partners = models.execute_kw(
        ODOO_DB, uid, ODOO_PASS,
        "res.partner", "search_read",
        [[["vat", "like", digitos[:7]], ["is_company", "=", True], ["active", "=", True]]],
        {"fields": ["id", "name", "vat"], "limit": 5}
    )
    for p in partners:
        vat_digits = re.sub(r"[^0-9kK]", "", (p.get("vat") or "").upper())
        if vat_digits == digitos:
            return {"encontrado": True, "id": p["id"], "nombre": p["name"]}
    return {"encontrado": False}

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
}

def normalizar_texto(texto: str) -> str:
    t = texto.lower()
    for a, b in [("á","a"),("é","e"),("í","i"),("ó","o"),("ú","u"),("ü","u"),("ñ","n")]:
        t = t.replace(a, b)
    return re.sub(r"[^a-z0-9\s\-]", " ", t)

def limpiar_termino(texto: str) -> str:
    t = normalizar_texto(texto)
    palabras = [p for p in t.split() if len(p) > 1 and p not in RUIDO]
    if not palabras:
        todas = [p for p in t.split() if len(p) > 2]
        return todas[0] if todas else texto.strip()
    codigos = [p for p in palabras if re.search(r"[\d\-]", p)]
    if codigos:
        return codigos[0]
    palabra = palabras[0]
    if palabra.endswith("es") and len(palabra) > 4:
        palabra = palabra[:-2]
    elif palabra.endswith("s") and len(palabra) > 3:
        palabra = palabra[:-1]
    return palabra

def formatear_wa(productos: list, termino: str) -> str:
    if not productos:
        return (
            f"No encontre productos para *{termino}*.\n\n"
            "Intenta con terminos como:\n"
            "- _F-91_, _W-800_, _AE-1200_ (relojes)\n"
            "- _calculadora_, _MR-27_ (calculadoras)\n"
            "- _AA_, _AAA_, _CR2032_ (pilas)"
        )
    lineas = [f"Encontre *{len(productos)} producto{'s' if len(productos) > 1 else ''}* para _{termino}_:\n"]
    for p in productos[:10]:
        stock_txt = f"{p['stock']} en stock" if p["stock"] > 0 else "Sin stock"
        precio = "$" + f"{int(p['precio']):,}".replace(",", ".")
        lineas.append(f"- *{p['nombre']}*\n  Cod: {p['codigo']} | {precio} | {stock_txt}")
    if len(productos) > 10:
        lineas.append(f"\n_{len(productos)-10} productos mas. Refina tu busqueda para ver menos._")
    return "\n".join(lineas)

def menu_autenticado(nombre: str) -> str:
    return (
        f"Con que quieres continuar, *{nombre}*?\n\n"
        "- Escribe un producto para ver stock\n"
        "- Escribe *mi deuda* para ver tus facturas pendientes"
    )

BIENVENIDA = (
    "Hola! Bienvenido a *Temponovo*.\n\n"
    "- Para consultar *stock* escribe el producto que buscas\n"
    "  _ej: calculadoras, pilas AA, F-91_\n\n"
    "- Para ver tu *deuda o hacer un pedido* escribe tu RUT\n"
    "  _ej: 12.345.678-9_"
)

SALUDOS = {"hola","hi","hello","buenas","buenos","buen","hey","ola","saludos"}
AYUDA   = {"ayuda","help","menu","opciones","inicio","start"}
DEUDA   = {"deuda","factura","facturas","debo","cobro","cuenta","saldo","pendiente","pendientes"}


class StockRequest(BaseModel):
    producto: str


@app.post("/stock")
def consultar_stock(req: StockRequest):
    try:
        return {"productos": buscar_productos(req.producto)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    body = form.get("Body", "").strip()
    numero = form.get("From", "").strip()

    body_norm = normalizar_texto(body)
    palabras = set(body_norm.split())
    sesion = sesiones.get(numero, {})

    # Saludo
    if palabras & SALUDOS and len(body.split()) <= 4:
        if sesion.get("nombre"):
            respuesta = (
                f"Hola de nuevo, *{sesion['nombre']}*!\n\n"
                + menu_autenticado(sesion["nombre"]).split("\n\n", 1)[1]
            )
        else:
            respuesta = BIENVENIDA

    # Ayuda / menú
    elif palabras & AYUDA:
        respuesta = BIENVENIDA

    # RUT → autenticación
    elif es_rut(body):
        rut_norm = normalizar_rut(body)
        try:
            cliente = buscar_cliente_por_rut(rut_norm)
            if cliente["encontrado"]:
                sesiones[numero] = {"partner_id": cliente["id"], "nombre": cliente["nombre"]}
                respuesta = (
                    f"Hola, *{cliente['nombre']}*!\n\n"
                    + menu_autenticado(cliente["nombre"]).split("\n\n", 1)[1]
                )
            else:
                respuesta = (
                    f"No encontre un cliente con el RUT *{rut_norm}*.\n"
                    "Verifica el numero o contacta a tu vendedor."
                )
        except Exception:
            respuesta = "Error al consultar el sistema. Intenta de nuevo."

    # Deuda (requiere autenticación)
    elif palabras & DEUDA:
        if not sesion.get("partner_id"):
            respuesta = "Para ver tu deuda primero necesito identificarte.\nEscribe tu *RUT* y te busco en el sistema."
        else:
            respuesta = "Funcion de deuda proximamente disponible."

    # Búsqueda de producto
    else:
        try:
            termino = limpiar_termino(body)
            productos = buscar_productos(termino)
            respuesta = formatear_wa(productos, termino)
        except Exception:
            respuesta = "Hubo un error consultando el sistema. Intenta de nuevo en un momento."

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{respuesta}</Message>
</Response>"""
    return PlainTextResponse(content=twiml, media_type="application/xml")


@app.get("/health")
def health():
    return {"status": "ok"}
