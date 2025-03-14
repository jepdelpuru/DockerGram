import logging
import html
import paramiko
import asyncio
import nest_asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    CallbackQueryHandler, 
    ContextTypes
)
from telegram.error import BadRequest, RetryAfter

# Permitir anidar event loops (útil en algunos entornos)
nest_asyncio.apply()

# --- Configuración de logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

from config import SSH_HOST, SSH_PORT, SSH_USER, SSH_PASSWORD, DOCKER_PATH, BOT_TOKEN

# Variable global para la conexión SSH persistente
ssh_client = None

# Global para almacenar la información del panel principal
main_panel_chat_id = None
main_panel_message_id = None
main_panel_job = None   # <-- Variable global para el job de actualización

# Diccionario para almacenar los trabajos de actualización de logs
# La clave es (chat_id, cont_id) y el valor es el job
log_jobs = {}

# --- Funciones de conexión SSH y comandos Docker ---

def init_ssh():
    """Inicializa y establece una conexión SSH persistente sin usar ssh-agent."""
    global ssh_client
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh_client.connect(
            SSH_HOST,
            port=SSH_PORT,
            username=SSH_USER,
            password=SSH_PASSWORD,
            allow_agent=False,
            look_for_keys=False
        )
        logger.info("Conexión SSH establecida exitosamente.")
    except Exception as e:
        logger.error(f"Error al conectar vía SSH: {e}")

def ejecutar_comando_ssh(comando: str):
    """
    Ejecuta un comando vía la conexión SSH persistente y retorna (salida, error).
    """
    global ssh_client
    if ssh_client is None:
        init_ssh()
    try:
        stdin, stdout, stderr = ssh_client.exec_command(comando)
        salida = stdout.read().decode('utf-8')
        error = stderr.read().decode('utf-8')
        return salida, error
    except Exception as e:
        logger.error(f"Error al ejecutar el comando '{comando}': {e}")
        return "", str(e)

def listar_dockers_ssh():
    """
    Lista TODOS los contenedores (activos y detenidos) usando 'docker ps -a' vía SSH.
    Para cada contenedor se obtiene:
      - ID, Nombre y Status (de docker ps -a)
      - Si está corriendo ("Up"), se usa docker inspect para obtener el campo .State.StartedAt
        y se calcula el uptime (diferencia entre ahora y ese valor) y se formatea la hora de inicio.
      - Si está detenido, se obtiene también .State.StartedAt (última vez iniciado) y se muestra "Stopped" en uptime.
    """
    comando = f'{DOCKER_PATH} ps -a --format "{{{{.ID}}}}|{{{{.Names}}}}|{{{{.Status}}}}"'
    salida, error = ejecutar_comando_ssh(comando)
    if error:
        logger.error("Error al listar dockers: " + error)
        return []
    contenedores = []
    for linea in salida.splitlines():
        partes = linea.split("|")
        if len(partes) == 3:
            cont_id, nombre, status = partes
            started_at = "Unknown"
            uptime_str = "Unknown"
            try:
                inspect_cmd = f'{DOCKER_PATH} inspect -f "{{{{.State.StartedAt}}}}" {cont_id}'
                started_raw, err = ejecutar_comando_ssh(inspect_cmd)
                if not err and started_raw.strip():
                    started_time = datetime.fromisoformat(started_raw.strip())
                    started_at = started_time.strftime("%Y-%m-%d %H:%M:%S")
                    if "Up" in status:
                        now = datetime.now(started_time.tzinfo) if started_time.tzinfo else datetime.now()
                        delta = now - started_time
                        total_seconds = delta.total_seconds()
                        if total_seconds >= 3600:
                            hours = int(total_seconds // 3600)
                            uptime_str = f"{hours} hours ago"
                        else:
                            minutes = int(total_seconds // 60)
                            uptime_str = f"{minutes} minutes ago"
                    else:
                        uptime_str = "Stopped"
            except Exception as ex:
                logger.error(f"Error al procesar el contenedor {cont_id}: {ex}")
            icono = "🟢" if "Up" in status else "🔴"
            contenedores.append({
                "id": cont_id,
                "nombre": nombre,
                "status": status,
                "uptime": uptime_str,
                "start": started_at,
                "icono": icono
            })
    return contenedores

def reiniciar_docker_ssh(cont_id: str):
    """Reinicia el contenedor especificado vía SSH."""
    comando = f'{DOCKER_PATH} restart {cont_id}'
    salida, error = ejecutar_comando_ssh(comando)
    if error:
        return f"Error al reiniciar el contenedor {cont_id}: {error}"
    return f"Contenedor {cont_id} reiniciado: {salida}"

def obtener_log_ssh(cont_id: str, lineas=20):
    """Obtiene las últimas 'lineas' del log del contenedor vía SSH."""
    comando = f'{DOCKER_PATH} logs --tail {lineas} {cont_id}'
    salida, error = ejecutar_comando_ssh(comando)
    if error:
        return f"Error al obtener el log: {error}"
    return salida

def stop_docker_ssh(cont_id: str):
    """Detiene el contenedor especificado vía SSH."""
    comando = f'{DOCKER_PATH} stop {cont_id}'
    salida, error = ejecutar_comando_ssh(comando)
    if error:
        return f"Error al detener el contenedor {cont_id}: {error}"
    return f"Contenedor {cont_id} detenido: {salida}"

def start_docker_ssh(cont_id: str):
    """Inicia el contenedor especificado vía SSH."""
    comando = f'{DOCKER_PATH} start {cont_id}'
    salida, error = ejecutar_comando_ssh(comando)
    if error:
        return f"Error al iniciar el contenedor {cont_id}: {error}"
    return f"Contenedor {cont_id} iniciado: {salida}"

# --- Funciones del Panel Principal y Actualizaciones ---
async def construir_mensaje_principal() -> (str, InlineKeyboardMarkup):
    """
    Construye el panel principal con un formato más estructurado.
    Cada contenedor se muestra en dos líneas:
      [Icono][Nombre]
       • Uptime: ⏱️ <tiempo>
       • Start: 🕒 <fecha y hora de inicio>
    """
    contenedores = listar_dockers_ssh()
    mensaje = "📊 *Contenedores Activos* 📊\n\n"
    if not contenedores:
        mensaje += "⚠️ No se encontraron contenedores activos."
    else:
        for cont in contenedores:
            mensaje += f"{cont['icono']} *{cont['nombre']}*\n"
            mensaje += f"   • Uptime: ⏱️ {cont['uptime']}\n"
            mensaje += f"   • Start: 📅 {cont['start']}\n"
    hora_actual = datetime.now().strftime("%H:%M:%S")
    mensaje += f"\n_Actualizado a las {hora_actual}_"
    
    # Construir el teclado de botones para cada contenedor
    teclado = []
    for cont in contenedores:
        teclado.append([InlineKeyboardButton(f"{cont['icono']} {cont['nombre']}", callback_data=f"container_{cont['id']}")])
    
    # Agregar el botón para detener el panel principal
    teclado.append([InlineKeyboardButton("🛑 Parar bot", callback_data="stop_main_panel")])
    
    return mensaje, InlineKeyboardMarkup(teclado)

async def update_main_panel(context: ContextTypes.DEFAULT_TYPE):
    global main_panel_chat_id, main_panel_message_id, main_panel_job
    if main_panel_chat_id and main_panel_message_id:
        mensaje, teclado = await construir_mensaje_principal()
        try:
            await context.bot.edit_message_text(
                chat_id=main_panel_chat_id,
                message_id=main_panel_message_id,
                text=mensaje,
                reply_markup=teclado,
                parse_mode="Markdown"
            )
        except RetryAfter as e:
            logger.warning(f"Flood control exceeded. Retrying after {e.retry_after} seconds.")
            await asyncio.sleep(e.retry_after)
        except BadRequest as e:
            # Capturamos ambos mensajes de error: "Message to edit not found" y "Not found"
            if "Message to edit not found" in str(e) or "Not found" in str(e):
                logger.info("El mensaje a editar no se encuentra. Cancelando la actualización.")
                if main_panel_job:
                    main_panel_job.schedule_removal()
            elif "Message is not modified" in str(e):
                pass
            else:
                raise e

async def stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Detiene el contenedor seleccionado.
    Elimina el mensaje de acciones y envía una confirmación que se elimina automáticamente.
    """
    query = update.callback_query
    await query.answer()
    await query.delete_message()  # Elimina el mensaje de acciones
    cont_id = query.data.split("_")[1]
    contenedores = listar_dockers_ssh()
    container_info = next((cont for cont in contenedores if cont["id"] == cont_id), None)
    nombre = container_info["nombre"] if container_info else cont_id
    resultado = stop_docker_ssh(cont_id)
    confirmation_text = f"🛑 Contenedor {nombre} detenido correctamente."
    confirmation = await context.bot.send_message(
         chat_id=update.effective_chat.id, 
         text=confirmation_text
    )
    context.job_queue.run_once(
         lambda ctx: ctx.bot.delete_message(chat_id=update.effective_chat.id, message_id=confirmation.message_id),
         when=5
    )

async def start_container_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Inicia el contenedor seleccionado.
    Elimina el mensaje de acciones y envía una confirmación que se elimina automáticamente.
    """
    query = update.callback_query
    await query.answer()
    await query.delete_message()
    cont_id = query.data.split("_")[1]
    contenedores = listar_dockers_ssh()
    container_info = next((cont for cont in contenedores if cont["id"] == cont_id), None)
    nombre = container_info["nombre"] if container_info else cont_id
    resultado = start_docker_ssh(cont_id)
    confirmation_text = f"🚀 Contenedor {nombre} iniciado correctamente."
    confirmation = await context.bot.send_message(
         chat_id=update.effective_chat.id, 
         text=confirmation_text
    )
    context.job_queue.run_once(
         lambda ctx: ctx.bot.delete_message(chat_id=update.effective_chat.id, message_id=confirmation.message_id),
         when=5
    )

async def restart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Reinicia el contenedor seleccionado.
    Elimina el mensaje de acciones inmediatamente.
    Envía una confirmación con un emoji y el nombre del contenedor, y la elimina después de 5 segundos.
    """
    query = update.callback_query
    await query.answer()
    await query.delete_message()
    cont_id = query.data.split("_")[1]
    reinicio_resultado = reiniciar_docker_ssh(cont_id)
    contenedores = listar_dockers_ssh()
    container_info = next((cont for cont in contenedores if cont["id"] == cont_id), None)
    nombre = container_info["nombre"] if container_info else cont_id
    confirmation_text = f"✅ Contenedor *{nombre}* reiniciado correctamente."
    confirmation = await context.bot.send_message(
        chat_id=update.effective_chat.id, 
        text=confirmation_text
    )
    context.job_queue.run_once(
        lambda ctx: ctx.bot.delete_message(chat_id=update.effective_chat.id, message_id=confirmation.message_id),
        when=5
    )

async def update_log(context: ContextTypes.DEFAULT_TYPE):
    """
    Actualiza el mensaje de log del contenedor cada 10 segundos.
    Se incluye un botón "🗑 Eliminar" para detener el seguimiento.
    """
    job_data = context.job.data
    chat_id = job_data['chat_id']
    message_id = job_data['message_id']
    cont_id = job_data['cont_id']
    raw_log = obtener_log_ssh(cont_id)
    max_chars = 4000
    if len(raw_log) > max_chars:
        raw_log = raw_log[-max_chars:]
    import html
    log_text = html.escape(raw_log)
    botones = [[InlineKeyboardButton("🗑 Eliminar", callback_data=f"deleteLog_{cont_id}")]]
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"Log del contenedor {cont_id}:\n\n<pre>{log_text}</pre>",
            reply_markup=InlineKeyboardMarkup(botones),
            parse_mode="HTML"
        )
    except RetryAfter as e:
        logger.warning(f"Flood control exceeded in update_log. Retrying after {e.retry_after} seconds.")
        await asyncio.sleep(e.retry_after)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise e

async def log_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Al pulsar "Ver log", elimina el mensaje de acciones y envía un mensaje con el log que se actualiza cada 10 segundos.
    """
    query = update.callback_query
    await query.answer()
    await query.delete_message()
    cont_id = query.data.split("_")[1]
    raw_log = obtener_log_ssh(cont_id)
    max_chars = 4000
    if len(raw_log) > max_chars:
        raw_log = raw_log[-max_chars:]
    log_text = html.escape(raw_log)
    botones = [[InlineKeyboardButton("🗑 Eliminar", callback_data=f"deleteLog_{cont_id}")]]
    chat_id = update.effective_chat.id
    sent_log_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"Log del contenedor {cont_id}:\n\n<pre>{log_text}</pre>",
        reply_markup=InlineKeyboardMarkup(botones),
        parse_mode="HTML"
    )
    job = context.job_queue.run_repeating(
        update_log, interval=10, first=0,
        data={'chat_id': chat_id, 'message_id': sent_log_msg.message_id, 'cont_id': cont_id}
    )
    log_jobs[(chat_id, cont_id)] = job

async def delete_log_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Al pulsar el botón "🗑 Eliminar" en el mensaje de log,
    se cancela el job de actualización y se elimina el mensaje.
    """
    query = update.callback_query
    await query.answer("Seguimiento de log detenido")
    cont_id = query.data.split("_")[1]
    chat_id = update.effective_chat.id
    key = (chat_id, cont_id)
    if key in log_jobs:
        job = log_jobs.pop(key)
        job.schedule_removal()
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
    except Exception as e:
        logger.error("Error al eliminar el mensaje de log: " + str(e))

# Nuevo handler para detener el panel principal
async def stop_main_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Al pulsar el botón "🛑 Parar bot" en el panel principal,
    se cancela el job de actualización y se elimina el mensaje.
    """
    global main_panel_chat_id, main_panel_message_id, main_panel_job
    query = update.callback_query
    await query.answer("Actualización detenida")
    if main_panel_job:
        main_panel_job.schedule_removal()
        main_panel_job = None
    try:
        await context.bot.delete_message(
            chat_id=main_panel_chat_id, 
            message_id=main_panel_message_id
        )
    except Exception as e:
        logger.error("Error al eliminar el panel principal: " + str(e))
    # Reiniciar las variables globales
    main_panel_chat_id = None
    main_panel_message_id = None

# --- Handlers del Bot ---
MY_CHAT_ID = 6501204809

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Comando /start: Envía el panel principal y programa su actualización.
    """
    if update.effective_chat.id != MY_CHAT_ID:
        # Opcional: enviar mensaje de no autorizado o simplemente no hacer nada
        await update.message.reply_text("No estás autorizado para usar este bot.")
        return
    global main_panel_chat_id, main_panel_message_id, main_panel_job
    mensaje, teclado = await construir_mensaje_principal()
    sent_msg = await update.message.reply_text(
        text=mensaje, 
        reply_markup=teclado, 
        parse_mode="Markdown"
    )
    main_panel_chat_id = sent_msg.chat.id
    main_panel_message_id = sent_msg.message_id
    # Programa la actualización del panel principal cada 10 segundos y guarda el job
    main_panel_job = context.job_queue.run_repeating(
        update_main_panel, interval=10, first=0,
        data={'chat_id': main_panel_chat_id, 'message_id': main_panel_message_id}
    )
    # Borra el mensaje original del comando /start para dejar solo el panel principal
    try:
        await update.message.delete()
    except Exception as e:
        logger.error("Error al borrar el mensaje /start: %s", e)


async def container_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Al pulsar un botón del panel principal de un contenedor,
    se envía un mensaje nuevo debajo con botones de "Reiniciar", "Ver log", "Parar" e "Iniciar".
    """
    query = update.callback_query
    await query.answer()
    cont_id = query.data.split("_")[1]
    contenedores = listar_dockers_ssh()
    docker_info = next((c for c in contenedores if c['id'] == cont_id), None)
    if not docker_info:
        await query.message.reply_text("No se encontró información para ese contenedor.")
        return
    botones = [
       [
         InlineKeyboardButton("🔄 Reiniciar", callback_data=f"restart_{cont_id}"),
         InlineKeyboardButton("📜 Ver log", callback_data=f"log_{cont_id}")
       ],
       [
         InlineKeyboardButton("🛑 Parar", callback_data=f"stop_{cont_id}"),
         InlineKeyboardButton("🚀 Iniciar", callback_data=f"start_{cont_id}")
       ]
    ]
    await query.message.reply_text(
         text=f"Acciones para {docker_info['nombre']}:",
         reply_markup=InlineKeyboardMarkup(botones)
    )

async def restart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Reinicia el contenedor seleccionado.
    """
    query = update.callback_query
    await query.answer()
    await query.delete_message()
    cont_id = query.data.split("_")[1]
    reinicio_resultado = reiniciar_docker_ssh(cont_id)
    contenedores = listar_dockers_ssh()
    container_info = next((cont for cont in contenedores if cont["id"] == cont_id), None)
    nombre = container_info["nombre"] if container_info else cont_id
    confirmation_text = f"✅ Contenedor *{nombre}* reiniciado correctamente."
    confirmation = await context.bot.send_message(
        chat_id=update.effective_chat.id, 
        text=confirmation_text
    )
    context.job_queue.run_once(
        lambda ctx: ctx.bot.delete_message(chat_id=update.effective_chat.id, message_id=confirmation.message_id),
        when=5
    )

# --- Función principal y ejecución del Bot ---
async def main():
    init_ssh()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(stop_callback, pattern="^stop_"))
    app.add_handler(CallbackQueryHandler(start_container_callback, pattern="^start_"))
    app.add_handler(CallbackQueryHandler(container_callback, pattern="^container_"))
    app.add_handler(CallbackQueryHandler(restart_callback, pattern="^restart_"))
    app.add_handler(CallbackQueryHandler(log_callback, pattern="^log_"))
    app.add_handler(CallbackQueryHandler(delete_log_callback, pattern="^deleteLog_"))
    # Handler para detener el panel principal
    app.add_handler(CallbackQueryHandler(stop_main_panel, pattern="^stop_main_panel$"))
    await app.run_polling()
    if ssh_client:
        ssh_client.close()
        logger.info("Conexión SSH cerrada.")

if __name__ == '__main__':
    asyncio.run(main())
