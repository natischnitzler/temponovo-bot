import os
import xmlrpc.client
import re
import httpx
from datetime import date, datetime
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ODOO_URL  = os.environ.get("ODOO_URL",  "https://temponovo.odoo.com")
ODOO_DB   = os.environ.get("ODOO_DB",   "cmcorpcl-temponovo-main-24490235")
ODOO_USER = os.environ.get("ODOO_USER", "")
ODOO_PASS = os.environ.get("ODOO_PASS", "")
CATALOGOS_API_URL = "https://api.github.com/repos/natischnitzler/temponovo_catalogos/releases/tags/catalogos-latest"

sesiones = {}
_catalogos_cache = None


# ── Odoo ──────────────────────────────────────────────────────
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
    productos = [
        {"nombre": p["name"], "codigo": p.get("default_code") or "—",
         "precio": p["list_price"], "stock": int(p.get("qty_available", 0))}
        for p in resultados
    ]
    return sorted(productos, key=lambda x: x["stock"], reverse=True)

def consultar_deuda(partner_id: int) -> dict:
    uid, models = odoo_connect()
    facturas = models.execute_kw(
        ODOO_DB, uid, ODOO_PASS,
        "account.move", "search_read",
        [[["partner_id", "=", partner_id], ["move_type", "=", "out_invoice"],
          ["payment_state", "in", ["not_paid", "partial"]], ["state", "=", "posted"]]],
        {"fields": ["name", "invoice_date_due", "amount_residual"], "limit": 20}
    )
    hoy = date.today().isoformat()
    vencidas, pendientes = [], []
    for f in facturas:
        venc = f.get("invoice_date_due") or ""
        item = {"factura": f["name"], "monto": round(f["amount_residual"]), "vencimiento": venc}
        (vencidas if venc and venc < hoy else pendientes).append(item)
    return {"vencidas": vencidas, "pendientes": pendientes}

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


# ── Helpers ───────────────────────────────────────────────────
def normalizar_rut(rut: str) -> str:
    r = re.sub(r"[.\s]", "", rut.strip()).upper().lstrip("0") or "0"
    if "-" in r: return r
    return r[:-1] + "-" + r[-1] if len(r) >= 2 else r

def es_rut(texto: str) -> bool:
    t = texto.strip().replace(" ", "").replace(".", "")
    return bool(re.match(r"^0?\d{6,8}[-]?[\dkK]$", t))

def normalizar_texto(texto: str) -> str:
    t = texto.lower()
    for a, b in [("á","a"),("é","e"),("í","i"),("ó","o"),("ú","u"),("ü","u"),("ñ","n")]:
        t = t.replace(a, b)
    return re.sub(r"[^a-z0-9\s\-]", " ", t)

def fmt_monto(n): return "$" + f"{n:,}".replace(",", ".")

def fmt_fecha(f):
    try: return datetime.strptime(f, "%Y-%m-%d").strftime("%d/%m/%Y")
    except: return f or "—"

def stock_emoji(s):
    if s == 0:  return "⚫"
    if s < 10:  return "🔴"
    if s <= 20: return "🟡"
    return "🟢"

def stock_txt(s):
    if s == 0:    return "Sin stock"
    if s > 100:   return "100+"
    return str(s)

ALIAS = {"gshock": "g-shock", "g shock": "g-shock", "protreck": "pro trek", "protrek": "pro trek"}
RUIDO = {"casio","maxell","hay","tienen","quiero","ver","busco","buscar","necesito","dame",
         "muestrame","de","del","la","las","los","el","un","una","unos","unas","que","con",
         "para","por","en","y","o","a","me","stock","precio","disponible","disponibles",
         "cuantos","cuanto","cual","tienes","tiene","tenemos","modelo","modelos","producto",
         "productos","mostrar","puedes","puedo","saber","si","no"}

def limpiar_termino(texto: str) -> str:
    t = normalizar_texto(texto)
    for k, v in ALIAS.items():
        if k in t: return v
    palabras = [p for p in t.split() if len(p) > 1 and p not in RUIDO]
    if not palabras:
        todas = [p for p in t.split() if len(p) > 2]
        return todas[0] if todas else texto.strip()
    codigos = [p for p in palabras if re.search(r"[\d\-]", p)]
    if codigos: return codigos[0]
    palabra = palabras[0]
    if palabra.endswith("es") and len(palabra) > 4: palabra = palabra[:-2]
    elif palabra.endswith("s") and len(palabra) > 3: palabra = palabra[:-1]
    return palabra

def formatear_wa(productos: list, termino: str) -> str:
    if not productos:
        return (f"😕 No encontre productos para *{termino}*.\n\n"
                "Intenta con otro termino:\n• _F-91_, _W-800_\n• _calculadora_, _MR-27_\n• _AA_, _AAA_")
    lineas = [f"📦 *{len(productos)} resultado{'s' if len(productos)>1 else ''}* para _{termino}_:\n"]
    for p in productos[:10]:
        lineas.append(f"{stock_emoji(p['stock'])} {p['codigo']} | {fmt_monto(int(p['precio']))} | {stock_txt(p['stock'])}")
    if len(productos) > 10:
        lineas.append(f"\n_...y {len(productos)-10} mas. Refina tu busqueda._")
    return "\n".join(lineas)

def formatear_deuda(deuda: dict, nombre: str) -> str:
    v, p = deuda["vencidas"], deuda["pendientes"]
    if not v and not p: return f"✅ *{nombre}* no tiene facturas pendientes. Todo al dia!"
    total = sum(f["monto"] for f in v + p)
    lineas = [f"💰 *Total adeudado: {fmt_monto(total)}*\n"]
    if v:
        lineas.append(f"🔴 *Vencidas* — {fmt_monto(sum(f['monto'] for f in v))}")
        for f in v: lineas.append(f"  • {f['factura']} | {fmt_monto(f['monto'])} | {fmt_fecha(f['vencimiento'])}")
    if p:
        if v: lineas.append("")
        lineas.append(f"🟡 *Por vencer* — {fmt_monto(sum(f['monto'] for f in p))}")
        for f in p: lineas.append(f"  • {f['factura']} | {fmt_monto(f['monto'])} | {fmt_fecha(f['vencimiento'])}")
    return "\n".join(lineas)


# ── Catálogos ─────────────────────────────────────────────────
async def cargar_catalogos():
    global _catalogos_cache
    if _catalogos_cache:
        return _catalogos_cache
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            r = await client.get(CATALOGOS_API_URL, headers={"Accept": "application/vnd.github+json"})
            release = r.json()
            asset = next((a for a in release.get("assets", []) if a["name"] == "catalogos_links.json"), None)
            if not asset:
                print("Asset catalogos_links.json no encontrado")
                return {}
            r2 = await client.get(asset["browser_download_url"], headers={"Accept": "application/octet-stream"})
            _catalogos_cache = r2.json()
            print(f"Catalogos cargados: {len(_catalogos_cache)}")
            return _catalogos_cache
    except Exception as e:
        print(f"Error cargando catalogos: {e}")
        return {}

NUM_EMOJIS = [f"{i}." for i in range(1, 30)]

NOMBRES_LEGIBLES = {
    "Catalogo_Relojes_Casio_Clasico_A-L.pdf":    "Relojes Casio Clásico A-L",
    "Catalogo_Relojes_Casio_Clasico_M_W.pdf":    "Relojes Casio Clásico M-W",
    "Catalogo_Relojes_Casio_Despertadores.pdf":  "Relojes Casio Despertadores",
    "Catalogo_Relojes_Casio_EdificeyDuro.pdf":   "Relojes Casio Edifice & Duro",
    "Catalogo_Relojes_Casio_Gshock.pdf":         "Relojes Casio G-Shock",
    "Catalogo_Relojes_Casio_Murales_y_Crono.pdf":"Relojes Casio Murales",
    "Catalogo_Relojes_Casio_Protreck.pdf":       "Relojes Casio Pro Trek",
    "Catalogo_Relojes_QQ_Alfabeto.pdf":          "Relojes QQ (alfabético)",
    "Catalogo_Relojes_QQ_Familia.pdf":           "Relojes QQ (familia)",
    "Catalogo_Relojes_Guess.pdf":                "Relojes Guess",
    "Catalogo_Relojes_Suizos.pdf":               "Relojes Suizos",
    "Catalogo_RelojesEconomicos.pdf":            "Relojes Económicos",
    "Catalogo_Relojes_Timesonic.pdf":            "Relojes Timesonic",
    "Catalogo_Calculadoras_Casio.pdf":           "Calculadoras Casio",
    "Catalogo_Calculadoras_Economicas.pdf":      "Calculadoras Económicas",
    "Catalogo_Correas_de_Cuero.pdf":             "Correas de Cuero",
    "Catalogo_Correas_PU.pdf":                   "Correas PU",
    "Catalogo_Estuches_Joyas.pdf":               "Estuches Joyas",
    "Catalogo_LimpiezaJoyas.pdf":                "Limpieza Joyas",
    "Catalogo_Pilas_De_Reloj.pdf":               "Pilas de Reloj",
    "Catalogo_Encendedores_Zippo.pdf":           "Encendedores Zippo (alfabético)",
    "Catalogo_Encendedores_Zippo_Familia.pdf":   "Encendedores Zippo (familia)",
}

NUMEROS_CATALOGOS = {}

def generar_menu(catalogos: dict) -> str:
    global NUMEROS_CATALOGOS
    NUMEROS_CATALOGOS = {}
    lineas = ["📂 *Catálogos disponibles:*\n"]
    for i, archivo in enumerate(catalogos.keys()):
        emoji = NUM_EMOJIS[i] if i < len(NUM_EMOJIS) else f"{i+1}."
        nombre = NOMBRES_LEGIBLES.get(archivo, archivo.replace("_", " ").replace(".pdf", ""))
        lineas.append(f"{emoji} {nombre}")
        NUMEROS_CATALOGOS[str(i + 1)] = archivo
    lineas.append("\nEscribe el *número* del catálogo que quieres recibir.")
    return "\n".join(lineas)

NOMBRES_CATALOGOS_ALIAS = {
    "clasico a-l": "Catalogo_Relojes_Casio_Clasico_A-L.pdf",
    "clasico m-w": "Catalogo_Relojes_Casio_Clasico_M_W.pdf",
    "despertadores": "Catalogo_Relojes_Casio_Despertadores.pdf",
    "edifice": "Catalogo_Relojes_Casio_EdificeyDuro.pdf",
    "gshock": "Catalogo_Relojes_Casio_Gshock.pdf",
    "g-shock": "Catalogo_Relojes_Casio_Gshock.pdf",
    "g shock": "Catalogo_Relojes_Casio_Gshock.pdf",
    "murales": "Catalogo_Relojes_Casio_Murales_y_Crono.pdf",
    "protreck": "Catalogo_Relojes_Casio_Protreck.pdf",
    "qq": "Catalogo_Relojes_QQ_Familia.pdf",
    "guess": "Catalogo_Relojes_Guess.pdf",
    "suizos": "Catalogo_Relojes_Suizos.pdf",
    "economicos": "Catalogo_RelojesEconomicos.pdf",
    "timesonic": "Catalogo_Relojes_Timesonic.pdf",
    "calculadoras casio": "Catalogo_Calculadoras_Casio.pdf",
    "calculadoras economicas": "Catalogo_Calculadoras_Economicas.pdf",
    "correas cuero": "Catalogo_Correas_de_Cuero.pdf",
    "correas pu": "Catalogo_Correas_PU.pdf",
    "estuches": "Catalogo_Estuches_Joyas.pdf",
    "limpieza": "Catalogo_LimpiezaJoyas.pdf",
    "pilas": "Catalogo_Pilas_De_Reloj.pdf",
    "zippo": "Catalogo_Encendedores_Zippo_Familia.pdf",
}


# ── Constantes de chat ────────────────────────────────────────
BIENVENIDA = (
    "👋 Hola! Bienvenido a *Temponovo*!\n\n"
    "Soy Temo, tu asistente 😊\n\n"
    "Puedes preguntarme por:\n"
    "📦 *Stock y precios* — escribe el producto o codigo\n"
    "   _ej: F-91, calculadora, pila AA_\n\n"
    "💳 *Tu cuenta* — escribe tu RUT para ver tus facturas\n"
    "   _ej: 12.345.678-9_\n"
    "   Luego escribe _cuenta_, _deuda_ o _facturas_\n\n"
    "📂 *Catalogos* — escribe _catalogos_ para ver la lista\n\n"
    "En que te puedo ayudar? 🙌"
)

SALUDOS  = {"hola","hi","hello","buenas","buenos","buen","hey","ola","saludos"}
AYUDA    = {"ayuda","help","menu","opciones","inicio","start"}
DEUDA    = {"deuda","cuenta","facturas","factura","saldo","cobro","debo","pendiente","pendientes"}
CATALOGO = {"catalogo","catalogos","pdf"}


# ── Endpoints ─────────────────────────────────────────────────
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
    form    = await request.form()
    body    = form.get("Body", "").strip()
    numero  = form.get("From", "").strip()

    body_norm = normalizar_texto(body)
    palabras  = set(body_norm.split())
    sesion    = sesiones.get(numero, {})
    media_url = None

    # Saludo
    if palabras & SALUDOS and len(body.split()) <= 4:
        if sesion.get("nombre"):
            respuesta = (f"👋 Hola de nuevo, *{sesion['nombre']}*!\n\n"
                         "En que te puedo ayudar?\n"
                         "📦 Escribe un producto para ver stock\n"
                         "💳 Escribe _cuenta_, _deuda_ o _facturas_\n"
                         "📂 Escribe _catalogos_ para ver la lista")
        else:
            respuesta = BIENVENIDA

    # Ayuda
    elif palabras & AYUDA:
        respuesta = BIENVENIDA

    # RUT
    elif es_rut(body):
        rut_norm = normalizar_rut(body)
        try:
            cliente = buscar_cliente_por_rut(rut_norm)
            if cliente["encontrado"]:
                sesiones[numero] = {"partner_id": cliente["id"], "nombre": cliente["nombre"]}
                respuesta = (f"✅ Hola, *{cliente['nombre']}*! Ya te tengo en el sistema 🎉\n\n"
                             "Con que quieres continuar?\n"
                             "📦 Escribe un producto para ver stock\n"
                             "💳 Escribe _cuenta_, _deuda_ o _facturas_\n"
                             "📂 Escribe _catalogos_ para ver la lista")
            else:
                respuesta = (f"❌ No encontre un cliente con el RUT *{rut_norm}*.\n\n"
                             "Verifica el numero o contacta a tu vendedor.")
        except Exception:
            respuesta = "⚠️ Error al consultar el sistema. Intenta de nuevo."

    # Deuda
    elif palabras & DEUDA:
        if not sesion.get("partner_id"):
            respuesta = ("🔐 Para ver tu cuenta primero necesito identificarte.\n\n"
                         "Escribe tu *RUT* y te busco en el sistema.\n_ej: 12.345.678-9_")
        else:
            try:
                deuda = consultar_deuda(sesion["partner_id"])
                respuesta = formatear_deuda(deuda, sesion["nombre"])
            except Exception:
                respuesta = "⚠️ Error al consultar tus facturas. Intenta de nuevo."

    # Catálogos — mostrar menú
    elif palabras & CATALOGO and len(body.split()) <= 2:
        catalogos = await cargar_catalogos()
        if not catalogos:
            respuesta = "⚠️ No se pudieron cargar los catalogos. Intenta de nuevo."
        else:
            sesiones[numero] = {**sesion, "esperando_catalogo": True}
            respuesta = generar_menu(catalogos)

    # Selección de catálogo
    elif sesion.get("esperando_catalogo"):
        catalogos = await cargar_catalogos()
        archivo = None
        num = body_norm.strip()

        if num in NUMEROS_CATALOGOS:
            archivo = NUMEROS_CATALOGOS[num]
        else:
            for key, val in NOMBRES_CATALOGOS_ALIAS.items():
                if key in body_norm:
                    archivo = val
                    break

        if archivo and archivo in catalogos:
            url = catalogos[archivo]
            sesiones[numero] = {**sesion, "esperando_catalogo": False}
            respuesta = "📎 Aqui va tu catalogo:"
            media_url = url
        elif archivo:
            respuesta = "⚠️ Ese catalogo no esta disponible en este momento."
        else:
            respuesta = ("No entendi cual catalogo quieres.\n\n"
                         "Escribe el *número* de la lista o escribe _catalogos_ para verla de nuevo.")

    # Búsqueda de producto
    else:
        try:
            termino = limpiar_termino(body)
            productos = buscar_productos(termino)
            respuesta = formatear_wa(productos, termino)
        except Exception:
            respuesta = "⚠️ Hubo un error. Intenta de nuevo en un momento."

    def xe(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    if media_url:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>
        <Body>{xe(respuesta)}</Body>
        <Media>{media_url}</Media>
    </Message>
</Response>"""
    else:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{xe(respuesta)}</Message>
</Response>"""

    return PlainTextResponse(content=twiml, media_type="application/xml")


@app.get("/health")
def health():
    return {"status": "ok"}
