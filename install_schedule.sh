#!/bin/bash
# =============================================================================
# Установка/удаление расписания (launchd, macOS): ежедневная проверка
# источников + еженедельный обход СПбПУ. Путь к проекту подставляется
# автоматически — переносимо между машинами.
#
#   ./install_schedule.sh            # установить и запустить оба агента
#   ./install_schedule.sh uninstall  # выгрузить и удалить
# =============================================================================
set -euo pipefail
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
PLISTS=(com.spbpu.registry-watch com.spbpu.registry-scan-weekly)

if [ "${1:-install}" = "uninstall" ]; then
  for name in "${PLISTS[@]}"; do
    launchctl unload "$AGENTS/$name.plist" 2>/dev/null || true
    rm -f "$AGENTS/$name.plist"
    echo "удалён: $name"
  done
  exit 0
fi

chmod +x "$PROJECT/daily_watch.sh" "$PROJECT/weekly_scan.sh"
mkdir -p "$AGENTS"
for name in "${PLISTS[@]}"; do
  # Подставляем реальный путь проекта в шаблон plist.
  sed "s|__PROJECT_DIR__|$PROJECT|g" "$PROJECT/launchd/$name.plist" > "$AGENTS/$name.plist"
  launchctl unload "$AGENTS/$name.plist" 2>/dev/null || true
  launchctl load "$AGENTS/$name.plist"
  echo "установлен и загружен: $name"
done
echo ""
echo "Проверка:  launchctl list | grep registry"
echo "Путь проекта: $PROJECT"
