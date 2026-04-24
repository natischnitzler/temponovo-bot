import os
import xmlrpc.client
import re
import httpx
import csv
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
USUARIOS_URL = "https://raw.githubusercontent.com/natischnitzler/temponovo-bot/main/usuarios.csv"

sesiones = {}
_catalogos_cache = None
_usuarios = {}  # { "+56985495930": {"tipo": "admin", "nombre": "Natalia"} }


# ── Usuarios ──────────────────────────────────────────────────
def normalizar_numero(n: str) -> str:
    """Normaliza número a formato +56XXXXXXXXX"""
    if not n: return ""
    n = re.sub(r"[\s\-\(\)]", "", n)
    if n.startswith("09"): n = "+56" + n[1:]
    elif n.startswith("9") and len(n) == 9: n = "+56" + n
    elif n.startswith("56") and not n.startswith("+56"): n = "+" + n
    return n

async def cargar_usuarios():
    global _usuarios
    try:
        # 1. Cargar admins desde CSV
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(USUARIOS_URL)
            lineas = r.text.strip().split("\n")
            for linea in lineas[1:]:
                partes = [p.strip() for p in linea.split(",")]
                if len(partes) >= 3:
                    numero = normalizar_numero(partes[0])
                    _usuarios[numero] = {"tipo": partes[1], "nombre": partes[2]}

        # 2. Cargar vendedores desde Odoo (res.users internos con mobile)
        def _cargar_vendedores():
            uid, models = odoo_connect()
            usuarios_odoo = models.execute_kw(
                ODOO_DB, uid, ODOO_PASS,
                "res.users", "search_read",
                [[["active", "=", True], ["share", "=", False]]],
                {"fields": ["name", "partner_id"], "limit": 50}
            )
            partner_ids = [u["partner_id"][0] for u in usuarios_odoo if u.get("partner_id")]
            partners = models.execute_kw(
                ODOO_DB, uid, ODOO_PASS,
                "res.partner", "search_read",
                [[["id", "in", partner_ids]]],
                {"fields": ["id", "name", "mobile"]}
            )
            partner_map = {p["id"]: p for p in partners}
            for u in usuarios_odoo:
                pid = u["partner_id"][0] if u.get("partner_id") else None
                p = partner_map.get(pid, {})
                mobile = normalizar_numero(p.get("mobile") or "")
                if mobile and mobile not in _usuarios:
                    _usuarios[mobile] = {"tipo": "vendedor", "nombre": u["name"]}

        # 3. Cargar clientes desde Odoo (res.partner empresas con mobile)
        def _cargar_clientes():
            uid, models = odoo_connect()
            partners = models.execute_kw(
                ODOO_DB, uid, ODOO_PASS,
                "res.partner", "search_read",
                [[["is_company", "=", True], ["active", "=", True],
                  ["mobile", "!=", False], ["customer_rank", ">", 0]]],
                {"fields": ["id", "name", "vat", "mobile", "user_id"], "limit": 500}
            )
            for p in partners:
                mobile = normalizar_numero(p.get("mobile") or "")
                if mobile and mobile not in _usuarios:
                    vendedor = p["user_id"][1] if p.get("user_id") else ""
                    _usuarios[mobile] = {
                        "tipo": "cliente",
                        "nombre": p["name"],
                        "partner_id": p["id"],
                        "rut": p.get("vat", ""),
                        "vendedor": vendedor
                    }

        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _cargar_vendedores)
        await loop.run_in_executor(None, _cargar_clientes)

        print(f"Usuarios cargados: {len(_usuarios)} (admins+vendedores+clientes)")
    except Exception as e:
        print(f"Error cargando usuarios: {e}")

def get_usuario(numero_wa: str) -> dict:
    # numero_wa viene como "whatsapp:+56985495930"
    numero = numero_wa.replace("whatsapp:", "").replace(" ", "")
    return _usuarios.get(numero, {"tipo": "publico", "nombre": ""})


@app.on_event("startup")
async def startup():
    await cargar_catalogos()
    await cargar_usuarios()
    print("Bot listo")


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
        {"fields": ["name", "default_code", "list_price", "qty_available", "incoming_qty"], "limit": 20}
    )
    productos = [
        {"nombre": p["name"], "codigo": p.get("default_code") or "—",
         "precio": p["list_price"], "stock": int(p.get("qty_available", 0)), "entrante": int(p.get("incoming_qty", 0))}
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

def buscar_cliente_por_nombre(nombre: str) -> list:
    uid, models = odoo_connect()
    partners = models.execute_kw(
        ODOO_DB, uid, ODOO_PASS,
        "res.partner", "search_read",
        [[["name", "ilike", nombre], ["is_company", "=", True], ["active", "=", True]]],
        {"fields": ["id", "name", "vat"], "limit": 5}
    )
    return [{"id": p["id"], "nombre": p["name"], "rut": p.get("vat","")} for p in partners]


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
        entrante = p.get("entrante", 0)
        entrante_txt = f" | 📥 {entrante} en camino" if entrante > 0 else ""
        lineas.append(f"{stock_emoji(p['stock'])} {p['codigo']} | {fmt_monto(int(p['precio']))} | {stock_txt(p['stock'])}{entrante_txt}")
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

def generar_menu(catalogos: dict) -> tuple[str, dict]:
    numeros = {}
    lineas = ["📂 *Catálogos disponibles:*\n"]
    for i, archivo in enumerate(catalogos.keys()):
        emoji = NUM_EMOJIS[i] if i < len(NUM_EMOJIS) else f"{i+1}."
        nombre = NOMBRES_LEGIBLES.get(archivo, archivo.replace("_", " ").replace(".pdf", ""))
        lineas.append(f"{emoji} {nombre}")
        numeros[str(i + 1)] = archivo
    lineas.append("\nEscribe el *número* del catálogo que quieres recibir.")
    return "\n".join(lineas), numeros

NOMBRES_CATALOGOS_ALIAS = {
    "gshock": "Catalogo_Relojes_Casio_Gshock.pdf",
    "g-shock": "Catalogo_Relojes_Casio_Gshock.pdf",
    "edifice": "Catalogo_Relojes_Casio_EdificeyDuro.pdf",
    "protreck": "Catalogo_Relojes_Casio_Protreck.pdf",
    "despertadores": "Catalogo_Relojes_Casio_Despertadores.pdf",
    "murales": "Catalogo_Relojes_Casio_Murales_y_Crono.pdf",
    "qq": "Catalogo_Relojes_QQ_Familia.pdf",
    "guess": "Catalogo_Relojes_Guess.pdf",
    "suizos": "Catalogo_Relojes_Suizos.pdf",
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


# ── Mensajes ──────────────────────────────────────────────────
def bienvenida_admin(nombre: str) -> str:
    return (
        f"👋 Hola, *{nombre}*!\n\n"
        "Puedes consultar:\n"
        "📦 *Stock* — escribe el producto o codigo\n"
        "💳 *Cuenta* — escribe _cuenta de [cliente]_ o el RUT\n"
        "📂 *Catalogos* — escribe _catalogo_\n\n"
        "En que te puedo ayudar?"
    )

BIENVENIDA_PUBLICA = (
    "👋 Hola! Bienvenido a *Temponovo*!\n\n"
    "Para acceder al asistente necesitas estar registrado.\n\n"
    "Contacta a Natalia para que te den acceso:\n"
    "📱 +56 9 8549 5930"
)

SALUDOS  = {"hola","hi","hello","buenas","buenos","buen","hey","ola","saludos"}
AYUDA    = {"ayuda","help","menu","opciones","inicio","start"}
DEUDA    = {"deuda","cuenta","facturas","factura","saldo","cobro","debo","pendiente","pendientes"}
CATALOGO = {"catalogo","catalogos","pdf","catalogue"}


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
    usuario   = get_usuario(numero)
    es_admin  = usuario["tipo"] in ("admin", "vendedor")

    def xe(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    # Auto-autenticar cliente si viene de Odoo
    if usuario["tipo"] == "cliente" and not sesion.get("partner_id"):
        sesiones[numero] = {
            "partner_id": usuario.get("partner_id"),
            "nombre": usuario["nombre"],
            "rut": usuario.get("rut", "")
        }
        sesion = sesiones[numero]

    # Saludo
    if palabras & SALUDOS and len(body.split()) <= 4:
        if es_admin:
            respuesta = bienvenida_admin(usuario["nombre"])
        elif usuario["tipo"] == "vendedor":
            respuesta = bienvenida_admin(usuario["nombre"])
        elif sesion.get("nombre"):
            respuesta = (f"👋 Hola de nuevo, *{sesion['nombre']}*!\n\n"
                         "En que te puedo ayudar?\n"
                         "📦 Escribe un producto para ver su stock\n"
                         "💳 Escribe _cuenta_ para ver tu deuda\n"
                         "📂 Escribe _catalogo_ para ver lista de catalogos")
        else:
            respuesta = BIENVENIDA_PUBLICA

    # Ayuda
    elif palabras & AYUDA:
        if es_admin:
            respuesta = bienvenida_admin(usuario["nombre"])
        else:
            respuesta = BIENVENIDA_PUBLICA

    # RUT
    elif es_rut(body):
        rut_norm = normalizar_rut(body)
        try:
            cliente = buscar_cliente_por_rut(rut_norm)
            if cliente["encontrado"]:
                sesiones[numero] = {**sesion, "partner_id": cliente["id"], "nombre": cliente["nombre"]}
                if es_admin:
                    # Admin ve la deuda directo
                    deuda = consultar_deuda(cliente["id"])
                    respuesta = formatear_deuda(deuda, cliente["nombre"])
                else:
                    respuesta = (f"✅ Hola, *{cliente['nombre']}*! Ya te tengo en el sistema 🎉\n\n"
                                 "Con que quieres continuar?\n"
                                 "📦 Escribe un producto para ver su stock\n"
                                 "💳 Escribe _cuenta_ para ver tu deuda\n"
                                 "📂 Escribe _catalogo_ para ver lista de catalogos")
            else:
                respuesta = f"❌ No encontre un cliente con el RUT *{rut_norm}*."
        except Exception:
            respuesta = "⚠️ Error al consultar el sistema. Intenta de nuevo."

    # Deuda / cuenta
    elif palabras & DEUDA:
        texto_sin_deuda = body_norm
        for palabra in DEUDA:
            texto_sin_deuda = texto_sin_deuda.replace(palabra, "").strip()
        texto_sin_deuda = re.sub(r"\bde\b", "", texto_sin_deuda).strip()

        if len(texto_sin_deuda) > 3 and (es_admin or not sesion.get("partner_id")):
            try:
                clientes = buscar_cliente_por_nombre(texto_sin_deuda)
                if len(clientes) == 1:
                    c = clientes[0]
                    sesiones[numero] = {**sesion, "partner_id": c["id"], "nombre": c["nombre"]}
                    deuda = consultar_deuda(c["id"])
                    deuda_txt = formatear_deuda(deuda, c["nombre"])
                    respuesta = f"✅ *{c['nombre']}*\n\n{deuda_txt}"
                elif len(clientes) > 1:
                    lista = "\n".join([f"• {c['nombre']} ({c['rut']})" for c in clientes])
                    respuesta = f"Encontre varios clientes:\n{lista}\n\nEscribe el RUT del que quieres consultar."
                else:
                    respuesta = "No encontre ese cliente. Prueba con el RUT."
            except Exception:
                respuesta = "⚠️ Error al buscar el cliente."
        elif sesion.get("partner_id"):
            try:
                deuda = consultar_deuda(sesion["partner_id"])
                respuesta = formatear_deuda(deuda, sesion.get("nombre","cliente"))
            except Exception:
                respuesta = "⚠️ Error al consultar las facturas."
        else:
            if es_admin:
                respuesta = "Escribe _cuenta de [nombre del cliente]_ o el RUT del cliente."
            else:
                respuesta = "🔐 Escribe tu *RUT* primero.\n_ej: 12.345.678-9_"

    # Catálogos
    elif palabras & CATALOGO and len(body.split()) <= 2:
        catalogos = await cargar_catalogos()
        if not catalogos:
            respuesta = "⚠️ No se pudieron cargar los catalogos. Intenta de nuevo."
        else:
            menu_txt, numeros = generar_menu(catalogos)
            sesiones[numero] = {**sesion, "esperando_catalogo": True, "menu_numeros": numeros}
            respuesta = menu_txt

    # Selección de catálogo
    elif sesion.get("esperando_catalogo"):
        catalogos = await cargar_catalogos()
        archivo = None
        num = body_norm.strip()

        if num in {"menu", "lista", "volver", "catalogos", "catalogo"}:
            menu_txt, numeros = generar_menu(catalogos)
            sesiones[numero] = {**sesion, "esperando_catalogo": True, "menu_numeros": numeros}
            respuesta = menu_txt
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{xe(respuesta)}</Message>
</Response>"""
            return PlainTextResponse(content=twiml, media_type="application/xml")

        menu_numeros = sesion.get("menu_numeros", {})
        if num in menu_numeros:
            archivo = menu_numeros[num]
        else:
            for key, val in NOMBRES_CATALOGOS_ALIAS.items():
                if key in body_norm:
                    archivo = val
                    break

        if archivo and archivo in catalogos:
            url = catalogos[archivo]
            respuesta = "📎 Aqui va tu catalogo\n\nEscribe otro número para ver más, o _menu_ para volver a la lista."
            media_url = url
        elif archivo:
            respuesta = "⚠️ Ese catalogo no esta disponible en este momento."
        else:
            sesiones[numero] = {**sesion, "esperando_catalogo": False}
            try:
                termino = limpiar_termino(body)
                productos = buscar_productos(termino)
                respuesta = formatear_wa(productos, termino)
            except Exception:
                respuesta = "⚠️ Hubo un error. Intenta de nuevo."

    # Búsqueda de producto
    else:
        try:
            termino = limpiar_termino(body)
            productos = buscar_productos(termino)
            respuesta = formatear_wa(productos, termino)
        except Exception:
            respuesta = "⚠️ Hubo un error. Intenta de nuevo en un momento."

    print(f"RESP [{usuario['tipo']}]: {respuesta[:60]}")

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

@app.get("/reload")
async def reload():
    global _usuarios
    _usuarios = {}
    await cargar_usuarios()
    return {"usuarios": len(_usuarios), "numeros": list(_usuarios.keys())}

@app.get("/usuarios")
def ver_usuarios():
    return {k: v for k, v in _usuarios.items()}
