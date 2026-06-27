#!/bin/bash
# =============================================================================
# Еженедельный полный обход всех сайтов СПбПУ (структура + поддомены) и поиск
# вхождений запрещённых организаций/брендов из перечня.
# Запускается планировщиком раз в неделю. Полный проход — длительный (часы).
# =============================================================================
PROJECT="/Users/artemy/Desktop/практ/registry_merger"
cd "$PROJECT" || exit 2
# shellcheck disable=SC1091
source .venv/bin/activate

mkdir -p logs output/archive
TS=$(date +%Y%m%d)
LOCK="output/weekly_scan.lock"

# --- Защита от наложения: если прошлый обход ещё идёт, выходим. ---
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  echo "$(date) Предыдущий обход ещё выполняется (PID $(cat "$LOCK")). Пропуск." \
    >> logs/weekly_scan_skipped.log
  exit 0
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

# --- Архивируем отчёт прошлой недели (чтобы видеть динамику). ---
for ext in xlsx csv; do
  [ -f "output/scan_spbstu.${ext}" ] && \
    cp "output/scan_spbstu.${ext}" "output/archive/scan_spbstu_${TS}.${ext}"
done

# --- Свежий полный проход (сбрасываем состояние обхода). ---
rm -f output/scan_state.txt
LOG="logs/weekly_scan_${TS}.log"
python scan.py --config scan_config.yaml >> "$LOG" 2>&1
CODE=$?

# --- Уведомление с числом находок (macOS). ---
HITS=$(python -c "import pandas as pd;print(len(pd.read_csv('output/scan_spbstu.csv')))" 2>/dev/null || echo "?")
osascript -e "display notification \"Найдено совпадений: ${HITS}. Отчёт: output/scan_spbstu.xlsx\" with title \"Еженедельный обход СПбПУ завершён\"" 2>/dev/null || true

# --- Чистим архив и логи старше 180 дней. ---
find output/archive -name 'scan_spbstu_*' -mtime +180 -delete 2>/dev/null || true
find logs -name 'weekly_scan_*.log' -mtime +180 -delete 2>/dev/null || true
exit "$CODE"
