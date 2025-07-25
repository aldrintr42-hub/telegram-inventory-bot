import os
import time
import json
import base64
from io import BytesIO
from telegram import Update, ReplyKeyboardMarkup, Bot
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, filters, ConversationHandler
)
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError
from google.oauth2 import service_account
import nest_asyncio
nest_asyncio.apply()

# Estados del flujo de conversación
PUNTO_VENTA, CAJA, ACRILICO, ENVIO_FOTOS, CONFIRMACION = range(5)

# Teclados
cajas = [["CAJA A", "CAJA B", "CAJA C", "CAJA D"], ["CAJA E", "CAJA F", "CAJA G", "CAJA H"]]
acrilicos_opciones = [["ACRILICO 1", "ACRILICO 2", "ACRILICO 3"], ["ACRILICO 4", "ACRILICO 5", "ACRILICO 6"], ["ACRILICO 7", "ACRILICO 8", "ACRILICO 9"]]

# Config desde entorno
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GOOGLE_DRIVE_ROOT_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_ROOT_FOLDER_ID")

# ---------------- GOOGLE DRIVE ------------------
def authenticate_google_drive_service_account():
    try:
        b64_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not b64_json:
            raise ValueError("Variable GOOGLE_SERVICE_ACCOUNT_JSON no configurada.")
        service_account_info = json.loads(base64.b64decode(b64_json))
        creds = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        return creds
    except Exception as e:
        print(f"❌ Error en autenticación: {e}")
        return None

def get_or_create_drive_folder_id(service, folder_name, parent_folder_id):
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and '{parent_folder_id}' in parents and trashed=false"
    try:
        response = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        files = response.get('files', [])
        if files:
            return files[0]['id']
        file_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_folder_id]
        }
        folder = service.files().create(body=file_metadata, fields='id').execute()
        return folder.get('id')
    except HttpError as error:
        print(f"❌ Error carpeta '{folder_name}': {error}")
        return None

# ------------------- FLUJO BOT -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📍 Ingrese el nombre del punto de venta:")
    return PUNTO_VENTA

async def recibir_punto_venta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['punto_venta'] = update.message.text.strip()
    await update.message.reply_text("📦 Selecciona el tipo de caja:", reply_markup=ReplyKeyboardMarkup(cajas, one_time_keyboard=True, resize_keyboard=True))
    return CAJA

async def recibir_caja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['caja'] = update.message.text.strip().replace(" ", "_").upper()
    texto = "🧊 Selecciona los acrílicos (escribe los números separados por comas, ej: 1,2,4):\n" + "\n".join([", ".join(row) for row in acrilicos_opciones])
    await update.message.reply_text(texto)
    return ACRILICO

async def recibir_acrilico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        indices = [int(x.strip()) for x in update.message.text.split(",")]
        acrilicos = [f"ACRILICO_{i}" for i in indices if 1 <= i <= 9]
        if not acrilicos:
            raise ValueError
    except:
        await update.message.reply_text("⚠️ Entrada inválida. Usa formato: 1,2,3")
        return ACRILICO

    context.user_data['acrilicos'] = acrilicos
    context.user_data['acrilico_actual_idx'] = 0
    context.user_data['fotos_dict'] = {a: [] for a in acrilicos}
    return await iniciar_envio_fotos(update, context)

async def iniciar_envio_fotos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data['acrilico_actual_idx']
    acrilico = context.user_data['acrilicos'][idx]
    await update.message.reply_text(f"📸 Envía las fotos del {acrilico} (máximo 5).")
    return ENVIO_FOTOS

async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data['acrilico_actual_idx']
    acrilico = context.user_data['acrilicos'][idx]
    fotos_dict = context.user_data['fotos_dict']

    if len(fotos_dict[acrilico]) >= 5:
        await update.message.reply_text("⚠️ Máximo 5 fotos para este acrílico.")
        return CONFIRMACION

    fotos_dict[acrilico].append(update.message.photo[-1].file_id)
    await update.message.reply_text("✅ Foto recibida.\n/Siguiente para otra, /Acrilico para cambiar, /finalizar para guardar.")
    return CONFIRMACION

async def siguiente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    acrilico = context.user_data['acrilicos'][context.user_data['acrilico_actual_idx']]
    if len(context.user_data['fotos_dict'][acrilico]) >= 5:
        await update.message.reply_text("🚫 Ya enviaste 5 fotos. Usa /Acrilico o /finalizar.")
        return CONFIRMACION
    await update.message.reply_text(f"📸 Envía otra foto del {acrilico}.")
    return ENVIO_FOTOS

async def cambiar_acrilico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['acrilico_actual_idx'] += 1
    idx = context.user_data['acrilico_actual_idx']
    acrilicos = context.user_data['acrilicos']
    if idx >= len(acrilicos):
        return await finalizar(update, context)
    await update.message.reply_text(f"📸 Ahora envía fotos del {acrilicos[idx]} (max 5).")
    return ENVIO_FOTOS

async def finalizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    datos = context.user_data
    resumen = "\n".join([f"- {k}: {len(v)} foto(s)" for k, v in datos['fotos_dict'].items()])
    await update.message.reply_text(f"Guardando en Google Drive...\n{resumen}")

    creds = authenticate_google_drive_service_account()
    if not creds:
        await update.message.reply_text("❌ Error con autenticación Google Drive.")
        return ConversationHandler.END

    try:
        service = build('drive', 'v3', credentials=creds)
        punto = datos['punto_venta'].replace(" ", "_").upper()
        caja = datos['caja'].upper()
        folder_id = get_or_create_drive_folder_id(service, punto, GOOGLE_DRIVE_ROOT_FOLDER_ID)
        bot: Bot = context.bot
        
        contador = 0
        for acrilico, fotos in datos['fotos_dict'].items():
            for i, file_id in enumerate(fotos):
                photo_bytes_io = BytesIO()
                file_info = await bot.get_file(file_id)
                await file_info.download_to_memory(photo_bytes_io)
                photo_bytes_io.seek(0)

                media = MediaIoBaseUpload(photo_bytes_io, mimetype='image/jpeg', resumable=True)
                nombre = f"{punto}_{caja}_{acrilico}_{i+1}.jpg"
                metadata = {'name': nombre, 'parents': [folder_id]}
                service.files().create(body=metadata, media_body=media, fields='id').execute()
                contador += 1

        await update.message.reply_text(f"✔️ {contador} fotos subidas correctamente.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error subiendo fotos: {str(e)[:100]}")

    return ConversationHandler.END

# ------------------- CONTROL -------------------
async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Proceso cancelado.")
    return ConversationHandler.END

async def stop_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Deteniendo el bot.")
    await context.application.stop()
    await context.application.shutdown()
    return ConversationHandler.END

# ------------------- MAIN -------------------
def crear_y_ejecutar_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            PUNTO_VENTA: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_punto_venta)],
            CAJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_caja)],
            ACRILICO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_acrilico)],
            ENVIO_FOTOS: [MessageHandler(filters.PHOTO, recibir_foto)],
            CONFIRMACION: [
                CommandHandler("Siguiente", siguiente),
                CommandHandler("Acrilico", cambiar_acrilico),
                CommandHandler("finalizar", finalizar),
            ],
        },
        fallbacks=[
            CommandHandler("cancelar", cancelar),
            CommandHandler("stop", stop_bot_command),
        ],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("stop", stop_bot_command))

    print("🤖 Bot corriendo...")
    app.run_polling()

if __name__ == "__main__":
    crear_y_ejecutar_bot()
