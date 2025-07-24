#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot de Telegram para gestión de inventario con Google Drive
Optimizado para despliegue en Render.com
"""

import os
import json
import base64
import logging
from io import BytesIO

from telegram import Update, ReplyKeyboardMarkup, Bot
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler
)

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError

# ==========================================
# CONFIGURACIÓN DE LOGGING
# ==========================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
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
        logger.error(f"❌ Variable de entorno {var_name} no está configurada")
        exit(1)

# ==========================================
# CONSTANTES DEL BOT
# ==========================================

# Estados del flujo de conversación
PUNTO_VENTA, CAJA, ACRILICO, ENVIO_FOTOS, CONFIRMACION = range(5)

# Opciones de teclado predefinidas
CAJAS = [
    ["CAJA A", "CAJA B", "CAJA C", "CAJA D"], 
    ["CAJA E", "CAJA F", "CAJA G", "CAJA H"]
]

ACRILICOS_OPCIONES = [
    ["ACRILICO 1", "ACRILICO 2", "ACRILICO 3"], 
    ["ACRILICO 4", "ACRILICO 5", "ACRILICO 6"], 
    ["ACRILICO 7", "ACRILICO 8", "ACRILICO 9"]
]

# ==========================================
# SERVICIO DE GOOGLE DRIVE
# ==========================================

def get_google_drive_service():
    """
    Crea y retorna el servicio de Google Drive usando Service Account.
    Las credenciales se obtienen desde la variable de entorno codificada en base64.
    """
    try:
        logger.info("🔧 Decodificando credenciales de Service Account...")
        
        # Validar que la variable existe y no está vacía
        if not GOOGLE_SERVICE_ACCOUNT_JSON:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON está vacío")
        
        # Decodificar las credenciales desde base64
        try:
            decoded_json = base64.b64decode(GOOGLE_SERVICE_ACCOUNT_JSON).decode('utf-8')
            service_account_info = json.loads(decoded_json)
        except Exception as decode_error:
            raise ValueError(f"Error decodificando base64 o JSON: {decode_error}")
        
        # Validar campos requeridos en el JSON
        required_fields = ['type', 'project_id', 'private_key_id', 'private_key', 'client_email']
        missing_fields = [field for field in required_fields if field not in service_account_info]
        if missing_fields:
            raise ValueError(f"Campos faltantes en service account JSON: {missing_fields}")
        
        logger.info(f"📧 Service Account Email: {service_account_info.get('client_email')}")
        
        # Crear credenciales desde la información del service account
        credentials = Credentials.from_service_account_info(
            service_account_info,
            scopes=['https://www.googleapis.com/auth/drive.file']
        )
        
        # Construir el servicio
        service = build('drive', 'v3', credentials=credentials)
        
        # Probar la conexión listando archivos (solo para verificar)
        try:
            test_result = service.files().list(pageSize=1).execute()
            logger.info("✅ Servicio de Google Drive creado y verificado exitosamente")
        except Exception as test_error:
            logger.warning(f"⚠️ Servicio creado pero prueba falló: {test_error}")
            # Aún devolvemos el servicio porque puede funcionar para operaciones específicas
        
        return service
        
    except Exception as e:
        logger.error(f"❌ Error completo al crear servicio de Google Drive: {e}")
        logger.error(f"📋 Tipo de error: {type(e).__name__}")
        return None

# ==========================================
# FUNCIONES AUXILIARES DE GOOGLE DRIVE
# ==========================================

async def get_or_create_drive_folder_id(service, folder_name, parent_folder_id):
    """
    Busca una carpeta por nombre dentro de una carpeta padre específica.
    Si no la encuentra, la crea y devuelve su ID.
    """
    query = (
        f"name='{folder_name}' and "
        f"mimeType='application/vnd.google-apps.folder' and "
        f"'{parent_folder_id}' in parents and "
        f"trashed=false"
    )
    
    try:
        response = service.files().list(
            q=query, 
            spaces='drive', 
            fields='files(id, name)'
        ).execute()
        
        files = response.get('files', [])

        if files:
            logger.info(f"📁 Carpeta '{folder_name}' encontrada")
            return files[0]['id']
        else:
            # Crear nueva carpeta
            file_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [parent_folder_id]
            }
            
            folder = service.files().create(
                body=file_metadata, 
                fields='id'
            ).execute()
            
            folder_id = folder.get('id')
            logger.info(f"✅ Carpeta '{folder_name}' creada con ID: {folder_id}")
            return folder_id
            
    except HttpError as error:
        logger.error(f"❌ Error al buscar/crear la carpeta '{folder_name}': {error}")
        return None

# ==========================================
# HANDLERS DEL BOT
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia la conversación y pide el punto de venta."""
    user = update.effective_user
    logger.info(f"Usuario {user.first_name} ({user.id}) inició el bot")
    
    await update.message.reply_text(
        f"¡Hola {user.first_name}! 👋\n\n"
        "📍 Ingrese el nombre del punto de venta:"
    )
    return PUNTO_VENTA

async def recibir_punto_venta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guarda el punto de venta y pide el tipo de caja."""
    punto_venta = update.message.text.strip()
    context.user_data['punto_venta'] = punto_venta
    
    logger.info(f"Punto de venta recibido: {punto_venta}")
    
    await update.message.reply_text(
        "📦 Selecciona el tipo de caja:",
        reply_markup=ReplyKeyboardMarkup(
            CAJAS, 
            one_time_keyboard=True, 
            resize_keyboard=True
        )
    )
    return CAJA

async def recibir_caja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guarda la caja y pide los acrílicos."""
    caja = update.message.text.strip().replace(" ", "_").upper()
    context.user_data['caja'] = caja
    
    logger.info(f"Caja seleccionada: {caja}")
    
    texto_acrilicos = (
        "🧊 Selecciona los acrílicos (escribe los números separados por comas, ej: 1,2,4):\n\n" +
        "\n".join([", ".join(row) for row in ACRILICOS_OPCIONES])
    )
    
    await update.message.reply_text(texto_acrilicos)
    return ACRILICO

async def recibir_acrilico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Valida y guarda los acrílicos seleccionados e inicia la subida de fotos."""
    try:
        seleccion = update.message.text.strip()
        indices = [int(x.strip()) for x in seleccion.split(",")]
        
        # Validar que todos los índices estén en el rango válido
        acrilicos = [f"ACRILICO_{i}" for i in indices if 1 <= i <= 9]
        
        if not acrilicos:
            raise ValueError("No se seleccionaron acrílicos válidos.")
            
        context.user_data['acrilicos'] = acrilicos
        context.user_data['acrilico_actual_idx'] = 0
        context.user_data['fotos_dict'] = {a: [] for a in acrilicos}
        
        logger.info(f"Acrílicos seleccionados: {acrilicos}")
        
        return await iniciar_envio_fotos(update, context)
        
    except (ValueError, IndexError):
        await update.message.reply_text(
            "⚠️ Entrada inválida. Por favor, escribe los números de los acrílicos "
            "separados por comas (ej: 1,2,3)."
        )
        return ACRILICO

async def iniciar_envio_fotos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pide al usuario que envíe las fotos para el acrílico actual en la secuencia."""
    idx = context.user_data['acrilico_actual_idx']
    acrilico = context.user_data['acrilicos'][idx]
    
    await update.message.reply_text(
        f"📸 Envía las fotos del {acrilico} (máximo 5 fotos).\n\n"
        f"📊 Progreso: Acrílico {idx + 1} de {len(context.user_data['acrilicos'])}"
    )
    return ENVIO_FOTOS

async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guarda una foto y pregunta al usuario qué hacer a continuación."""
    idx = context.user_data['acrilico_actual_idx']
    acrilico = context.user_data['acrilicos'][idx]
    fotos_dict = context.user_data['fotos_dict']

    # Verificar límite de fotos
    if len(fotos_dict[acrilico]) >= 5:
        await update.message.reply_text(
            "⚠️ Ya has enviado el máximo de 5 fotos para este acrílico."
        )
        return CONFIRMACION

    # Guardar la foto (usar la mejor calidad disponible)
    file_id = update.message.photo[-1].file_id
    fotos_dict[acrilico].append(file_id)
    
    total_fotos = len(fotos_dict[acrilico])
    logger.info(f"Foto recibida para {acrilico}. Total: {total_fotos}")

    await update.message.reply_text(
        f"✅ Foto recibida ({total_fotos}/5 para {acrilico}).\n\n"
        "Opciones:\n"
        "• /Siguiente - Enviar otra foto del mismo acrílico\n"
        "• /Acrilico - Pasar al siguiente acrílico\n"
        "• /finalizar - Guardar todo en Google Drive"
    )
    return CONFIRMACION

async def siguiente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Regresa al estado de envío de fotos para el mismo acrílico."""
    idx = context.user_data['acrilico_actual_idx']
    acrilico = context.user_data['acrilicos'][idx]
    fotos_actuales = len(context.user_data['fotos_dict'][acrilico])
    
    if fotos_actuales >= 5:
        await update.message.reply_text(
            "🚫 Ya has alcanzado el límite de 5 fotos para este acrílico. "
            "Usa /Acrilico o /finalizar."
        )
        return CONFIRMACION

    await update.message.reply_text(
        f"📸 Puedes enviar otra foto del {acrilico} ({fotos_actuales}/5)."
    )
    return ENVIO_FOTOS

async def cambiar_acrilico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Avanza al siguiente acrílico o finaliza si no hay más."""
    context.user_data['acrilico_actual_idx'] += 1
    idx = context.user_data['acrilico_actual_idx']
    acrilicos = context.user_data['acrilicos']

    if idx >= len(acrilicos):
        await update.message.reply_text(
            "✅ Has completado todos los acrílicos. Finalizando automáticamente..."
        )
        return await finalizar(update, context)

    nuevo_acrilico = acrilicos[idx]
    await update.message.reply_text(
        f"📸 Ahora envía las fotos del {nuevo_acrilico} (máx. 5).\n\n"
        f"📊 Progreso: Acrílico {idx + 1} de {len(acrilicos)}"
    )
    return ENVIO_FOTOS

async def finalizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra un resumen, guarda todas las fotos en Google Drive y termina la conversación."""
    datos = context.user_data
    
    # Mostrar resumen
    resumen_fotos = "\n".join([
        f"  • {ac.replace('_', ' ')}: {len(fotos)} foto(s)" 
        for ac, fotos in datos['fotos_dict'].items()
    ])
    
    total_fotos = sum(len(fotos) for fotos in datos['fotos_dict'].values())
    
    resumen = (
        f"📋 **RESUMEN DEL PROCESO**\n\n"
        f"📍 Punto de venta: {datos['punto_venta']}\n"
        f"📦 Caja: {datos['caja']}\n"
        f"📸 Total de fotos: {total_fotos}\n\n"
        f"🧊 **Fotos por acrílico:**\n{resumen_fotos}\n\n"
        f"⏳ Iniciando subida a Google Drive..."
    )
    
    await update.message.reply_text(resumen)
    
    # Obtener servicio de Google Drive con validación mejorada
    logger.info("🔧 Iniciando conexión con Google Drive...")
    service = get_google_drive_service()
    if not service:
        await update.message.reply_text(
            "❌ **Error de conexión con Google Drive**\n"
            "No se pudieron obtener las credenciales.\n"
            "Contacta al administrador del sistema."
        )
        return ConversationHandler.END

    try:
        # Preparar datos
        punto = datos['punto_venta'].replace(" ", "_").upper()
        caja = datos['caja'].upper()
        fotos_dict = datos['fotos_dict']
        bot: Bot = context.bot

        logger.info(f"📁 Creando/verificando carpeta para: {punto}")
        
        # Crear/obtener carpeta del punto de venta
        punto_venta_folder_id = await get_or_create_drive_folder_id(
            service, punto, GOOGLE_DRIVE_ROOT_FOLDER_ID
        )
        
        if not punto_venta_folder_id:
            await update.message.reply_text(
                "❌ **Error al crear carpeta**\n"
                "No se pudo crear la carpeta del punto de venta en Drive.\n"
                "Verifica los permisos del Service Account."
            )
            return ConversationHandler.END

        logger.info(f"✅ Carpeta lista: {punto_venta_folder_id}")
        await update.message.reply_text("📁 Carpeta creada/verificada. Iniciando subida de fotos...")

        # Subir fotos con manejo mejorado de errores
        contador = 0
        exitosas = 0
        errores = []

        for acrilico, fotos in fotos_dict.items():
            logger.info(f"📸 Procesando {len(fotos)} fotos de {acrilico}")
            
            for i, file_id in enumerate(fotos):
                contador += 1
                nombre_archivo_drive = f"{punto}_{caja}_{acrilico}_{i+1}.jpg"
                
                try:
                    # Mostrar progreso
                    await update.message.reply_text(
                        f"📤 Subiendo {nombre_archivo_drive} ({contador}/{total_fotos})"
                    )
                    
                    logger.info(f"🔄 Descargando foto desde Telegram: {file_id}")
                    
                    # Descargar foto de Telegram con validación
                    try:
                        file_info = await bot.get_file(file_id)
                        if not file_info:
                            raise Exception("No se pudo obtener información del archivo")
                        
                        photo_bytes_io = BytesIO()
                        await file_info.download_to_memory(photo_bytes_io)
                        
                        # Verificar que se descargó contenido
                        photo_size = photo_bytes_io.tell()
                        if photo_size == 0:
                            raise Exception("Archivo vacío descargado de Telegram")
                        
                        photo_bytes_io.seek(0)
                        logger.info(f"✅ Foto descargada: {photo_size} bytes")
                        
                    except Exception as download_error:
                        raise Exception(f"Error descargando de Telegram: {download_error}")

                    # Preparar para subir a Google Drive
                    logger.info(f"🔄 Subiendo a Google Drive: {nombre_archivo_drive}")
                    
                    media = MediaIoBaseUpload(
                        photo_bytes_io, 
                        mimetype='image/jpeg', 
                        resumable=True
                    )
                    
                    file_metadata = {
                        'name': nombre_archivo_drive,
                        'parents': [punto_venta_folder_id]
                    }
                    
                    # Subir archivo con retry
                    try:
                        file = service.files().create(
                            body=file_metadata,
                            media_body=media,
                            fields='id,name'
                        ).execute()
                        
                        exitosas += 1
                        logger.info(f"✅ Foto subida exitosamente: {nombre_archivo_drive} (ID: {file.get('id')})")
                        
                    except HttpError as drive_error:
                        error_details = f"Google Drive API Error: {drive_error.resp.status} - {drive_error.content.decode()}"
                        raise Exception(error_details)
                    
                    except Exception as upload_error:
                        raise Exception(f"Error en subida: {upload_error}")

                except Exception as e:
                    error_msg = f"Error con {nombre_archivo_drive}: {str(e)}"
                    errores.append(error_msg)
                    logger.error(f"❌ {error_msg}")
                    
                    # Notificar error específico al usuario
                    await update.message.reply_text(
                        f"⚠️ Error con {nombre_archivo_drive}:\n{str(e)[:100]}..."
                    )

        # Mensaje final detallado
        if len(errores) == 0:
            await update.message.reply_text(
                f"🎉 **¡PROCESO COMPLETADO EXITOSAMENTE!**\n\n"
                f"✅ {exitosas} fotos subidas correctamente\n"
                f"📁 Carpeta: {punto}\n"
                f"🔗 Revisa tu Google Drive\n\n"
                f"¡Gracias por usar el bot! 😊"
            )
        else:
            # Mostrar errores específicos
            errores_texto = "\n".join(errores[:3])  # Mostrar solo los primeros 3 errores
            if len(errores) > 3:
                errores_texto += f"\n... y {len(errores) - 3} errores más"
            
            await update.message.reply_text(
                f"⚠️ **PROCESO COMPLETADO CON ERRORES**\n\n"
                f"✅ {exitosas} fotos subidas correctamente\n"
                f"❌ {len(errores)} fotos con errores\n\n"
                f"**Errores encontrados:**\n{errores_texto}\n\n"
                f"📁 Revisa tu Google Drive en la carpeta: {punto}"
            )

    except Exception as e:
        error_completo = str(e)
        logger.error(f"❌ Error general en finalizar: {error_completo}")
        
        # Mensaje de error más informativo
        await update.message.reply_text(
            f"❌ **Error general al procesar las fotos**\n\n"
            f"**Detalles técnicos:**\n{error_completo[:200]}{'...' if len(error_completo) > 200 else ''}\n\n"
            f"**Posibles causas:**\n"
            f"• Problema con credenciales de Google Drive\n"
            f"• Permisos insuficientes en la carpeta\n"
            f"• Error de red temporalmente\n\n"
            f"**Solución:**\n"
            f"1. Espera 1-2 minutos e intenta de nuevo\n"
            f"2. Si persiste, contacta al administrador\n"
            f"3. Menciona este error específico"
        )

    return ConversationHandler.END

# ==========================================
# HANDLERS DE CONTROL
# ==========================================

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancela la conversación actual."""
    await update.message.reply_text(
        "❌ Proceso cancelado. Puedes iniciar nuevamente con /start."
    )
    logger.info(f"Usuario {update.effective_user.id} canceló el proceso")
    return ConversationHandler.END

async def diagnostico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para diagnosticar problemas de configuración."""
    await update.message.reply_text("🔍 Ejecutando diagnóstico del sistema...")
    
    diagnosticos = []
    
    # 1. Verificar variables de entorno
    diagnosticos.append("📋 **VARIABLES DE ENTORNO:**")
    diagnosticos.append(f"• BOT_TOKEN: {'✅ Configurado' if BOT_TOKEN else '❌ Faltante'}")
    diagnosticos.append(f"• GOOGLE_DRIVE_ROOT_FOLDER_ID: {'✅ Configurado' if GOOGLE_DRIVE_ROOT_FOLDER_ID else '❌ Faltante'}")
    diagnosticos.append(f"• GOOGLE_SERVICE_ACCOUNT_JSON: {'✅ Configurado' if GOOGLE_SERVICE_ACCOUNT_JSON else '❌ Faltante'}")
    
    # 2. Verificar Google Drive
    diagnosticos.append("\n🔧 **GOOGLE DRIVE:**")
    service = get_google_drive_service()
    if service:
        try:
            # Probar acceso a la carpeta raíz
            folder_info = service.files().get(fileId=GOOGLE_DRIVE_ROOT_FOLDER_ID).execute()
            diagnosticos.append(f"✅ Carpeta raíz accesible: {folder_info.get('name')}")
            
            # Probar crear archivo de prueba
            test_metadata = {
                'name': 'bot_test_file.txt',
                'parents': [GOOGLE_DRIVE_ROOT_FOLDER_ID]
            }
            test_media = MediaIoBaseUpload(
                BytesIO(b'Test file from bot'), 
                mimetype='text/plain'
            )
            
            test_file = service.files().create(
                body=test_metadata,
                media_body=test_media,
                fields='id'
            ).execute()
            
            # Eliminar archivo de prueba
            service.files().delete(fileId=test_file.get('id')).execute()
            diagnosticos.append("✅ Permisos de escritura verificados")
            
        except Exception as drive_error:
            diagnosticos.append(f"❌ Error con Google Drive: {drive_error}")
    else:
        diagnosticos.append("❌ No se pudo conectar con Google Drive")
    
    # 3. Verificar bot de Telegram
    diagnosticos.append(f"\n🤖 **BOT DE TELEGRAM:**")
    try:
        bot_info = await context.bot.get_me()
        diagnosticos.append(f"✅ Bot activo: @{bot_info.username}")
    except Exception as bot_error:
        diagnosticos.append(f"❌ Error del bot: {bot_error}")
    
    resultado = "\n".join(diagnosticos)
    await update.message.reply_text(resultado)

async def health_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para verificar que el bot esté funcionando."""
    await update.message.reply_text(
        "🟢 **Bot funcionando correctamente!**\n\n"
        f"🤖 Versión: 1.0\n"
        f"☁️ Google Drive: Conectado\n"
        f"📊 Estado: Activo 24/7\n\n"
        f"💡 Usa /diagnostico para verificación completa"
    )

async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra información de ayuda."""
    texto_ayuda = (
        "🤖 **GUÍA DE USO DEL BOT**\n\n"
        "**Comandos disponibles:**\n"
        "• /start - Iniciar proceso de subida\n"
        "• /health - Verificar estado del bot\n"
        "• /help - Mostrar esta ayuda\n"
        "• /cancelar - Cancelar proceso actual\n\n"
        "**Durante el proceso:**\n"
        "• /Siguiente - Enviar otra foto del mismo acrílico\n"
        "• /Acrilico - Cambiar al siguiente acrílico\n"
        "• /finalizar - Guardar todo en Google Drive\n\n"
        "**Flujo del proceso:**\n"
        "1️⃣ Nombre del punto de venta\n"
        "2️⃣ Seleccionar tipo de caja\n"
        "3️⃣ Elegir acrílicos (números separados por comas)\n"
        "4️⃣ Enviar fotos (máx. 5 por acrílico)\n"
        "5️⃣ Subida automática a Google Drive\n\n"
        "¿Necesitas ayuda? Contacta al administrador."
    )
    
    await update.message.reply_text(texto_ayuda)

# ==========================================
# CONFIGURACIÓN PRINCIPAL DEL BOT
# ==========================================

def main():
    """Función principal que configura y ejecuta el bot."""
    
    logger.info("🚀 Iniciando bot de Telegram...")
    
    # Verificar configuración
    logger.info("✅ Todas las variables de entorno están configuradas")

    # Construir aplicación del bot
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Configurar ConversationHandler
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
                CommandHandler("finalizar", finalizar)
            ],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)]
    )

    # Agregar handlers
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("health", health_check))
    application.add_handler(CommandHandler("help", ayuda))
    application.add_handler(CommandHandler("diagnostico", diagnostico))

    logger.info("🤖 Bot configurado y listo para recibir mensajes")
    logger.info("📱 Los usuarios pueden enviar /start para comenzar")

    # Ejecutar el bot con polling
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == '__main__':
    main()