import json
import os
import re
import tempfile
import threading
from io import BytesIO
from pathlib import Path
from typing import Any

import anthropic
import arabic_reshaper
import requests
from bidi.algorithm import get_display
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from pypdf import PdfReader, PdfWriter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


BASE_DIR = Path(__file__).resolve().parent
CONTRACT_TEMPLATE = BASE_DIR / "ejari_contract.pdf"
FONT_PATH = BASE_DIR / "DejaVuSans.ttf"

app = FastAPI(title="Tenancy WhatsApp Bot", version="0.1.0")
sessions: dict[str, dict[str, Any]] = {}
processed_message_ids: set[str] = set()
state_lock = threading.Lock()
model_id_cache: str | None = None


FIELD_LABELS_AR = {
    "contract_date": "تاريخ إعداد العقد",
    "owner_name": "اسم المالك",
    "lessor_name": "اسم المؤجر",
    "lessor_emirates_id": "رقم هوية المؤجر",
    "lessor_is_company": "هل المؤجر شركة؟",
    "lessor_license_no": "رقم رخصة المؤجر",
    "lessor_licensing_authority": "جهة إصدار رخصة المؤجر",
    "lessor_email": "بريد المؤجر الإلكتروني",
    "lessor_phone": "هاتف المؤجر",
    "tenant_name": "اسم المستأجر",
    "tenant_emirates_id": "رقم هوية المستأجر",
    "tenant_is_company": "هل المستأجر شركة؟",
    "tenant_license_no": "رقم رخصة المستأجر",
    "tenant_licensing_authority": "جهة إصدار رخصة المستأجر",
    "tenant_email": "بريد المستأجر الإلكتروني",
    "tenant_phone": "هاتف المستأجر",
    "property_usage": "استخدام العقار: سكني أو تجاري أو صناعي",
    "plot_no": "رقم الأرض",
    "makani_no": "رقم مكاني",
    "building_name": "اسم المبنى",
    "property_no": "رقم العقار أو الوحدة",
    "property_type": "نوع العقار",
    "property_area_sqm": "مساحة العقار بالمتر المربع",
    "location": "المنطقة أو الموقع",
    "dewa_premises_no": "رقم المبنى لدى ديوا",
    "start_date": "تاريخ بداية العقد",
    "end_date": "تاريخ نهاية العقد",
    "contract_value": "قيمة العقد الإجمالية",
    "annual_rent": "الإيجار السنوي",
    "security_deposit": "مبلغ التأمين",
    "payment_mode": "طريقة الدفع وعدد الدفعات",
}

FIELD_LABELS_EN = {
    "contract_date": "Contract preparation date",
    "owner_name": "Owner name",
    "lessor_name": "Lessor name",
    "lessor_emirates_id": "Lessor Emirates ID",
    "lessor_is_company": "Is the lessor a company?",
    "lessor_license_no": "Lessor licence number",
    "lessor_licensing_authority": "Lessor licensing authority",
    "lessor_email": "Lessor email",
    "lessor_phone": "Lessor phone",
    "tenant_name": "Tenant name",
    "tenant_emirates_id": "Tenant Emirates ID",
    "tenant_is_company": "Is the tenant a company?",
    "tenant_license_no": "Tenant licence number",
    "tenant_licensing_authority": "Tenant licensing authority",
    "tenant_email": "Tenant email",
    "tenant_phone": "Tenant phone",
    "property_usage": "Property usage: residential, commercial, or industrial",
    "plot_no": "Plot number",
    "makani_no": "Makani number",
    "building_name": "Building name",
    "property_no": "Property or unit number",
    "property_type": "Property type",
    "property_area_sqm": "Property area in square metres",
    "location": "Location",
    "dewa_premises_no": "DEWA premises number",
    "start_date": "Contract start date",
    "end_date": "Contract end date",
    "contract_value": "Total contract value",
    "annual_rent": "Annual rent",
    "security_deposit": "Security deposit",
    "payment_mode": "Payment method and number of payments",
}

BASE_REQUIRED = list(FIELD_LABELS_AR)


def env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def graph_url(path: str) -> str:
    version = os.getenv("GRAPH_API_VERSION", "v25.0")
    return f"https://graph.facebook.com/{version}/{path.lstrip('/')}"


def send_whatsapp_text(to: str, body: str) -> None:
    token = env("WHATSAPP_TOKEN")
    phone_number_id = env("PHONE_NUMBER_ID")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    for start in range(0, len(body), 3900):
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body[start : start + 3900]},
        }
        response = requests.post(graph_url(f"{phone_number_id}/messages"), headers=headers, json=payload, timeout=30)
        response.raise_for_status()


def send_whatsapp_document(to: str, pdf_path: Path) -> None:
    token = env("WHATSAPP_TOKEN")
    phone_number_id = env("PHONE_NUMBER_ID")
    auth_headers = {"Authorization": f"Bearer {token}"}
    with pdf_path.open("rb") as handle:
        upload = requests.post(
            graph_url(f"{phone_number_id}/media"),
            headers=auth_headers,
            data={"messaging_product": "whatsapp", "type": "application/pdf"},
            files={"file": (pdf_path.name, handle, "application/pdf")},
            timeout=60,
        )
    upload.raise_for_status()
    media_id = upload.json()["id"]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": {"id": media_id, "filename": pdf_path.name, "caption": "Tenancy Contract | عقد الإيجار"},
    }
    response = requests.post(
        graph_url(f"{phone_number_id}/messages"),
        headers={**auth_headers, "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    response.raise_for_status()


def resolve_model(client: anthropic.Anthropic) -> str:
    global model_id_cache
    configured = os.getenv("CLAUDE_MODEL", "").strip()
    if configured:
        return configured
    if model_id_cache:
        return model_id_cache
    models = list(client.models.list(limit=100).data)
    ids = [model.id for model in models]
    for keyword in ("haiku", "sonnet", "opus"):
        for candidate in ids:
            if keyword in candidate.lower():
                model_id_cache = candidate
                return candidate
    if not ids:
        raise RuntimeError("No Claude models are available for this API key")
    model_id_cache = ids[0]
    return model_id_cache


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.S)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Claude response did not contain a JSON object")
    return json.loads(cleaned[start : end + 1])


def extract_details(message: str, known: dict[str, Any]) -> tuple[dict[str, Any], str]:
    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    allowed = list(FIELD_LABELS_EN) + ["additional_terms"]
    prompt = f"""
You extract data for the Dubai Land Department unified tenancy contract.
Return JSON only, with exactly this shape:
{{"language":"ar" or "en", "data":{{}}}}

Allowed data keys:
{json.dumps(allowed)}

Rules:
- Extract only facts explicitly stated by the customer. Never guess, translate a person's name, or invent a value.
- Preserve names exactly as written.
- Normalize yes/no company fields to true or false only when clear.
- Normalize property_usage to Residential, Commercial, or Industrial.
- additional_terms must be an array of strings.
- Exclude unknown keys and empty values.
- Use the language mainly used in the latest customer message.

Already known data:
{json.dumps(known, ensure_ascii=False)}

Latest customer message:
{message}
"""
    response = client.messages.create(
        model=resolve_model(client),
        max_tokens=1800,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    parsed = parse_json_object(text)
    data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
    safe_data = {key: value for key, value in data.items() if key in allowed and value not in (None, "", [])}
    language = "en" if parsed.get("language") == "en" else "ar"
    return safe_data, language


def missing_fields(data: dict[str, Any]) -> list[str]:
    required = list(BASE_REQUIRED)
    if data.get("lessor_is_company") is not True:
        required = [key for key in required if key not in {"lessor_license_no", "lessor_licensing_authority"}]
    if data.get("tenant_is_company") is not True:
        required = [key for key in required if key not in {"tenant_license_no", "tenant_licensing_authority"}]
    return [key for key in required if data.get(key) in (None, "", [])]


def questions_for(missing: list[str], language: str) -> str:
    labels = FIELD_LABELS_EN if language == "en" else FIELD_LABELS_AR
    if language == "en":
        lines = ["I understood the information you sent. Please send the following missing details in one message:"]
    else:
        lines = ["فهمت المعلومات التي أرسلتها. أرسل البيانات الناقصة التالية في رسالة واحدة:"]
    lines.extend(f"• {labels[key]}" for key in missing)
    return "\n".join(lines)


def summary(data: dict[str, Any], language: str) -> str:
    labels = FIELD_LABELS_EN if language == "en" else FIELD_LABELS_AR
    heading = "Please review the contract details:" if language == "en" else "راجع بيانات العقد قبل إصداره:"
    lines = [heading]
    for key in BASE_REQUIRED:
        if key in data and key not in {"lessor_is_company", "tenant_is_company"}:
            lines.append(f"• {labels[key]}: {data[key]}")
    if data.get("additional_terms"):
        label = "Additional terms" if language == "en" else "الشروط الإضافية"
        lines.append(f"• {label}: " + " | ".join(str(x) for x in data["additional_terms"]))
    lines.append("\nReply CONFIRM to issue the PDF, or send the correction." if language == "en" else "\nاكتب «تأكيد» لإصدار ملف PDF، أو أرسل التصحيح المطلوب.")
    return "\n".join(lines)


def is_arabic(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06FF]", text))


def display_text(value: Any) -> str:
    text = str(value)
    return get_display(arabic_reshaper.reshape(text)) if is_arabic(text) else text


def fit_text(c: canvas.Canvas, value: Any, x: float, y: float, max_width: float, size: float = 7.5) -> None:
    if value in (None, ""):
        return
    rendered = display_text(value)
    current = size
    while current > 5 and pdfmetrics.stringWidth(rendered, "ContractFont", current) > max_width:
        current -= 0.25
    c.setFont("ContractFont", current)
    c.setFillColorRGB(0.05, 0.12, 0.28)
    c.drawString(x, y, rendered)


def create_overlay(data: dict[str, Any], width: float, height: float, page_index: int) -> BytesIO:
    stream = BytesIO()
    c = canvas.Canvas(stream, pagesize=(width, height))
    if page_index == 0:
        placements = {
            "contract_date": (47, 726, 82),
            "owner_name": (92, 663, 430),
            "lessor_name": (92, 641, 430),
            "lessor_emirates_id": (112, 619, 380),
            "lessor_license_no": (83, 597, 165),
            "lessor_licensing_authority": (380, 597, 135),
            "lessor_email": (93, 574, 415),
            "lessor_phone": (93, 552, 415),
            "tenant_name": (92, 497, 420),
            "tenant_emirates_id": (112, 475, 360),
            "tenant_license_no": (83, 453, 165),
            "tenant_licensing_authority": (380, 453, 135),
            "tenant_email": (93, 431, 395),
            "tenant_phone": (93, 409, 395),
            "plot_no": (94, 330, 150),
            "makani_no": (384, 330, 140),
            "building_name": (93, 308, 160),
            "property_no": (380, 308, 145),
            "property_type": (84, 286, 170),
            "property_area_sqm": (380, 286, 140),
            "location": (84, 264, 170),
            "dewa_premises_no": (386, 264, 135),
            "start_date": (118, 198, 75),
            "end_date": (216, 198, 75),
            "contract_value": (384, 198, 140),
            "annual_rent": (83, 174, 160),
            "security_deposit": (386, 174, 135),
            "payment_mode": (94, 152, 415),
        }
        for key, (x, y, max_width) in placements.items():
            fit_text(c, data.get(key), x, y, max_width)

        usage = str(data.get("property_usage", "")).lower()
        usage_x = {"industrial": 153, "commercial": 260, "residential": 373}.get(usage)
        if usage_x:
            c.setStrokeColorRGB(0.05, 0.12, 0.28)
            c.setLineWidth(1.4)
            c.line(usage_x - 3.5, 345.5, usage_x + 3.5, 352.5)
            c.line(usage_x - 3.5, 352.5, usage_x + 3.5, 345.5)

    if page_index == 2:
        terms = data.get("additional_terms") or []
        for index, term in enumerate(terms[:5]):
            fit_text(c, term, 55, 516 - index * 25.2, 480, 6.5)
    c.showPage()
    c.save()
    stream.seek(0)
    return stream


def render_contract(data: dict[str, Any], destination: Path) -> None:
    if not CONTRACT_TEMPLATE.exists() or not FONT_PATH.exists():
        raise FileNotFoundError("Contract template or font is missing")
    if "ContractFont" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("ContractFont", str(FONT_PATH)))
    source = PdfReader(str(CONTRACT_TEMPLATE))
    writer = PdfWriter()
    for index, page in enumerate(source.pages):
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        overlay = PdfReader(create_overlay(data, width, height, index)).pages[0]
        page.merge_page(overlay)
        writer.add_page(page)
    with destination.open("wb") as output:
        writer.write(output)


def handle_text_message(sender: str, text: str) -> None:
    normalized = text.strip().lower()
    if normalized in {"إلغاء", "الغاء", "cancel", "restart", "ابدأ من جديد"}:
        with state_lock:
            sessions.pop(sender, None)
        send_whatsapp_text(sender, "تم إلغاء الطلب. اكتب «عقد إيجار» للبدء من جديد.\nRequest cancelled. Send “tenancy contract” to restart.")
        return

    if normalized in {"عقد إيجار", "عقد ايجار", "tenancy contract", "rent contract", "start"}:
        with state_lock:
            sessions[sender] = {"data": {}, "language": "ar" if is_arabic(text) else "en", "ready": False}
        send_whatsapp_text(
            sender,
            "أرسل كل تفاصيل عقد الإيجار المتوفرة لديك في رسالة واحدة، بالعربي أو الإنجليزي. سأفهمها وأسألك عن الناقص فقط.\n\nSend all available tenancy details in one message, in Arabic or English. I will ask only for missing information.",
        )
        return

    with state_lock:
        session = sessions.setdefault(sender, {"data": {}, "language": "ar" if is_arabic(text) else "en", "ready": False})
        current_data = dict(session["data"])

    if normalized in {"تأكيد", "تاكيد", "confirm", "confirmed"}:
        missing = missing_fields(current_data)
        if missing:
            send_whatsapp_text(sender, questions_for(missing, session["language"]))
            return
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / "tenancy_contract.pdf"
            render_contract(current_data, pdf_path)
            send_whatsapp_document(sender, pdf_path)
        send_whatsapp_text(sender, "تم تجهيز عقد الإيجار للمراجعة والتوقيع.\nYour tenancy contract is ready for review and signature.")
        with state_lock:
            sessions.pop(sender, None)
        return

    updates, language = extract_details(text, current_data)
    current_data.update(updates)
    missing = missing_fields(current_data)
    with state_lock:
        sessions[sender] = {"data": current_data, "language": language, "ready": not missing}
    send_whatsapp_text(sender, questions_for(missing, language) if missing else summary(current_data, language))


def process_webhook(payload: dict[str, Any]) -> None:
    try:
        entries = payload.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for message in value.get("messages", []) or []:
                    message_id = message.get("id", "")
                    with state_lock:
                        if message_id and message_id in processed_message_ids:
                            continue
                        if message_id:
                            processed_message_ids.add(message_id)
                            if len(processed_message_ids) > 2000:
                                processed_message_ids.clear()
                    sender = message.get("from")
                    if not sender:
                        continue
                    if message.get("type") != "text":
                        send_whatsapp_text(sender, "حالياً أرسل التفاصيل كتابةً. دعم الصور والمستندات سيكون في المرحلة التالية.\nFor now, please send the details as text.")
                        continue
                    handle_text_message(sender, message.get("text", {}).get("body", ""))
    except Exception as exc:
        print(f"Webhook processing failed: {type(exc).__name__}: {exc}", flush=True)


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "tenancy-whatsapp-bot", "status": "running"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/webhook")
def verify_webhook(request: Request) -> Response:
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == env("VERIFY_TOKEN"):
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    form = await request.form()
sender = str(form.get("From", "")).replace("whatsapp:", "")
message = str(form.get("Body", ""))

if sender and message:
    background_tasks.add_task(handle_text_message, sender, message)

return {"status": "accepted"}
