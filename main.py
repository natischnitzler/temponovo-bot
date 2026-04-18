import os
import xmlrpc.client
import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import json

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
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


def consultar_stock(producto: str) -> dict:
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        productos = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "product.template", "search_read",
            [[["name", "ilike", producto], ["sale_ok", "=", True], ["active", "=", True]]],
            {"fields": ["name", "default_code", "list_price", "qty_available"], "limit": 5}
        )
        if not productos:
            return {"encontrado": False, "mensaje": f"No encontré ningún producto con '{producto}'."}
        return {
            "encontrado": True,
            "productos": [
                {
                    "nombre": p["name"],
                    "codigo": p.get("default_code") or "—",
                    "precio": p["list_price"],
                    "stock": int(p.get("qty_available", 0)),
                }
                for p in productos
            ]
        }
    except Exception as e:
        return {"error": str(e)}


TOOLS = [
    {
        "name": "consultar_stock",
        "description": "Consulta stock y precio de un producto en Odoo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "producto": {"type": "string", "description": "Nombre o código del producto a buscar"}
            },
            "required": ["producto"]
        }
    }
]

SYSTEM = """Eres el asistente de Temponovo, distribuidora chilena de relojes Casio, calculadoras y pilas Maxell.

Solo puedes consultar stock y precios de productos. Para cualquier otra consulta (deudas, pedidos, etc.) indica amablemente que por ahora esa función no está disponible y que contacten a su vendedor.

Cuando el cliente pregunte por un producto, usa la tool consultar_stock.
Responde en español chileno, de forma breve y directa.
Los precios en formato $XX.XXX."""


class ChatRequest(BaseModel):
    messages: list

class ChatResponse(BaseModel):
    respuesta: str
    messages: list


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    messages = req.messages
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        while response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    resultado = consultar_stock(block.input["producto"])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(resultado, ensure_ascii=False),
                    })

            messages = messages + [
                {"role": "assistant", "content": [b.model_dump() for b in response.content]},
                {"role": "user", "content": tool_results},
            ]
            response = claude.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=800,
                system=SYSTEM,
                tools=TOOLS,
                messages=messages,
            )

        texto = " ".join(b.text for b in response.content if hasattr(b, "text"))
        messages = messages + [{"role": "assistant", "content": texto}]
        return ChatResponse(respuesta=texto, messages=messages)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "ok"}
