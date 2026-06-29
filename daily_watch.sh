#!/bin/bash
# =============================================================================
# Ежедневная проверка источников перечня: нужно ли обновлять список запрещённых.
# Запускается планировщиком (launchd / cron) раз в сутки.
# Код возврата watch_sources.py: 0 — всё актуально; 1 — есть изменения/ошибки.
# =============================================================================
# Каталог проекта определяется автоматически (где лежит сам скрипт) — переносимо.
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT" || exit 2
# shellcheck disable=SC1091
source .venv/bin/activate

mkdir -p logs
LOG="logs/watch_$(date +%Y%m%d_%H%M%S).log"

python watch_sources.py --config config.yaml >> "$LOG" 2>&1
CODE=$?

# Уведомление на рабочий стол, если перечень требует обновления (macOS).
if [ "$CODE" -eq 1 ]; then
  NEED=$(python -c "import json;print(', '.join(json.load(open('output/source_watch_report.json'))['need_update']) or 'см. лог')" 2>/dev/null)
  osascript -e "display notification \"Источники изменились: ${NEED}\" with title \"Реестр запрещённых: нужно обновить\"" 2>/dev/null || true
fi

# Чистим логи старше 90 дней.
find logs -name 'watch_*.log' -mtime +90 -delete 2>/dev/null || true
exit "$CODE"
