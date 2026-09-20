# Tenancy WhatsApp Bot - Pilot

نسخة تجريبية لبوت واتساب يجمع بيانات عقد الإيجار برسالة حرة، ويسأل عن البيانات الناقصة مجتمعة، ثم يجهز عقد PDF بعد تأكيد العميل.

## Environment variables

- `ANTHROPIC_API_KEY`: Claude API key
- `WHATSAPP_TOKEN`: Meta WhatsApp Cloud API token
- `PHONE_NUMBER_ID`: WhatsApp phone number ID
- `VERIFY_TOKEN`: a private phrase you choose for webhook verification
- `GRAPH_API_VERSION`: defaults to `v25.0`
- `CLAUDE_MODEL`: optional; when empty, the app selects an available Haiku/Sonnet model

## Render

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn -k uvicorn.workers.UvicornWorker -w 1 -b 0.0.0.0:$PORT app:app`
- Health check: `/health`
- Meta callback URL after deployment: `https://YOUR-SERVICE.onrender.com/webhook`

## Pilot commands in WhatsApp

- `عقد إيجار` or `tenancy contract`: start/restart
- Send all known details in one message.
- `تأكيد` or `confirm`: generate the PDF after the summary is complete.
- `إلغاء` or `cancel`: clear the current request.

The pilot keeps conversation state in memory. A later production version should use a persistent database and a permanent Meta access token.
