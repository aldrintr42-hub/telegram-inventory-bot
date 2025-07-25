import os
import json
from io import BytesIO
import pickle # Necesario para cargar/guardar credenciales si usas 'token.pickle'
import time # Importado si es necesario para pausas, aunque no se usa directamente en este flujo

from telegram import Update, ReplyKeyboardMarkup, Bot
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, filters, ConversationHandler
)

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request # Para refrescar el token

import nest_asyncio
nest_asyncio.apply() # Permite correr bucles de asyncio anidados, útil en ciertos entornos

# --- Configuración inicial ---

# Estados del flujo de conversación
PUNTO_VENTA, CAJA, ACRILICO, ENVIO_FOTOS, CONFIRMACION = range(5)

# Opciones de teclado predefinidas
cajas = [["CAJA A", "CAJA B", "CAJA C", "CAJA D"], ["CAJA E", "CAJA F", "CAJA G", "CAJA H"]]
acrilicos_opciones = [
    ["ACRILICO 1", "ACRILICO 2", "ACRILICO 3"],
    ["ACRILICO 4", "ACRILICO 5", "ACRILICO 6"],
    ["ACRILICO 7", "ACRILICO 8", "ACRILICO 9"]
]

# ID de la carpeta principal de Google Drive donde quieres guardar los archivos
# Es buena práctica que esto sea una variable de entorno en Render también.
GOOGLE_DRIVE_ROOT_FOLDER_ID = "1KOwAELybcfzEBRxO4oa9WeLeHvIru5R5"

# Scopes para Google Drive API
# drive.file permite acceso solo a los archivos creados o abiertos por la aplicación
# Si necesitas acceso a cualquier archivo en el Drive del usuario (ej. para verificar carpetas que no creó la app), usa 'https://www.googleapis.com/auth/drive'
SCOPES = ['https://www.googleapis.com/auth/drive.file']

# --- Variables de Entorno (se recomienda siempre obtener desde el entorno en Render) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "TU_BOT_TOKEN_POR_DEFECTO_SI_NO_ESTA_EN_ENV") # Reemplaza el default si lo tienes en código
# Credenciales OAuth de usuario
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN")

# Variable global para almacenar las credenciales de Google Drive una vez autenticadas
global_creds = None

# --- Función de Autenticación de Google Drive (OAuth de Usuario) ---

def authenticate_google_drive_oauth_user():
    """
    Autentica con Google Drive usando un token de refresco y credenciales OAuth de usuario.
    Intenta cargar credenciales existentes, refrescarlas si es necesario,
    o crearlas por primera vez si es un nuevo inicio con el refresh token.
    """
    global global_creds # Declara que vamos a modificar la variable global

    if global_creds and global_creds.valid:
        return global_creds # Ya tenemos credenciales válidas

    # Si no tenemos credenciales válidas en memoria, intentamos crearlas/refrescarlas
    try:
        if not all([GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN]):
            print("⚠️ Faltan variables de entorno para la autenticación OAuth de Google Drive.")
            return None

        # Crea el objeto Credentials usando el token de refresco y los detalles del cliente
        creds = Credentials(
            token=None,  # El token de acceso se obtendrá con el refresh_token
            refresh_token=GOOGLE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=SCOPES # Usa los scopes definidos
        )

        # Intenta refrescar el token de acceso. Si el refresh_token es válido,
        # esto obtendrá un nuevo token de acceso.
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif not creds.valid: # Si no son válidas y no tienen refresh_token (o ya se usó)
            print("❌ El token de refresco no es válido o ha expirado y no se pudo refrescar.")
            return None

        global_creds = creds # Almacena las credenciales válidas globalmente
        print("✅ Autenticación de Google Drive exitosa.")
        return creds

    except Exception as e:
        print(f"❌ Error en autenticación de Google Drive (OAuth de usuario): {e}")
        return None

# --- Funciones de la Conversación ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia la conversación y pide el punto de venta."""
    await update.message.reply_text("📍 Ingrese el nombre del punto de venta:")
    return PUNTO_VENTA

async def recibir_punto_venta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guarda el punto de venta y pide el tipo de caja."""
    context.user_data['punto_venta'] = update.message.text.strip()
    await update.message.reply_text("📦 Selecciona el tipo de caja:",
                                     reply_markup=ReplyKeyboardMarkup(cajas, one_time_keyboard=True, resize_keyboard=True))
    return CAJA

async def recibir_caja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guarda la caja y pide los acrílicos."""
    context.user_data['caja'] = update.message.text.strip().replace(" ", "_").upper()
    texto = "🧊 Selecciona los acrílicos (escribe los números separados por comas, ej: 1,2,4):\n" + "\n".join([", ".join(row) for row in acrilicos_opciones])
    await update.message.reply_text(texto)
    return ACRILICO

async def recibir_acrilico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Valida y guarda los acrílicos seleccionados e inicia la subida de fotos."""
    try:
        seleccion = update.message.text
        indices = [int(x.strip()) for x in seleccion.split(",")]
        # Filtrar para asegurar que los índices estén entre 1 y 9 y mapearlos a "ACRILICO_X"
        acrilicos = [f"ACRILICO_{i}" for i in indices if 1 <= i <= 9]
        if not acrilicos:
            raise ValueError("No se seleccionaron acrílicos válidos.")
    except (ValueError, IndexError):
        await update.message.reply_text("⚠️ Entrada inválida. Por favor, escribe los números de los acrílicos separados por comas (ej: 1,2,3).")
        return ACRILICO

    context.user_data['acrilicos'] = acrilicos
    context.user_data['acrilico_actual_idx'] = 0
    context.user_data['fotos_dict'] = {a: [] for a in acrilicos}

    return await iniciar_envio_fotos(update, context)

async def iniciar_envio_fotos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pide al usuario que envíe las fotos para el acrílico actual en la secuencia."""
    idx = context.user_data['acrilico_actual_idx']
    acrilico = context.user_data['acrilicos'][idx]
    await update.message.reply_text(f"📸 Envía las fotos del {acrilico} (máximo 5).")
    return ENVIO_FOTOS

async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guarda una foto y pregunta al usuario qué hacer a continuación."""
    idx = context.user_data['acrilico_actual_idx']
    acrilico = context.user_data['acrilicos'][idx]
    fotos_dict = context.user_data['fotos_dict']

    if len(fotos_dict[acrilico]) >= 5:
        await update.message.reply_text("⚠️ Ya has enviado el máximo de 5 fotos para este acrílico.")
        return CONFIRMACION

    file_id = update.message.photo[-1].file_id
    fotos_dict[acrilico].append(file_id)

    await update.message.reply_text(
        "✅ Foto recibida.\n\n"
        "Escribe /siguiente para enviar otra foto.\n"
        "Escribe /acrilico para pasar al siguiente acrílico.\n"
        "O escribe /finalizar para guardar todo."
    )
    return CONFIRMACION

async def siguiente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Regresa al estado de envío de fotos para el mismo acrílico."""
    idx = context.user_data['acrilico_actual_idx']
    acrilico = context.user_data['acrilicos'][idx]
    if len(context.user_data['fotos_dict'][acrilico]) >= 5:
        await update.message.reply_text("🚫 Ya has alcanzado el límite de 5 fotos para este acrílico. Usa /acrilico o /finalizar.")
        return CONFIRMACION

    await update.message.reply_text(f"📸 Puedes enviar otra foto del {acrilico}.")
    return ENVIO_FOTOS

async def cambiar_acrilico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Avanza al siguiente acrílico o finaliza si no hay más."""
    context.user_data['acrilico_actual_idx'] += 1
    idx = context.user_data['acrilico_actual_idx']
    acrilicos = context.user_data['acrilicos']

    if idx >= len(acrilicos):
        await update.message.reply_text("✅ Has completado todos los acrílicos. Finalizando...")
        return await finalizar(update, context)

    nuevo_acrilico = acrilicos[idx]
    await update.message.reply_text(f"📸 Ahora envía las fotos del {nuevo_acrilico} (máx. 5).")
    return ENVIO_FOTOS

# --- Funciones auxiliares de Google Drive ---

async def get_or_create_drive_folder_id(service, folder_name, parent_folder_id):
    """
    Busca una carpeta por nombre dentro de una carpeta padre específica.
    Si no la encuentra, la crea y devuelve su ID.
    """
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and '{parent_folder_id}' in parents and trashed=false"
    try:
        response = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        files = response.get('files', [])

        if files:
            print(f"✅ Carpeta '{folder_name}' encontrada en Google Drive con ID: {files[0]['id']}")
            return files[0]['id']
        else:
            file_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [parent_folder_id]
            }
            folder = service.files().create(body=file_metadata, fields='id').execute()
            print(f"✅ Carpeta '{folder_name}' creada en Google Drive con ID: {folder.get('id')}")
            return folder.get('id')
    except HttpError as error:
        print(f"❌ Error al buscar/crear la carpeta '{folder_name}': {error}")
        # Detalle del error 403 (permisos) o 404 (no encontrado)
        # update.message.reply_text está comentado porque no tenemos 'update' aquí directamente
        # Si quieres este mensaje, deberías pasar 'update' como argumento a esta función.
        # if error.resp.status == 403:
        #     await update.message.reply_text(f"❌ Error de permisos al acceder/crear carpeta en Drive. Asegúrate de que la carpeta raíz '{parent_folder_id}' esté compartida con tu cuenta de Google con permisos de Editor, y que tus credenciales OAuth sean correctas.")
        return None

# Finaliza la conversación y guarda todos los archivos en Google Drive
async def finalizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra un resumen, guarda todas las fotos en Google Drive y termina la conversación."""
    datos = context.user_data

    resumen_fotos = "\n".join([f"  - {ac}: {len(fotos)} foto(s)" for ac, fotos in datos['fotos_dict'].items()])
    resumen = (
        f"✅ Proceso finalizado. Se guardarán los siguientes datos:\n\n"
        f"📍 Punto de venta: {datos['punto_venta']}\n"
        f"📦 Caja: {datos['caja']}\n"
        f"🧊 Fotos por acrílico:\n{resumen_fotos}"
    )
    await update.message.reply_text(resumen)

    await update.message.reply_text("📥 Subiendo fotos a Google Drive... por favor espera.")

    # Intentar autenticarse
    creds = authenticate_google_drive_oauth_user()

    if not creds or not creds.valid:
        await update.message.reply_text("❌ No se pudieron obtener credenciales válidas para Google Drive. Por favor, asegúrate de que las variables de entorno de OAuth estén configuradas correctamente en Render.")
        print("Error: No hay credenciales válidas para Google Drive al finalizar.")
        return ConversationHandler.END

    try:
        service = build('drive', 'v3', credentials=creds)

        punto = datos['punto_venta'].replace(" ", "_").upper()
        caja = datos['caja'].upper()
        fotos_dict = datos['fotos_dict']
        bot: Bot = context.bot

        # Obtener o crear la carpeta del punto de venta dentro de la carpeta raíz
        punto_venta_folder_id = await get_or_create_drive_folder_id(service, punto, GOOGLE_DRIVE_ROOT_FOLDER_ID)
        if not punto_venta_folder_id:
            await update.message.reply_text("❌ No se pudo crear/obtener la carpeta del punto de venta en Drive. Abortando subida.")
            return ConversationHandler.END

        # Recorrido de los acrílicos y sus fotos para subir
        total_fotos_subidas = 0
        for acrilico, fotos in fotos_dict.items():
            for i, file_id in enumerate(fotos):
                nombre_archivo_drive = f"{punto}_{caja}_{acrilico}_{i+1}.jpg"
                try:
                    photo_bytes_io = BytesIO()
                    file_info = await bot.get_file(file_id)
                    await file_info.download_to_memory(photo_bytes_io)
                    photo_bytes_io.seek(0) # Rebobinar al inicio del stream para la subida

                    media = MediaIoBaseUpload(photo_bytes_io, mimetype='image/jpeg', resumable=True)
                    
                    file_metadata = {
                        'name': nombre_archivo_drive,
                        'parents': [punto_venta_folder_id] # Asegúrate de subir a la carpeta del punto de venta
                    }
                    
                    file = service.files().create(body=file_metadata,
                                                  media_body=media,
                                                  fields='id').execute()
                    
                    print(f"✅ Foto subida a Google Drive: {nombre_archivo_drive} (ID: {file.get('id')})")
                    # *** CAMBIO AQUÍ: Mensaje por cada foto ***
                    await update.message.reply_text(f"✔ Foto '{nombre_archivo_drive}' subida correctamente.")
                    total_fotos_subidas += 1

                except HttpError as http_error:
                    print(f"❌ Error HTTP al subir {nombre_archivo_drive}: {http_error}")
                    await update.message.reply_text(f"❌ Error al subir '{nombre_archivo_drive}' a Drive: {http_error.resp.status}. Revisar permisos.")
                except Exception as e:
                    print(f"❌ Error inesperado al subir {nombre_archivo_drive}: {e}")
                    await update.message.reply_text(f"❌ Error al subir '{nombre_archivo_drive}' a Drive: {e}")
                finally:
                    pass # No hay archivos temporales en disco que limpiar

        await update.message.reply_text(f"✔️ ¡Felicidades! Se subieron {total_fotos_subidas} fotos correctamente a Google Drive.")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ocurrió un error general al intentar subir las fotos: {str(e)[:150]}...")
        print(f"Error general en finalizar(): {e}")

    return ConversationHandler.END

# --- Funciones de Fallback ---

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancela la conversación actual."""
    await update.message.reply_text("❌ Proceso cancelado. Puedes iniciar de nuevo con /inicio.")
    return ConversationHandler.END

async def stop_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detiene el bot por completo (en Render, esto podría causar un reinicio)."""
    await update.message.reply_text("👋 Deteniendo el bot. ¡Hasta pronto!")
    await context.application.stop()
    await context.application.shutdown()
    return ConversationHandler.END

# --- Configuración y Ejecución del Bot ---

def main():
    """Configura y ejecuta la aplicación del bot de Telegram."""
    # El BOT_TOKEN se obtiene del entorno, pero si no está, usa un placeholder
    # Es crucial que BOT_TOKEN esté configurado en Render.
    if not BOT_TOKEN or BOT_TOKEN == "TU_BOT_TOKEN_POR_DEFECTO_SI_NO_ESTA_EN_ENV":
        print("🚨 ATENCIÓN: BOT_TOKEN no configurado en variables de entorno. El bot no podrá conectarse a Telegram.")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Configuración del ConversationHandler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("inicio", start)],
        states={
            PUNTO_VENTA: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_punto_venta)],
            CAJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_caja)],
            ACRILICO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_acrilico)],
            ENVIO_FOTOS: [MessageHandler(filters.PHOTO, recibir_foto)],
            CONFIRMACION: [
                CommandHandler("siguiente", siguiente),
                CommandHandler("acrilico", cambiar_acrilico),
                CommandHandler("finalizar", finalizar)
            ],
        },
        fallbacks=[
            CommandHandler("cancelar", cancelar),
            CommandHandler("stop", stop_bot_command)
        ]
    )

    # Agregar manejadores a la aplicación
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("stop", stop_bot_command)) # Manejador global para 'stop'

    print("🤖 Bot iniciado. Envíale /inicio desde Telegram.")
    
    # Iniciar la autenticación de Google Drive al arrancar el bot
    # Esto intentará obtener/refrescar las credenciales al inicio
    authenticate_google_drive_oauth_user()

    app.run_polling()
    print("Bot detenido.")

if __name__ == "__main__":
    main()
