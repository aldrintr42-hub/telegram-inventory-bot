import os
import logging
import json
import io
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                          filters, ContextTypes, ConversationHandler)
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# CONFIGURACIÓN DESDE VARIABLES DE ENTORNO
# ==========================================
BOT_TOKEN = os.getenv('BOT_TOKEN')
GOOGLE_DRIVE_ROOT_FOLDER_ID = os.getenv('GOOGLE_DRIVE_ROOT_FOLDER_ID')
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')

# Validar que todas las variables estén configuradas
required_vars = {
    'BOT_TOKEN': BOT_TOKEN,
    'GOOGLE_DRIVE_ROOT_FOLDER_ID': GOOGLE_DRIVE_ROOT_FOLDER_ID,
    'GOOGLE_SERVICE_ACCOUNT_JSON': GOOGLE_SERVICE_ACCOUNT_JSON
}

for var_name, var_value in required_vars.items():
    if not var_value:
        print(f"❌ Variable de entorno {var_name} no está configurada")
        exit(1)

# LOGGING
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# ESTADOS DE CONVERSACIÓN
PUNTO_VENTA, CAJA, ACRILICO, FOTOS = range(4)

# OPCIONES DE CAJA
cajas = [["CAJA A", "CAJA B", "CAJA C"], ["CAJA D", "CAJA E", "CAJA F"], ["CAJA G", "CAJA H"]]

# Google Drive API
SCOPES = ['https://www.googleapis.com/auth/drive']

# Cargar credenciales desde JSON
service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
creds = service_account.Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=creds)

# Variables en memoria
user_data = {}

# ======== FUNCIONES DE GOOGLE DRIVE =========

def crear_carpeta(nombre, carpeta_padre_id):
    results = drive_service.files().list(q=f"mimeType='application/vnd.google-apps.folder' and name='{nombre}' and '{carpeta_padre_id}' in parents and trashed = false",
                                        spaces='drive', fields='files(id, name)').execute()
    items = results.get('files', [])
    if items:
        return items[0]['id']
    metadata = {
        'name': nombre,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [carpeta_padre_id]
    }
    carpeta = drive_service.files().create(body=metadata, fields='id').execute()
    return carpeta.get('id')

def subir_archivo(nombre_archivo, contenido, mime_type, carpeta_id):
    media = MediaIoBaseUpload(io.BytesIO(contenido), mimetype=mime_type)
    archivo = {
        'name': nombre_archivo,
        'parents': [carpeta_id]
    }
    drive_service.files().create(body=archivo, media_body=media, fields='id').execute()

# ========= FLUJO DEL BOT ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📍 Ingresa el nombre del punto de venta:")
    return PUNTO_VENTA

async def recibir_punto_venta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data[update.effective_chat.id] = {
        'punto_venta': update.message.text,
        'imagenes': [],
        'acrilicos': [],
        'acrilico_actual': None,
        'fotos_actuales': []
    }
    await update.message.reply_text("📦 Selecciona el tipo de caja:", reply_markup=ReplyKeyboardMarkup(cajas, one_time_keyboard=True))
    return CAJA

async def recibir_caja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data[update.effective_chat.id]['caja'] = update.message.text
    await update.message.reply_text("🖼️ Escribe los acrílicos seleccionados separados por comas. Ej: ACRILICO 1, ACRILICO 2")
    return ACRILICO

async def recibir_acrilicos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    acrilicos = [a.strip().upper() for a in update.message.text.split(',')]
    user_data[update.effective_chat.id]['acrilicos'] = acrilicos
    user_data[update.effective_chat.id]['acrilico_actual'] = acrilicos[0]
    await update.message.reply_text(f"📸 Envía las fotos del {acrilicos[0]} (máximo 5 fotos).")
    return FOTOS

async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    data = user_data[chat_id]
    punto = data['punto_venta']
    caja = data['caja']
    acrilico = data['acrilico_actual']

    file = await context.bot.get_file(update.message.photo[-1].file_id)
    contenido = await file.download_as_bytearray()

    # Crear carpeta principal
    carpeta_punto = crear_carpeta(punto, GOOGLE_DRIVE_ROOT_FOLDER_ID)

    # Nombre del archivo
    nro_foto = len(data['fotos_actuales']) + 1
    nombre_archivo = f"{punto}_{caja}_{acrilico}_{nro_foto}.jpg"

    try:
        subir_archivo(nombre_archivo, contenido, 'image/jpeg', carpeta_punto)
        data['fotos_actuales'].append(nombre_archivo)
        if len(data['fotos_actuales']) < 5:
            await update.message.reply_text(f"✅ Foto {nro_foto} subida. Puedes enviar otra.")
        else:
            idx = data['acrilicos'].index(acrilico)
            if idx + 1 < len(data['acrilicos']):
                nuevo_acrilico = data['acrilicos'][idx + 1]
                data['acrilico_actual'] = nuevo_acrilico
                data['fotos_actuales'] = []
                await update.message.reply_text(f"📸 Ahora envía las fotos del {nuevo_acrilico} (máximo 5 fotos).")
            else:
                await update.message.reply_text("✅ Todas las fotos fueron subidas. ¡Gracias!")
                return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error al subir foto: {e}")
        await update.message.reply_text("❌ Error general al procesar las fotos. Por favor, intenta nuevamente o contacta al soporte.")

    return FOTOS

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Operación cancelada.")
    return ConversationHandler.END

# ========= MAIN ==========
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            PUNTO_VENTA: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_punto_venta)],
            CAJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_caja)],
            ACRILICO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_acrilicos)],
            FOTOS: [MessageHandler(filters.PHOTO, recibir_foto)],
        },
        fallbacks=[CommandHandler('cancelar', cancelar)]
    )

    app.add_handler(conv_handler)
    app.run_polling()

if __name__ == '__main__':
    main()
