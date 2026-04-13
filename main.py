import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import random
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, JobQueue

load_dotenv()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_DATA: Dict[str, Any] = {"users": [], "last_median": None}

# Lista de modelos OpenRouter gratuitos actuales (ordenados por calidad/capacidad)
OPENROUTER_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "qwen/qwen3-coder:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-3-27b-it:free",
    "minimax/minimax-m2.5:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-3n-e4b-it:free",
    "google/gemma-3n-e2b-it:free",
    "google/gemma-3-12b-it:free",
    "z-ai/glm-4.5-air:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
    "openai/gpt-oss-120b:free",
    "openai/gpt-oss-20b:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "google/gemma-3-4b-it:free",
    "arcee-ai/trinity-large-preview:free",
    "liquid/lfm-2.5-1.2b-instruct:free",
    "liquid/lfm-2.5-1.2b-thinking:free",
]

FALLBACK_REPLIES = [
    "Qué quieres?",
    "Estás aburrido?",
    "Déjame tranquilo",
    "Déjame vivir",
    "Deja la muela",
    "Corta la wara",
    "No me molestes",
    "brother...",
    "No tienes chamba?",
    "Ya salió el preguntón",
    "No inventes",
    "Anda pa'llá bobo",
    "Déjame en paz",
    "Vete a escuchar al Ferxxo",
    "No me jodas",
    "Búscate algo que hacer",
    "Esto no funciona asi",
    "Qué pesado eres",
    "Corta ya",
    "Tú estás aburrido o qué?",
    "Qué bolá contigo?",
    "Déjame con mi vida",
    "No me vengas con cuentos",
    "Tú sí eres especial",
    "Ya, suéltame"
]


class JsonStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()

    async def _read(self) -> Dict[str, Any]:
        def _read_sync() -> Dict[str, Any]:
            if not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("w", encoding="utf-8") as handle:
                    json.dump(DEFAULT_DATA, handle, indent=2)
            with self.path.open("r", encoding="utf-8") as handle:
                return json.load(handle)

        return await asyncio.to_thread(_read_sync)

    async def _write(self, data: Dict[str, Any]) -> None:
        def _write_sync() -> None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)

        await asyncio.to_thread(_write_sync)

    async def add_user(self, user_id: int) -> bool:
        async with self.lock:
            data = await self._read()
            if user_id in data.get("users", []):
                return False
            data.setdefault("users", []).append(user_id)
            await self._write(data)
            return True

    async def remove_user(self, user_id: int) -> bool:
        async with self.lock:
            data = await self._read()
            users: List[int] = data.get("users", [])
            if user_id not in users:
                return False
            data["users"] = [u for u in users if u != user_id]
            await self._write(data)
            return True

    async def list_users(self) -> List[int]:
        async with self.lock:
            data = await self._read()
            return list(data.get("users", []))

    async def get_last_median(self) -> Optional[float]:
        async with self.lock:
            data = await self._read()
            median = data.get("last_median")
            return float(median) if median is not None else None

    async def set_last_median(self, value: float) -> None:
        async with self.lock:
            data = await self._read()
            data["last_median"] = value
            await self._write(data)


def format_cup(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def build_settings() -> Dict[str, Any]:
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not telegram_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN es obligatorio")

    cubanomic_url = os.getenv("CUBANOMIC_URL")
    if not cubanomic_url:
        raise RuntimeError("CUBANOMIC_URL es obligatorio en el archivo .env")

    # Modelo primario desde .env, con fallback a la lista de modelos
    primary_model = os.getenv("OPENROUTER_MODEL", "stepfun/step-3.5-flash:free")
    model_list = [primary_model] + [m for m in OPENROUTER_MODELS if m != primary_model]

    return {
        "telegram_token": telegram_token,
        "openrouter_api_key": os.getenv("OPENROUTER_API_KEY", ""),
        "openrouter_models": model_list,
        "poll_seconds": int(os.getenv("POLL_SECONDS", "60")),
        "store_path": Path(os.getenv("STORE_PATH", "data/store.json")),
        "cubanomic_url": cubanomic_url,
    }


async def fetch_current_median(client: httpx.AsyncClient, url: str) -> float:
    response = await client.get(url, timeout=10)
    response.raise_for_status()
    payload = response.json()
    try:
        usd_cup = payload["statistics"]["USD.CUP"]
        if "median" not in usd_cup:
            raise ValueError("El campo 'median' no existe en la respuesta")
        return float(usd_cup["median"])
    except (KeyError, TypeError):
        raise ValueError("Respuesta inesperada de Cubanomic")


async def generate_openrouter_message(
        client: httpx.AsyncClient,
        api_key: str,
        models: List[str],
        previous: float,
        current: float,
) -> str:
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY es obligatorio para IA")

    content = (
        "Eres un analista financiero cubano con sentido del humor. "
        "Usas frecuentemente palabras como \"asere\" . "
        f"El valor del USD respecto al peso cubano ha cambiado de {format_cup(previous)} "
        f"CUP a {format_cup(current)} CUP. Redacta un mensaje muy breve para Telegram "
        "informando esto con un tono sarcástico y cómico e informal."
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error: Optional[Exception] = None
    for model in models:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
        }
        try:
            response = await client.post(OPENROUTER_URL, json=payload, headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices") or []
            if choices:
                message = choices[0].get("message", {}).get("content")
                if message:
                    return message.strip()
            raise ValueError("Respuesta de OpenRouter sin contenido utilizable")
        except Exception as exc:  # noqa: BLE001
            logging.warning("Modelo %s falló: %s", model, exc)
            last_error = exc
            continue

    raise RuntimeError(
        f"Todos los modelos fallaron. Último error: {last_error}"
    )


async def broadcast_message(application: Application, user_ids: List[int], text: str) -> None:
    for user_id in user_ids:
        try:
            await application.bot.send_message(chat_id=user_id, text=text)
        except Exception as exc:  # noqa: BLE001
            logging.warning("No se pudo enviar mensaje a %s: %s", user_id, exc)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    store: JsonStore = context.application.bot_data["store"]
    added = await store.add_user(update.effective_user.id)
    greeting = (
        "Asere, bienvenido. Soy Toquencio y te enviaré notificaciones en tiempo real "
        "cada vez que cambie el precio del USD en el Toque.\n\n"
        "Comandos disponibles:\n"
        "/start - Suscribirse a las notificaciones\n"
        "/stop - Darse de baja de la lista\n"
        "/status - Ver el último precio registrado"
        if added
        else "Consorte, ya estabas en la lista. Mantente en sintonía."
    )
    await update.message.reply_text(greeting)


async def handle_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    store: JsonStore = context.application.bot_data["store"]
    removed = await store.remove_user(update.effective_user.id)
    text = "Te sacamos de la lista. Suerte con el cambio." if removed else "No estabas suscrito, asere."
    await update.message.reply_text(text)


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: JsonStore = context.application.bot_data["store"]
    last = await store.get_last_median()
    if last is None:
        msg = "Aún no tenemos datos. Espera al próximo chequeo."
    else:
        msg = f"Último valor registrado: {format_cup(last)} CUP."
    await update.message.reply_text(msg)


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(random.choice(FALLBACK_REPLIES))


async def poll_cubanomic(context: ContextTypes.DEFAULT_TYPE) -> None:
    store: JsonStore = context.application.bot_data["store"]
    settings: Dict[str, Any] = context.application.bot_data["settings"]

    async with httpx.AsyncClient() as client:
        try:
            current_median = await fetch_current_median(client, settings["cubanomic_url"])
        except Exception as exc:  # noqa: BLE001
            logging.warning("No se pudo obtener Cubanomic: %s", exc)
            return

        # Redondear a entero como El Toque (evita spam por decimales)
        current_median = round(current_median)

        last = await store.get_last_median()
        if last is None:
            await store.set_last_median(current_median)
            logging.info("Valor inicial guardado: %s", format_cup(current_median))
            return

        if current_median == last:
            return

        try:
            message = await generate_openrouter_message(
                client,
                settings.get("openrouter_api_key", ""),
                settings.get("openrouter_models", OPENROUTER_MODELS),
                last,
                current_median,
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("OpenRouter falló, usando respaldo: %s", exc)
            message = (
                "Asere, el USD se movió de "
                f"{format_cup(last)} a {format_cup(current_median)} CUP."
            )

        await store.set_last_median(current_median)
        users = await store.list_users()
        if not users:
            logging.info("Cambio detectado pero no hay suscriptores aún.")
            return

        await broadcast_message(context.application, users, message)
        logging.info("Mensaje enviado a %d usuarios", len(users))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    settings = build_settings()
    store = JsonStore(settings["store_path"])

    application = (
        Application.builder()
        .token(settings["telegram_token"])
        .job_queue(JobQueue())
        .build()
    )
    application.bot_data["store"] = store
    application.bot_data["settings"] = settings

    application.add_handler(CommandHandler("start", handle_start))
    application.add_handler(CommandHandler("stop", handle_stop))
    application.add_handler(CommandHandler("status", handle_status))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown)
    )
    job_queue = application.job_queue
    if job_queue is None:
        raise RuntimeError("JobQueue no está inicializado")

    job_queue.run_repeating(
        poll_cubanomic,
        interval=settings["poll_seconds"],
        first=2,
        name="cubanomic-poller",
    )

    logging.info("Bot iniciado. Intervalo: %ss", settings["poll_seconds"])
    application.run_polling()


if __name__ == "__main__":
    main()
