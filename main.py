import os
import xmlrpc.client
import traceback
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ODOO_URL  = os.environ.get("ODOO_URL",  "https://temponovo.odoo.com")
ODOO_DB   = os.environ.get("ODOO_DB",   "cmcorpcl-temponovo-main")
ODOO_USER = os.environ.get("ODOO_USER", "")
ODOO_PASS = os.environ.get("ODOO_PASS", "")


def odoo_uid():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    return common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})


class StockRequest(BaseModel):
    producto: str


@app.post("/stock")
def consultar_stock(req: StockRequest):
    try:
        uid = odoo_uid()
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        t = req.producto.strip()
        resultados = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "product.template", "search_read",
            [[["active", "=", True], "|", ["name", "ilike", t], ["default_code", "ilike", t]]],
            {"fields": ["name", "default_code", "list_price", "qty_available"], "limit": 50}
        )
        return {
            "productos": [
                {
                    "nombre": p["name"],
                    "codigo": p.get("default_code") or "—",
                    "precio": p["list_price"],
                    "stock": int(p.get("qty_available", 0)),
                }
                for p in resultados
            ]
        }
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "ok", "odoo_url": ODOO_URL, "odoo_db": ODOO_DB, "odoo_user": ODOO_USER}
