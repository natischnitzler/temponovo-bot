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
_usuarios = {}
_stock_cache = {}    # { id: {nombre, codigo, precio, stock, entrante} }
_deuda_cache = {}    # { partner_id: {vencidas, pendientes} }
_pedidos_cache = {}  # { partner_id: [{numero, total, estado, fecha}] }
_pedidos_por_num = {}  # { "S04572": {numero, partner_id, partner_nombre, estado, fecha} }
ADMIN_KEY = os.environ.get("ADMIN_KEY", "temponovo2025")


# ── Usuarios ──────────────────────────────────────────────────
def normalizar_numero(n: str) -> str:
    """Normaliza numero a formato +56XXXXXXXXX"""
    if not n: return ""
    n = re.sub(r"[\s\-\(\)]", "", n)
    if n.startswith("09"): n = "+56" + n[1:]
    elif n.startswith("9") and len(n) == 9: n = "+56" + n
    elif n.startswith("56") and not n.startswith("+56"): n = "+" + n
    return n

async def cargar_stock_cache():
    """Carga todos los productos en cache"""
    global _stock_cache
    try:
        def _fetch():
            uid, models = odoo_connect()
            return models.execute_kw(
                ODOO_DB, uid, ODOO_PASS,
                "product.template", "search_read",
                [[["active", "=", True], ["sale_ok", "=", True]]],
                {"fields": ["name", "default_code", "list_price", "qty_available", "incoming_qty"], "limit": 2000}
            )
        import asyncio
        loop = asyncio.get_event_loop()
        productos = await loop.run_in_executor(None, _fetch)
        _stock_cache = {}
        for p in productos:
            codigo = p.get("default_code") or ""
            _stock_cache[p["id"]] = {
                "nombre": p["name"],
                "codigo": codigo or "—",
                "precio": p["list_price"],
                "stock": int(p.get("qty_available", 0)),
                "entrante": int(p.get("incoming_qty", 0)),
            }
        print(f"Stock cacheado: {len(_stock_cache)} productos")
    except Exception as e:
        print(f"Error cargando stock: {e}")

async def cargar_pedidos_cache():
    """Carga pedidos de todos los clientes"""
    global _pedidos_cache, _pedidos_por_num
    try:
        def _fetch():
            uid, models = odoo_connect()
            pedidos = models.execute_kw(
                ODOO_DB, uid, ODOO_PASS,
                "sale.order", "search_read",
                [[["state", "not in", ["cancel", "draft"]]]],
                {"fields": ["name", "partner_id", "amount_total", "tempo_delivery_state", "date_order"],
                 "limit": 10000, "order": "date_order desc"}
            )
            cache = {}
            por_num = {}
            for p in pedidos:
                pid = p["partner_id"][0] if p.get("partner_id") else None
                if not pid: continue
                estado = ESTADO_PEDIDO.get(p.get("tempo_delivery_state") or "", "—")
                fecha = p.get("date_order", "")[:10] if p.get("date_order") else "—"
                item = {
                    "numero": p["name"],
                    "total": round(p["amount_total"]),
                    "estado": estado,
                    "fecha": fecha,
                }
                if pid not in cache:
                    cache[pid] = []
                cache[pid].append(item)
                por_num[p["name"]] = {
                    "numero": p["name"],
                    "partner_id": pid,
                    "partner_nombre": p["partner_id"][1] if p.get("partner_id") else "—",
                    "estado": estado,
                    "fecha": fecha,
                }
            return cache, por_num
        import asyncio
        loop = asyncio.get_event_loop()
        _pedidos_cache, _pedidos_por_num = await loop.run_in_executor(None, _fetch)
        print(f"Pedidos cacheados: {len(_pedidos_cache)} clientes, {len(_pedidos_por_num)} pedidos")
    except Exception as e:
        print(f"Error cargando pedidos: {e}")

async def cargar_deuda_cache():
    """Carga deudas de todos los clientes activos"""
    global _deuda_cache
    try:
        def _fetch():
            uid, models = odoo_connect()
            from datetime import date
            hoy = date.today().isoformat()
            facturas = models.execute_kw(
                ODOO_DB, uid, ODOO_PASS,
                "account.move", "search_read",
                [[["move_type", "=", "out_invoice"],
                  ["payment_state", "in", ["not_paid", "partial"]],
                  ["state", "=", "posted"]]],
                {"fields": ["partner_id", "name", "invoice_date_due", "amount_residual"], "limit": 5000}
            )
            cache = {}
            for f in facturas:
                pid = f["partner_id"][0] if f.get("partner_id") else None
                if not pid: continue
                if pid not in cache:
                    cache[pid] = {"vencidas": [], "pendientes": []}
                venc = f.get("invoice_date_due") or ""
                item = {"factura": f["name"], "monto": round(f["amount_residual"]), "vencimiento": venc}
                if venc and venc < hoy:
                    cache[pid]["vencidas"].append(item)
                else:
                    cache[pid]["pendientes"].append(item)
            return cache
        import asyncio
        loop = asyncio.get_event_loop()
        _deuda_cache = await loop.run_in_executor(None, _fetch)
        print(f"Deuda cacheada: {len(_deuda_cache)} clientes")
    except Exception as e:
        print(f"Error cargando deuda: {e}")

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
                {"fields": ["id", "name", "mobile", "phone"]}
            )
            partner_map = {p["id"]: p for p in partners}
            for u in usuarios_odoo:
                pid = u["partner_id"][0] if u.get("partner_id") else None
                p = partner_map.get(pid, {})
                # Intentar mobile primero, luego phone
                mobile = normalizar_numero(p.get("mobile") or p.get("phone") or "")
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
    numero = numero_wa.replace("whatsapp:", "").replace(" ", "")
    if numero in _usuarios:
        return _usuarios[numero]
    # No está en cache — buscar en Odoo directamente
    try:
        uid, models = odoo_connect()
        # Buscar en res.partner (clientes y vendedores)
        num_limpio = numero.replace("+56", "").replace("+", "")
        partners = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "res.partner", "search_read",
            [["|", ["mobile", "like", num_limpio], ["phone", "like", num_limpio]]],
            {"fields": ["id", "name", "mobile", "phone", "customer_rank", "user_ids"], "limit": 3}
        )
        for p in partners:
            mobile = normalizar_numero(p.get("mobile") or p.get("phone") or "")
            if mobile == numero:
                # Es usuario interno (vendedor)?
                if p.get("user_ids"):
                    resultado = {"tipo": "vendedor", "nombre": p["name"]}
                elif p.get("customer_rank", 0) > 0:
                    resultado = {"tipo": "cliente", "nombre": p["name"], "partner_id": p["id"]}
                else:
                    continue
                _usuarios[numero] = resultado
                print(f"Usuario encontrado en Odoo: {numero} → {resultado}")
                return resultado
    except Exception as e:
        print(f"Error buscando usuario en Odoo: {e}")
    return {"tipo": "publico", "nombre": ""}


@app.on_event("startup")
async def startup():
    import asyncio
    await cargar_catalogos()
    await cargar_usuarios()
    await cargar_stock_cache()
    await cargar_deuda_cache()
    await cargar_pedidos_cache()
    print("Bot listo")

    async def recargar_periodico():
        from datetime import datetime, timezone, timedelta
        tz_santiago = timezone(timedelta(hours=-3))
        while True:
            ahora = datetime.now(tz_santiago)
            hora = ahora.hour
            # Esperar hasta la próxima recarga (12:00 o 18:00)
            if hora < 12:
                espera = (12 - hora) * 3600 - ahora.minute * 60 - ahora.second
            elif hora < 18:
                espera = (18 - hora) * 3600 - ahora.minute * 60 - ahora.second
            else:
                # Esperar hasta las 12 del día siguiente
                espera = (24 - hora + 12) * 3600 - ahora.minute * 60 - ahora.second
            print(f"Próxima recarga en {espera//3600}h {(espera%3600)//60}m")
            await asyncio.sleep(espera)
            global _usuarios, _stock_cache, _deuda_cache
            _usuarios = {}
            await cargar_usuarios()
            _stock_cache = {}
            await cargar_stock_cache()
            _deuda_cache = {}
            await cargar_deuda_cache()
            _pedidos_cache.clear()
            _pedidos_por_num.clear()
            await cargar_pedidos_cache()
            print(f"Cache recargado a las {datetime.now(tz_santiago).strftime('%H:%M')}")

    asyncio.create_task(recargar_periodico())


# ── Odoo ──────────────────────────────────────────────────────
def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models

def buscar_productos(termino: str) -> list:
    t = termino.strip().lower()
    if _stock_cache:
        # Buscar en cache
        resultados = [
            p for p in _stock_cache.values()
            if t in p["nombre"].lower() or t in p["codigo"].lower()
        ]
        return sorted(resultados, key=lambda x: x["stock"], reverse=True)[:20]
    # Fallback a Odoo si no hay cache
    uid, models = odoo_connect()
    resultados = models.execute_kw(
        ODOO_DB, uid, ODOO_PASS,
        "product.template", "search_read",
        [[["active", "=", True], "|", ["name", "ilike", termino], ["default_code", "ilike", termino]]],
        {"fields": ["name", "default_code", "list_price", "qty_available", "incoming_qty"], "limit": 20}
    )
    productos = [
        {"nombre": p["name"], "codigo": p.get("default_code") or "—",
         "precio": p["list_price"], "stock": int(p.get("qty_available", 0)), "entrante": int(p.get("incoming_qty", 0))}
        for p in resultados
    ]
    return sorted(productos, key=lambda x: x["stock"], reverse=True)

def consultar_deuda(partner_id: int) -> dict:
    if partner_id in _deuda_cache:
        return _deuda_cache[partner_id]
    # No está en cache = no tiene facturas pendientes
    return {"vencidas": [], "pendientes": []}

ESTADO_PEDIDO = {
    "quotation":  "⏰ Cotizacion",
    "confirmed":  "✔ Confirmado",
    "pick":       "💰 Facturado",
    "pack":       "📦 Listo para despacho",
    "delivered":  "🚚 Entregado",
    "cancel":     "❌ Cancelado",
}

def consultar_pedidos(partner_id: int, limite: int = 8) -> list:
    return _pedidos_cache.get(partner_id, [])[:limite]

def consultar_pedidos_cliente(partner_id: int) -> list:
    """Para clientes: solo los últimos 5"""
    return _pedidos_cache.get(partner_id, [])[:5]

def formatear_pedidos(pedidos: list, nombre: str) -> str:
    if not pedidos:
        return f"📋 *{nombre}* no tiene pedidos recientes."
    lineas = [f"📋 *Pedidos de {nombre}*:\n"]
    for p in pedidos:
        fecha = ""
        if p["fecha"] and p["fecha"] != "—":
            try:
                from datetime import datetime
                fecha = " | " + datetime.strptime(p["fecha"], "%Y-%m-%d").strftime("%d/%m/%Y")
            except:
                fecha = ""
        lineas.append(f"{p['estado']} {p['numero']} | {fecha.replace(' | ', '')}")
    return "\n".join(lineas)

def buscar_cliente_por_rut(rut_normalizado: str) -> dict:
    uid, models = odoo_connect()
    digitos = re.sub(r"[^0-9kK]", "", rut_normalizado).upper()
    partners = models.execute_kw(
        ODOO_DB, uid, ODOO_PASS,
        "res.partner", "search_read",
        [[["vat", "like", digitos[:7]], ["is_company", "=", True], ["active", "=", True]]],
        {"fields": ["id", "name", "vat", "parent_id"], "limit": 5}
    )
    for p in partners:
        vat_digits = re.sub(r"[^0-9kK]", "", (p.get("vat") or "").upper())
        if vat_digits == digitos:
            # Si es una dirección (tiene parent), usar el partner principal
            pid = p["parent_id"][0] if p.get("parent_id") else p["id"]
            nombre = p["parent_id"][1] if p.get("parent_id") else p["name"]
            return {"encontrado": True, "id": pid, "nombre": nombre}
    return {"encontrado": False}

def buscar_cliente_por_nombre(nombre: str, vendedor_nombre: str = "") -> list:
    """Siempre busca en Odoo directamente, no en cache"""
    try:
        uid, models = odoo_connect()
        # Buscar con el término original y también sin caracteres especiales
        terminos = [nombre]
        nombre_limpio = re.sub(r"[^a-z0-9\s]", " ", nombre).strip()
        if nombre_limpio != nombre:
            terminos.append(nombre_limpio)
        
        dominio_base = [["is_company", "=", True], ["active", "=", True]]
        if len(terminos) == 1:
            dominio = [["name", "ilike", nombre]] + dominio_base
        else:
            dominio = ["|", ["name", "ilike", terminos[0]], ["name", "ilike", terminos[1]]] + dominio_base
        
        # Si es vendedor, filtrar solo sus clientes
        if vendedor_nombre:
            usuarios = models.execute_kw(
                ODOO_DB, uid, ODOO_PASS,
                "res.users", "search_read",
                [[["name", "ilike", vendedor_nombre]]],
                {"fields": ["id", "name"], "limit": 3}
            )
            if usuarios:
                user_ids = [u["id"] for u in usuarios]
                dominio.append(["user_id", "in", user_ids])
        
        # Filtrar por vendedor si aplica
        if vendedor_nombre:
            usuarios = models.execute_kw(
                ODOO_DB, uid, ODOO_PASS,
                "res.users", "search_read",
                [[["name", "ilike", vendedor_nombre]]],
                {"fields": ["id"], "limit": 3}
            )
            if usuarios:
                user_ids = [u["id"] for u in usuarios]
                dominio = dominio + [["user_id", "in", user_ids]]

        partners = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "res.partner", "search_read",
            [dominio],
            {"fields": ["id", "name", "vat"], "limit": 5}
        )
        return [{"id": p["id"], "nombre": p["name"], "rut": p.get("vat","")} for p in partners]
    except Exception as e:
        print(f"Error buscar_cliente_por_nombre: {e}")
        return []


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
        return (f"😕 No encontré productos para *{termino}*.\n\n"
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
    if not v and not p:
        return f"✅ *{nombre}* no tiene facturas pendientes. ¡Todo al día! 🎉"
    total = sum(f["monto"] for f in v + p)
    lineas = [f"*{nombre}*\n💰 *Total deuda: {fmt_monto(total)}*\n"]
    if p:
        total_p = sum(f["monto"] for f in p)
        lineas.append(f"🟡 *Por vencer* ({len(p)} {'factura' if len(p)==1 else 'facturas'}) — {fmt_monto(total_p)}")
        for f in p[:7]:
            lineas.append(f"  {f['factura']} | {fmt_monto(f['monto'])} | {fmt_fecha(f['vencimiento'])}")
        if len(p) > 7:
            lineas.append(f"  _...y {len(p)-7} facturas mas_")
    if v:
        if p: lineas.append("")
        total_v = sum(f["monto"] for f in v)
        lineas.append(f"🔴 *Vencidas* ({len(v)} {'factura' if len(v)==1 else 'facturas'}) — {fmt_monto(total_v)}")
        for f in v[:7]:
            lineas.append(f"  {f['factura']} | {fmt_monto(f['monto'])} | {fmt_fecha(f['vencimiento'])}")
        if len(v) > 7:
            lineas.append(f"  _...y {len(v)-7} facturas mas_")
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
    lineas.append("\nEscribe el *numero* del catálogo que quieres recibir.")
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
def saludo_hora() -> str:
    from datetime import datetime, timezone, timedelta
    hora = datetime.now(timezone(timedelta(hours=-3))).hour
    if hora < 12: return "🌅 Buenos días"
    if hora < 19: return "☀️ Buenas tardes"
    return "🌙 Buenas noches"

def bienvenida_admin(nombre: str) -> str:
    return (
        f"{saludo_hora()}, *{nombre}*!\n\n"
        "Puedes consultar:\n"
        "1. 📦 *Stock* — escribe el producto o código\n"
        "2. 💳 *Cuenta* — escribe _cuenta de [cliente]_ o el RUT\n"
        "3. 📂 *Catálogos* — escribe _catálogo_\n"
        "4. 📋 *Estado de pedidos* — escribe _pedidos de [cliente]_ o numero de pedido\n\n"
        "¿En qué te puedo ayudar?"
    )

BIENVENIDA_PUBLICA = (
    "👋 Hola! Bienvenido a *Temponovo*!\n\n"
    "Para acceder al asistente necesitas estar registrado.\n\n"
    "Contacta a Natalia para que te den acceso:\n"
    "📱 +56 9 8549 5930"
)

MENU_OPCIONES = {"1": "stock", "2": "cuenta", "3": "catalogo", "4": "pedido"}

SALUDOS  = {"hola","hi","hello","buenas","buenos","buen","hey","ola","saludos"}
GRACIAS  = {"gracias","thank","thanks","dale","ok","oki","listo","perfecto","excelente","genial","bacán","bacan"}
DESPEDIDA = {"chao","adios","bye","hasta","nos","vemos","cuidate"}
AYUDA    = {"ayuda","help","menu","opciones","inicio","start"}
DEUDA    = {"deuda","cuenta","facturas","factura","saldo","cobro","debo","pendiente","pendientes"}
CATALOGO = {"catalogo","catalogos","pdf","catalogue"}
PEDIDO   = {"pedido","pedidos","orden","ordenes","compra","compras"}


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
    body       = form.get("Body", "").strip()
    numero     = form.get("From", "").strip()
    num_media  = int(form.get("NumMedia", "0") or "0")

    # Si manda media (foto, audio, video, documento)
    if num_media > 0 and not body:
        def xe_early(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        msg = "Lo siento, no puedo procesar imágenes, audios ni videos 😊\n\nPuedo ayudarte con texto:\n1. 📦 Stock\n2. 💳 Cuenta\n3. 📂 Catálogos\n4. 📋 Estado de pedidos"
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n    <Message>{xe_early(msg)}</Message>\n</Response>"""
        return PlainTextResponse(content=twiml, media_type="application/xml")

    body_norm = normalizar_texto(body)
    palabras  = set(body_norm.split())
    sesion    = sesiones.get(numero, {})
    media_url = None
    usuario   = get_usuario(numero)
    es_admin  = usuario["tipo"] in ("admin", "vendedor")

    def xe(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    # Detectar gracias y despedidas
    if palabras & GRACIAS and len(body.split()) <= 4:
        respuesta = "¡Con gusto! 😊 Escribe si necesitas algo más."
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n    <Message>{xe(respuesta)}</Message>\n</Response>"""
        return PlainTextResponse(content=twiml, media_type="application/xml")

    if palabras & DESPEDIDA and len(body.split()) <= 3:
        respuesta = f"{saludo_hora()}! Hasta pronto 👋"
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n    <Message>{xe(respuesta)}</Message>\n</Response>"""
        return PlainTextResponse(content=twiml, media_type="application/xml")

    # Selección de cliente por numero (1, 2, 3...)
    if sesion.get("clientes_lista") and body_norm.strip().isdigit():
        idx = int(body_norm.strip()) - 1
        lista = sesion["clientes_lista"]
        contexto = sesion.get("contexto_lista", "cuenta")
        if 0 <= idx < len(lista):
            c = lista[idx]
            sesiones[numero] = {**sesion, "partner_id": c["id"], "nombre": c["nombre"], "clientes_lista": None}
            if contexto == "pedidos":
                pedidos = consultar_pedidos(c["id"])
                respuesta = formatear_pedidos(pedidos, c["nombre"])
            else:
                deuda = consultar_deuda(c["id"])
                deuda_txt = formatear_deuda(deuda, c["nombre"])
                respuesta = deuda_txt
        else:
            respuesta = f"Escribe un numero entre 1 y {len(lista)}."
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n    <Message>{xe(respuesta)}</Message>\n</Response>"""
        return PlainTextResponse(content=twiml, media_type="application/xml")

    # Detectar opciones de menú 1, 2, 3
    if body_norm.strip() in MENU_OPCIONES and not sesion.get("esperando_catalogo") and not sesion.get("clientes_lista"):
        opcion = MENU_OPCIONES[body_norm.strip()]
        if opcion == "stock":
            respuesta = "📦 Escribe el producto o código que quieres consultar."
        elif opcion == "cuenta":
            if es_admin:
                respuesta = "💳 Escribe _cuenta de [nombre]_ o el RUT del cliente."
            elif sesion.get("partner_id"):
                deuda = consultar_deuda(sesion["partner_id"])
                respuesta = formatear_deuda(deuda, sesion.get("nombre", ""))
            else:
                respuesta = "💳 Escribe tu RUT para ver tu cuenta.\n_ej: 12.345.678-9_"
        elif opcion == "catalogo":
            catalogos = await cargar_catalogos()
            if catalogos:
                menu_txt, numeros = generar_menu(catalogos)
                sesiones[numero] = {**sesion, "esperando_catalogo": True, "menu_numeros": numeros}
                respuesta = menu_txt
            else:
                respuesta = "⚠️ No se pudieron cargar los catálogos."
        elif opcion == "pedido":
            if sesion.get("partner_id"):
                pedidos = consultar_pedidos(sesion["partner_id"])
                respuesta = formatear_pedidos(pedidos, sesion.get("nombre",""))
            else:
                respuesta = "Escribe _pedidos de [nombre]_ o el RUT del cliente."
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n    <Message>{xe(respuesta)}</Message>\n</Response>"""
        return PlainTextResponse(content=twiml, media_type="application/xml")

    # Auto-autenticar cliente si viene de Odoo
    if usuario["tipo"] == "cliente" and not sesion.get("partner_id"):
        sesiones[numero] = {
            "partner_id": usuario.get("partner_id"),
            "nombre": usuario["nombre"],
            "rut": usuario.get("rut", "")
        }
        sesion = sesiones[numero]

    def menu_cliente(nombre: str) -> str:
        return (f"{saludo_hora()}, *{nombre}*! 👋\n\n"
                "¿En qué te puedo ayudar?\n"
                "1. 📦 Stock — escribe el producto o código\n"
                "2. 💳 Cuenta — escribe _cuenta_\n"
                "3. 📂 Catálogos — escribe _catálogo_\n"
                "4. 📋 Estado de pedidos — escribe _pedidos_")

    # Saludo
    if palabras & SALUDOS and len(body.split()) <= 4:
        if es_admin:
            respuesta = bienvenida_admin(usuario["nombre"])
        elif usuario["tipo"] == "vendedor":
            respuesta = bienvenida_admin(usuario["nombre"])
        elif sesion.get("nombre"):
            respuesta = menu_cliente(sesion["nombre"])
        else:
            respuesta = BIENVENIDA_PUBLICA

    # Ayuda
    elif palabras & AYUDA:
        if es_admin:
            respuesta = bienvenida_admin(usuario["nombre"])
        elif sesion.get("nombre"):
            respuesta = menu_cliente(sesion["nombre"])
        else:
            respuesta = BIENVENIDA_PUBLICA

    # RUT
    elif es_rut(body):
        rut_norm = normalizar_rut(body)
        try:
            cliente = buscar_cliente_por_rut(rut_norm)
            if cliente["encontrado"]:
                sesiones[numero] = {**sesion, "partner_id": cliente["id"], "nombre": cliente["nombre"], "contexto": ""}
                if sesion.get("contexto") == "pedidos":
                    pedidos = consultar_pedidos(cliente["id"])
                    respuesta = formatear_pedidos(pedidos, cliente["nombre"])
                elif es_admin:
                    # Admin ve la deuda directo
                    deuda = consultar_deuda(cliente["id"])
                    respuesta = formatear_deuda(deuda, cliente["nombre"])
                else:
                    respuesta = (f"✅ Hola, *{cliente['nombre']}*! Ya te tengo en el sistema 🎉\n\n"
                                 "Con que quieres continuar?\n"
                                 "📦 Escribe un producto para ver su stock\n"
                                 "💳 Escribe _cuenta_ para ver tu deuda\n"
                                 "📂 Escribe _catálogo_ para ver lista de catálogos")
            else:
                respuesta = f"❌ No encontré un cliente con el RUT *{rut_norm}*."
        except Exception:
            respuesta = "⚠️ Error al consultar el sistema. Intenta de nuevo.\n\nLo sentimos, no pudimos procesar tu consulta. Contáctate con nuestra oficina y te ayudamos de inmediato\n💬 *Estrella*: +56 9 6292 9654\n🌐 www.temponovo.cl"

    # Deuda / cuenta
    elif palabras & DEUDA:
        # Primero verificar si viene un RUT en el mensaje
        rut_match = re.search(r"0?\d{6,8}[-]?[\dkK]", body.replace(".", "").replace(" ", ""))
        if rut_match:
            rut_candidato = rut_match.group()
            if es_rut(rut_candidato):
                rut_norm = normalizar_rut(rut_candidato)
                cliente = buscar_cliente_por_rut(rut_norm)
                if cliente["encontrado"]:
                    sesiones[numero] = {**sesion, "partner_id": cliente["id"], "nombre": cliente["nombre"]}
                    deuda = consultar_deuda(cliente["id"])
                    deuda_txt = formatear_deuda(deuda, cliente["nombre"])
                    respuesta = f"✅ *{cliente['nombre']}*\n\n{deuda_txt}"
                else:
                    respuesta = f"❌ No encontré cliente con RUT *{rut_norm}*."
                twiml = f"""<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n    <Message>{xe(respuesta)}</Message>\n</Response>"""
                return PlainTextResponse(content=twiml, media_type="application/xml")

        # Sacar frases completas primero, luego palabras sueltas
        texto_sin_deuda = body_norm
        for frase in ["cuenta de", "deuda de", "factura de", "saldo de"]:
            texto_sin_deuda = texto_sin_deuda.replace(frase, "").strip()
        for palabra in sorted(DEUDA, key=len, reverse=True):
            texto_sin_deuda = texto_sin_deuda.replace(palabra, "").strip()
        texto_sin_deuda = re.sub(r"^[^a-z0-9]+", "", texto_sin_deuda)
        texto_sin_deuda = re.sub(r"^(de|del|la|el)\s+", "", texto_sin_deuda).strip()

        if len(texto_sin_deuda) >= 2:
            try:
                vendedor_filtro = usuario["nombre"] if usuario["tipo"] == "vendedor" else ""
                print(f"Buscando cliente: [{texto_sin_deuda}] vendedor: [{vendedor_filtro}]")
                clientes = buscar_cliente_por_nombre(texto_sin_deuda, vendedor_filtro)
                print(f"Encontrados: {len(clientes)} - {clientes}")
                if len(clientes) == 1:
                    c = clientes[0]
                    # Limpiar sesión anterior y cargar nuevo cliente
                    sesiones[numero] = {**sesion, "partner_id": c["id"], "nombre": c["nombre"]}
                    deuda = consultar_deuda(c["id"])
                    deuda_txt = formatear_deuda(deuda, c["nombre"])
                    respuesta = deuda_txt
                elif len(clientes) > 1:
                    lista5 = clientes[:5]
                    opciones = "\n".join([f"{i+1}. {c['nombre']}" for i, c in enumerate(lista5)])
                    sesiones[numero] = {**sesion, "clientes_lista": lista5, "contexto_lista": "cuenta"}
                    respuesta = f"Encontré varios clientes:\n{opciones}\n\nEscribe el numero para ver su cuenta."
                else:
                    respuesta = "No encontré ese cliente. Puedes buscar por nombre o RUT.\n\nLo sentimos, no pudimos procesar tu consulta. Contáctate con nuestra oficina y te ayudamos de inmediato\n💬 *Estrella*: +56 9 6292 9654\n🌐 www.temponovo.cl"
            except Exception as e:
                print(f"ERROR DEUDA: {e}")
                respuesta = "⚠️ Error al buscar el cliente. Intenta de nuevo o usa el RUT.\n\nLo sentimos, no pudimos procesar tu consulta. Contáctate con nuestra oficina y te ayudamos de inmediato\n💬 *Estrella*: +56 9 6292 9654\n🌐 www.temponovo.cl"
        elif sesion.get("partner_id"):
            try:
                deuda = consultar_deuda(sesion["partner_id"])
                respuesta = formatear_deuda(deuda, sesion.get("nombre","cliente"))
            except Exception:
                respuesta = "⚠️ Error al consultar las facturas.\n\nLo sentimos, no pudimos procesar tu consulta. Contáctate con nuestra oficina y te ayudamos de inmediato\n💬 *Estrella*: +56 9 6292 9654\n🌐 www.temponovo.cl"
        else:
            if es_admin:
                respuesta = "Escribe _cuenta de [nombre del cliente]_ o el RUT del cliente."
            else:
                respuesta = "🔐 Escribe tu *RUT* primero.\n_ej: 12.345.678-9_"

    # Catálogos
    elif palabras & CATALOGO:
        catalogos = await cargar_catalogos()
        if not catalogos:
            respuesta = "⚠️ No se pudieron cargar los catálogos. Intenta de nuevo."
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
            respuesta = "📎 Aquí va tu catálogo\n\nEscribe otro numero para ver más, o _menu_ para volver a la lista."
            media_url = url
        elif archivo:
            respuesta = "⚠️ Ese catálogo no esta disponible en este momento."
        else:
            sesiones[numero] = {**sesion, "esperando_catalogo": False}
            try:
                termino = limpiar_termino(body)
                productos = buscar_productos(termino)
                respuesta = formatear_wa(productos, termino)
            except Exception:
                respuesta = "⚠️ Hubo un error. Intenta de nuevo.\n\nLo sentimos, no pudimos procesar tu consulta. Contáctate con nuestra oficina y te ayudamos de inmediato\n💬 *Estrella*: +56 9 6292 9654\n🌐 www.temponovo.cl"

    # Pedidos
    elif palabras & PEDIDO:
        texto_sin_pedido = body_norm
        # Ordenar por largo para reemplazar primero los más largos
        for p in sorted(PEDIDO, key=len, reverse=True):
            texto_sin_pedido = texto_sin_pedido.replace(p, "").strip()
        texto_sin_pedido = re.sub(r"^[^a-z0-9]+", "", texto_sin_pedido)  # quitar caracteres sueltos al inicio
        texto_sin_pedido = re.sub(r"^(de|del|la|el)\s+", "", texto_sin_pedido).strip()

        partner_id = sesion.get("partner_id")
        nombre_cliente = sesion.get("nombre", "")

        # Buscar por numero de pedido (ej: "pedido 4521" o "pedido S04521")
        num_match = re.search(r"s?0*(\d{4,})", texto_sin_pedido, re.IGNORECASE)
        if num_match and len(texto_sin_pedido) <= 8:
            num = num_match.group(1)
            try:
                uid, models = odoo_connect()
                pedidos = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASS,
                    "sale.order", "search_read",
                    [[["name", "like", num]]],
                    {"fields": ["name", "partner_id", "amount_total", "tempo_delivery_state", "date_order"], "limit": 3}
                )
                if pedidos:
                    lineas = []
                    for p in pedidos:
                        estado = ESTADO_PEDIDO.get(p.get("tempo_delivery_state") or "", "—")
                        fecha = p.get("date_order", "")[:10]
                        try:
                            from datetime import datetime
                            fecha = datetime.strptime(fecha, "%Y-%m-%d").strftime("%d/%m/%Y")
                        except: pass
                        cliente_nombre = p["partner_id"][1] if p.get("partner_id") else "—"
                        lineas.append(f"{estado} {p['name']} | {fecha} | {cliente_nombre}")
                    respuesta = "\n".join(lineas)
                else:
                    respuesta = f"No encontré el pedido *{num_match.group(0).upper()}*."
            except Exception as e:
                respuesta = f"⚠️ Error buscando pedido: {e}"
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n    <Message>{xe(respuesta)}</Message>\n</Response>"""
            return PlainTextResponse(content=twiml, media_type="application/xml")

        # Si el texto sin pedido es un RUT, buscar directamente
        if es_rut(texto_sin_pedido):
            rut_norm = normalizar_rut(texto_sin_pedido)
            cliente = buscar_cliente_por_rut(rut_norm)
            if cliente["encontrado"]:
                sesiones[numero] = {**sesion, "partner_id": cliente["id"], "nombre": cliente["nombre"]}
                pedidos = consultar_pedidos(cliente["id"])
                respuesta = formatear_pedidos(pedidos, cliente["nombre"])
            else:
                respuesta = f"❌ No encontré cliente con RUT *{rut_norm}*."
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n    <Message>{xe(respuesta)}</Message>\n</Response>"""
            return PlainTextResponse(content=twiml, media_type="application/xml")

        if len(texto_sin_pedido) >= 2:
            try:
                vendedor_filtro_p = usuario["nombre"] if usuario["tipo"] == "vendedor" else ""
                print(f"Buscando pedidos cliente: [{texto_sin_pedido}] vendedor: [{vendedor_filtro_p}]")
                clientes = buscar_cliente_por_nombre(texto_sin_pedido, vendedor_filtro_p)
                print(f"Pedidos encontrados clientes: {len(clientes)} - {[c['nombre'] for c in clientes]}")
                if len(clientes) == 1:
                    c = clientes[0]
                    sesiones[numero] = {**sesion, "partner_id": c["id"], "nombre": c["nombre"]}
                    pedidos = consultar_pedidos(c["id"])
                    respuesta = formatear_pedidos(pedidos, c["nombre"])
                elif len(clientes) > 1:
                    lista = "\n".join([f"- {c['nombre']} ({c['rut']})" for c in clientes])
                    sesiones[numero] = {**sesion, "contexto": "pedidos"}
                    respuesta = f"Encontré varios clientes:\n{lista}\n\nEscribe el RUT para ver sus pedidos."
                else:
                    respuesta = "No encontré ese cliente. Puedes buscar por nombre o RUT.\n\nLo sentimos, no pudimos procesar tu consulta. Contáctate con nuestra oficina y te ayudamos de inmediato\n💬 *Estrella*: +56 9 6292 9654\n🌐 www.temponovo.cl"
            except Exception as e:
                print(f"Error pedidos: {e}")
                respuesta = "⚠️ Error al consultar pedidos.\n\nLo sentimos, no pudimos procesar tu consulta. Contáctate con nuestra oficina y te ayudamos de inmediato\n💬 *Estrella*: +56 9 6292 9654\n🌐 www.temponovo.cl"
        elif partner_id:
            pedidos = consultar_pedidos(partner_id)
            respuesta = formatear_pedidos(pedidos, nombre_cliente)
        else:
            if es_admin:
                respuesta = "Escribe _pedidos de [nombre]_ o el RUT del cliente."
            else:
                respuesta = "Escribe tu RUT primero para ver tus pedidos."

    # Búsqueda de producto
    else:
        try:
            termino = limpiar_termino(body)
            if not termino or len(termino) < 2:
                # No entendió — mostrar menú según perfil
                if es_admin:
                    respuesta = bienvenida_admin(usuario["nombre"])
                elif sesion.get("nombre"):
                    respuesta = menu_cliente(sesion["nombre"])
                else:
                    respuesta = BIENVENIDA_PUBLICA
            else:
                productos = buscar_productos(termino)
                respuesta = formatear_wa(productos, termino)
        except Exception:
            respuesta = "⚠️ Hubo un error. Intenta de nuevo en un momento.\n\nLo sentimos, no pudimos procesar tu consulta. Contáctate con nuestra oficina y te ayudamos de inmediato\n💬 *Estrella*: +56 9 6292 9654\n🌐 www.temponovo.cl"

    print(f"RESP [{usuario['tipo']}]: {respuesta[:200]}")

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
async def reload(key: str = ""):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="No autorizado")
    global _usuarios, _stock_cache, _deuda_cache
    _usuarios = {}
    _stock_cache = {}
    _deuda_cache = {}
    _pedidos_cache.clear()
    _pedidos_por_num.clear()
    await cargar_usuarios()
    await cargar_stock_cache()
    await cargar_deuda_cache()
    await cargar_pedidos_cache()
    return {"usuarios": len(_usuarios), "stock": len(_stock_cache), "deuda": len(_deuda_cache), "pedidos": len(_pedidos_por_num)}

@app.get("/usuarios")
def ver_usuarios(key: str = ""):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="No autorizado")
    return {k: v for k, v in _usuarios.items()}
