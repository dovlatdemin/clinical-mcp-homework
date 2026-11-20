# Клинические MCP-инструменты реабилитации

Практическая работа «Архитектура и реализация протокола Model Context Protocol (MCP)
в клинических веб-приложениях: три паттерна интеграции инструментов».

Три независимых поставщика MCP-инструментов и единый веб-интерфейс с инспектором
JSON-RPC. Предметная область — шкалы медицинской реабилитации.

## Быстрый старт

Развёртывание одной командой (Windows, нужны Python 3.11/3.12 и Node.js 18+):

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1 -Run
```

Скрипт создаёт `.venv`, ставит Python- и npm-зависимости и с флагом `-Run`
сразу поднимает веб-интерфейс. Ручной вариант:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
npm install medical-mcp          # внешний сервер задания 3
.venv/Scripts/python.exe -m streamlit run app.py
```

Сквозные проверки без UI: `python -m clients.stdio_client` (задание 1),
`python -m clients.sse_client` (задание 2), `python -m clients.external_client`
(задание 3). Если браузер для puppeteer не нужен, ставьте пакет с
`PUPPETEER_SKIP_DOWNLOAD=true`.

## Структура

```
homework_mcp/
├── app.py                  # Задание 4: Streamlit UI + инспектор JSON-RPC
├── servers/
│   ├── stdio_server.py     # Задание 1: 4 шкалы реабилитации + list_calculators
│   └── sse_server.py       # Задание 2: 6 калькуляторов по нозологиям + list_scales
├── clients/
│   ├── stdio_client.py     # клиент stdio (initialize/list/call/shutdown, ToolError)
│   ├── sse_client.py       # клиент SSE (sanitizer, smart defaults, retry)
│   ├── external_client.py  # клиент внешнего medical-mcp (таймаут 12 с, кэш 24 ч)
│   └── router.py           # единый роутер трёх источников для UI
├── .cursor/mcp.json        # подключение stdio-сервера к IDE
├── requirements.txt
└── README.md
```

## Задание 1. Локальный сервер по транспорту stdio — готово

Сервер `servers/stdio_server.py` (FastMCP, официальный SDK `mcp`):

- запуск: `python -m servers.stdio_server` из корня `homework_mcp/`;
- обмен JSON-RPC 2.0 строго через `stdin`/`stdout`, журнал — только в `stderr`;
- четыре клинических инструмента со строгой типизацией (type hints + Pydantic):
  - `modified_rankin_scale` — шкала Ранкина (mRS), градации 0–6;
  - `rivermead_mobility_index` — индекс мобильности Ривмид, 15 пунктов, 0–15;
  - `rehab_routing_scale` — шкала реабилитационной маршрутизации (ШРМ), 0–5,
    с рекомендацией этапа МР (приказ Минздрава РФ № 788н, таблица 11);
  - `barthel_index` — индекс Бартел, 10 взвешенных пунктов, 0–100,
    с проверкой допустимых баллов каждого пункта;
- мета-инструмент `list_calculators` — описание методик, границы параметров,
  единицы измерения, ссылки на первоисточники;
- противоречивые параметры отклоняются `ValueError`, который клиент
  преобразует в структурированный `ToolError`.

Клиент `clients/stdio_client.py` поднимает сервер дочерним процессом через
`stdio_client` SDK, выполняет `initialize` → `tools/list` → `tools/call` и
корректно закрывает каналы. Запуск `python -m clients.stdio_client` показывает
полный цикл, включая перехват `ToolError`.

## Задание 2. Локальный сетевой сервер (SSE, порт 8000) — готово

Сервер `servers/sse_server.py` (FastMCP + Uvicorn):

- запуск: `python -m servers.sse_server`; эндпоинты `GET /sse` и
  `POST /messages?session_id=...` на `http://127.0.0.1:8000`;
- шесть инструментов с разбиением по нозологиям + мета-инструмент `list_scales`:
  - общая терапия: `body_mass_index` (ИМТ);
  - кардиология: `cha2ds2_vasc` (риск инсульта при ФП, 0–9);
  - нефрология: `ckd_epi_egfr` (СКФ, CKD-EPI 2021), `cockcroft_gault`;
  - реаниматология: `glasgow_coma_scale` (GCS 3–15), `qsofa` (0–3);
- при выходе показателей за физиологические границы сервер возвращает
  результат со списком `warnings`, а не падает; компоненты шкал вне
  диапазона мягко приводятся к границам с пометкой.

Клиент `clients/sse_client.py`:

- асинхронное подключение через `mcp.client.sse`;
- пул повторных попыток (5 попыток с нарастающей задержкой) при
  недоступности порта;
- Sanitizer: приведение типов из «сырой» формы (строки→числа, «да/нет»→bool,
  запятая→точка, регистр→enum) и Smart Defaults для пустых полей —
  все подстановки возвращаются заметками;
- ошибки сервера преобразуются в тот же `ToolError`, что и у stdio-клиента.

Проверка: `python -m clients.sse_client` поднимает сервер подпроцессом и
прогоняет полный цикл, включая «грязную» форму и выход за границы.

## Задание 3. Внешний распределённый сервер доказательной медицины — готово

Клиент `clients/external_client.py` подключает публичный пакет **medical-mcp**
(github.com/JamesANZ/medical-mcp: OpenFDA, PubMed, DailyMed, RxNorm, WHO):

- установка: `npm install medical-mcp` (с `PUPPETEER_SKIP_DOWNLOAD=true`,
  браузер для Google Scholar не требуется); запуск через `node
  node_modules/medical-mcp/build/index.js` — все вызовы строго по протоколу MCP;
- альтернатива без установки: переменная `EXTERNAL_MCP_URL` с удалённым
  Streamable HTTP эндпоинтом из каталога Glama.ai;
- таймаут вызова **12 с** — интерфейс не зависает при сетевых задержках;
- файловый кэш `data/cache/external/` с TTL 24 ч — защита от Rate Limits
  (повторный идентичный запрос отдаётся мгновенно);
- ошибки и таймауты преобразуются в структурированный `ToolError`.

Доступные внешние инструменты: `search-drugs` (FDA), `get-drug-details`,
`search-medical-literature` (PubMed), `get-article-details`,
`search-drug-nomenclature` (RxNorm), `get-health-statistics` (ВОЗ),
`search-clinical-guidelines`, `search-medical-journals` и др.

Проверка: `python -m clients.external_client` — поиск по PubMed
(первый вызов по сети ~2 с, повторный из кэша ~0 с).

## Задание 4. Веб-интерфейс и инспектор JSON-RPC — готово

Запуск: `streamlit run app.py` из корня `homework_mcp/`.

- **Переключатель источника** — радиокнопка: локальный stdio /
  локальный сетевой :8000 / внешний сервис доказательной медицины;
- **поиск и фильтрация** инструментов по русским и латинским названиям;
- **генератор клинических форм** из JSON-схемы: checkbox для bool,
  выпадающий список для enum, слайдер для малых целых диапазонов,
  числовые поля с шагом; для шкал Ривмид и Бартел — именованные пункты;
- **карточка результата**: главное значение крупно, цветовая индикация
  риска (зелёный/жёлтый/красный), разбор компонентов расчёта,
  рекомендации, ссылка на первоисточник (DOI);
- **инспектор протокола MCP** — аккордеон внизу с сырыми JSON-RPC
  пакетами `tools/call` и `result` последних вызовов;
- все вызовы обёрнуты в перехват исключений: некорректный ввод показывает
  понятное сообщение, приложение не падает;
- клиентский роутер `clients/router.py` держит по одной живой сессии на
  источник в фоновом event loop; SSE-демон поднимается автоматически,
  если порт 8000 закрыт; Sanitizer применяется ко всем источникам.
