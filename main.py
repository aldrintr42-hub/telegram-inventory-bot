import os
import logging
import json
import tempfile
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# CONFIGURACIÓN DESDE VARIABLES DE ENTORNO
# ==========================================
BOT_TOKEN = os.getenv('BOT_TOKEN')
GOOGLE_DRIVE_ROOT_FOLDER_ID = os.getenv('GOOGLE_DRIVE_ROOT_FOLDER_ID')
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')

required_vars = {
    'BOT_TOKEN': BOT_TOKEN,
    'GOOGLE_DRIVE_ROOT_FOLDER_ID': GOOGLE_DRIVE_ROOT_FOLDER_ID,
    'GOOGLE_SERVICE_ACCOUNT_JSON': GOOGLE_SERVICE_ACCOUNT_JSON
}
for var_name, var_value in required_vars.items():
    if not var_value:
        print(f"❌ Variable de entorno {var_name} no está configurada")
        exit(1)

# SETUP LOGS
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# GOOGLE DRIVE SETUP
SCOPES = ['https://www.googleapis.com/auth/drive']
service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
creds = service_account.Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=creds)

# ESTADOS DE CONVERSACIÓN
PUNTO, CAJA, ACRILICO, FOTOS = range(4)

# VARIABLES TEMPORALES
user_data_store = {}

# COMANDOS DE BOT
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📍 Ingresa el punto de venta:")
    return PUNTO

async def recibir_punto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['punto'] = update.message.text.upper().strip()
    await update.message.reply_text("📦 Selecciona el tipo de caja:", reply_markup=ReplyKeyboardMarkup([
        ["CAJA A", "CAJA B"], ["CAJA C", "CAJA D"]], one_time_keyboard=True))
    return CAJA

async def recibir_caja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['caja'] = update.message.text.upper().strip()
    await update.message.reply_text("🔢 Escribe el número de acrílico (ej. 7):")
    return ACRILICO

async def recibir_acrilico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['acrilico'] = update.message.text.strip()
    context.user_data['contador'] = 1
    await update.message.reply_text("📷 Envía la primera foto del acrílico")
    return FOTOS

async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    punto = context.user_data['punto']
    caja = context.user_data['caja']
    acrilico = context.user_data['acrilico']
    contador = context.user_data['contador']

    photo = update.message.photo[-1]
    file = await photo.get_file()

    filename = f"{punto}_{caja}_ACRILICO_{acrilico}_{contador}.jpg"
    temp_path = os.path.join(tempfile.gettempdir(), filename)

    await file.download_to_drive(temp_path)
    logger.info(f"📥 Guardado temporalmente en: {temp_path}")

    try:
        subir_a_drive(temp_path, filename)
    except Exception as e:
        logger.error(f"❌ Error al subir {filename}: {e}")
        await update.message.reply_text("⚠️ Error al subir la imagen. Intenta nuevamente.")
        return FOTOS
    finally:
        try:
            os.remove(temp_path)
            logger.info(f"🧹 Archivo temporal eliminado: {temp_path}")
        except Exception as e:
            logger.warning(f"⚠️ No se pudo eliminar el archivo temporal: {e}")

    context.user_data['contador'] += 1
    if context.user_data['contador'] <= 5:
        await update.message.reply_text(f"📷 Envía la siguiente foto del acrílico ({context.user_data['contador']}/5):")
        return FOTOS
    else:
        await update.message.reply_text("✅ Proceso finalizado. Usa /start para iniciar otro.")
        return ConversationHandler.END
# SUBIR A DRIVE
def subir_a_drive(filepath, filename):
    try:
        file_metadata = {
            'name': filename,
            'parents': [GOOGLE_DRIVE_ROOT_FOLDER_ID]
        }
        media = MediaFileUpload(filepath, mimetype='image/jpeg')
        uploaded_file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        logger.info(f"✅ Archivo subido a Drive: {filename} (ID: {uploaded_file.get('id')})")
    except Exception as e:
        logger.error(f"❌ Error al subir {filename}: {e}")

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Operación cancelada.")
    return ConversationHandler.END

# MAIN
if __name__ == '__main__':
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            PUNTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_punto)],
            CAJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_caja)],
            ACRILICO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_acrilico)],
            FOTOS: [MessageHandler(filters.PHOTO, recibir_foto)]
        },
        fallbacks=[CommandHandler("cancelar", cancelar)]
    )

    app.add_handler(conv_handler)
    app.run_polling()
