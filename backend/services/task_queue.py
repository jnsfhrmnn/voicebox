"""Background-task helper.

JFW-1 (Transkriptions-Produktprofil): Die serielle TTS-Generation-Queue
(GenerationJob, enqueue_generation, cancel_generation, init_queue) ist
entfernt -- es gibt im aktiven Profil keine Generationen mehr. Live bleibt
nur ``create_background_task``: Fire-and-forget-Tasks (Whisper-Download,
Modell-Migration) brauchen eine Referenz, damit der GC sie nicht wegräumt.
"""

import asyncio

# Keep references to fire-and-forget background tasks to prevent GC
_background_tasks: set = set()


def create_background_task(coro) -> asyncio.Task:
    """Create a background task and prevent it from being garbage collected."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
